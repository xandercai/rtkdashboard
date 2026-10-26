# Turns the raw GNSS files rclone_sync.py already pulls into the "gnss"
# cache (.ubx from a u-blox F9P, .rtcm3 from a Unicore UM980 - see
# _RAW_FORMATS) into two things gnssrefl (https://github.com/kristinemlarson/gnssrefl)
# can compute from GNSS interferometric reflectometry:
#
#   - a per-day GNSS signal-quality summary (cache/gnss_quality/*.json)
#   - a rolling soil-moisture (volumetric water content) time series
#     (cache/soil_moisture/*.json)
#
# Pipeline per calendar day (UTC): merge that day's 2-hourly raw files ->
# convert to RINEX with RTKLIB's convbin -> gnssrefl's rinex2snr ->
# gnssrefl's gnssir -> aggregate the results file into one JSON record.
# Soil moisture is recomputed from the accumulated history each run via
# gnssrefl's vwc module, since VWC leveling is inherently a whole-record
# computation (it normalizes against the wettest/driest points seen so
# far), not an incremental one.
#
# RINEX version: configurable per-station (stations.py's "rinex_version")
# rather than hardcoded - see
# _convert_to_rinex/_convert_to_rinex_v2/_convert_to_rinex_v3. "3.04" (the
# default) carries all four constellations, including BeiDou, in one mixed
# obs file; "2.11" is simpler (plain 4-char station naming) but
# structurally has no slot for BeiDou at all, so it caps out at
# GPS+GLONASS+Galileo. Both paths stay fully implemented so switching is a
# one-line config change per station, not a rewrite.
#
# Orbits: computing a satellite's elevation/azimuth (and hence its arc's
# reflector height) needs that constellation's orbit. GFZ's "gbm" rapid
# multi-GNSS orbit (GPS+GLONASS+Galileo+BeiDou) is what gives per-arc
# results for all four instead of GPS only - but it lags real time by ~2
# days (see _run_rinex2snr_with_fallback), too slow for "as of today". So
# every day is tried with gbm first and falls back to the GPS-only
# broadcast nav message extracted from the station's own raw file (-orb
# nav, seeded via _seed_local_nav so no external download is needed at
# all) when gbm isn't published yet. _select_dates_to_process() cheaply
# re-checks gbm availability for nav-only days on later runs (see
# gnssrefl_check_gbm.py) so a day that fell back to nav-only gets
# automatically upgraded to multi-GNSS gbm results once it's published,
# without redoing every recent day's full pipeline just to find out.

import glob
import json
import logging
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timedelta, timezone

import config
import stations
from rclone_sync import cache_dir_for

logger = logging.getLogger(__name__)

_FILENAME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})_GNSS")

# Raw file extension inside each .7z -> convbin's -r input format. rtkbase's
# archive_and_clean.sh names the extension after the station's
# receiver_format, so the extension alone says which receiver wrote it - a
# station that swaps an F9P for a UM980 keeps working without any config
# change here.
_RAW_FORMATS = {
    "ubx": "ubx",      # u-blox F9P, RXM-RAWX
    "rtcm3": "rtcm3",  # Unicore UM980, RTCM3 MSM7 + ephemerides
}

# SNR file column layout gnssrefl writes (see rinex2snr): satellite,
# elevation, azimuth, seconds-of-day, elev-rate, S6, S1, S2, S5, S7, S8.
# All in dB-Hz except where noted. Only S1/S2 are populated for this
# receiver (see convbin's -os signal set below), so those are the only
# ones averaged for the "mean C/N0" figure.
_SNR_S1_COL = 6
_SNR_S2_COL = 7

# gnssir results file columns (see its own header comment): year, doy, RH
# (m), sat, UTCtime, azim, amplitude, eminO, emaxO, n values, freq,
# rise/set, edot factor, peak2noise, arc length (min), MJD, refraction model.
_RES_RH_COL = 2
_RES_SAT_COL = 3
_RES_UTCTIME_COL = 4
_RES_AZIM_COL = 5
_RES_AMP_COL = 6
_RES_EMIN_COL = 7
_RES_EMAX_COL = 8
_RES_FREQ_COL = 10
_RES_RISE_COL = 11
_RES_PK2NOISE_COL = 13

# gnssir's frequency codes (see gnssir_input's -allfreq), grouped by GNSS
# constellation for chart coloring, and labeled for tooltips/CSV export.
# Requested via -allfreq T in _ensure_station_registered, but only ever
# populated for constellations the RINEX obs + orbit actually cover: in
# RINEX 2.11 mode BeiDou has no navigation-message slot at all (see
# _convert_to_rinex_v2), and GLONASS/Galileo/BeiDou only get positions
# (hence arcs) when the -orb gbm multi-GNSS orbit was used rather than the
# GPS-only nav fallback (see _run_rinex2snr_with_fallback).
_FREQ_SYSTEM = {
    1: "gps", 5: "gps", 20: "gps",
    101: "glonass", 102: "glonass",
    201: "galileo", 205: "galileo", 206: "galileo", 207: "galileo", 208: "galileo",
    301: "beidou", 302: "beidou", 305: "beidou", 306: "beidou", 307: "beidou", 308: "beidou",
}
_FREQ_LABEL = {
    1: "GPS L1", 5: "GPS L5", 20: "GPS L2C",
    101: "GLONASS L1", 102: "GLONASS L2",
    201: "Galileo E1", 205: "Galileo E5a", 206: "Galileo E6", 207: "Galileo E5b", 208: "Galileo E5(a+b)",
    301: "BeiDou B1", 302: "BeiDou B2", 305: "BeiDou B2a", 306: "BeiDou B3", 307: "BeiDou B2b", 308: "BeiDou B2(a+b)",
}

# Per-station locks/status, not single globals - two different stations'
# "Run GNSS-IR analysis" buttons shouldn't block or reflect each other.
# _run_locks is built once at import time from the fixed station list (see
# stations.py), so no lazy-creation race to guard against. _run_locks[h]
# enforces "only one run at a time for station h" (held for the run's whole
# duration); _status_lock only ever guards the _status dict below and is
# held just long enough to read/write it - sharing one lock for both would
# deadlock, since _set_status() is called while _run_locks[h] is already
# held by the in-progress run.
_run_locks = {s["hostname"]: threading.Lock() for s in stations.list_stations()}
_status_lock = threading.Lock()
_status = {}  # hostname -> {"running": bool, "message": str}


def status(hostname):
    with _status_lock:
        return dict(_status.get(hostname, {"running": False, "message": ""}))


def _set_status(hostname, running, message):
    with _status_lock:
        _status[hostname] = {"running": running, "message": message}


def _quality_cache_dir(hostname):
    return cache_dir_for("gnss_quality", hostname)


def _soil_moisture_cache_dir(hostname):
    return cache_dir_for("soil_moisture", hostname)


def _arc_detail_cache_dir(hostname, date_str):
    return os.path.join(cache_dir_for("gnss_arc_detail", hostname), date_str)


def _work_dir(hostname):
    return cache_dir_for("_gnssrefl_work", hostname)


def _gnssrefl_env():
    env = os.environ.copy()
    env["REFL_CODE"] = config.REFL_CODE
    env["ORBITS"] = config.ORBITS_DIR
    env["EXE"] = config.EXE_DIR
    return env


def _run(cmd, cwd, env, timeout=600):
    logger.info("gnssrefl pipeline: running %s (cwd=%s)", " ".join(cmd), cwd)
    result = subprocess.run(
        cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed (exit {result.returncode}): {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def _available_dates(hostname):
    """UTC calendar dates (YYYY-MM-DD) that have at least one synced raw
    GNSS archive, derived from the filename's own window-start timestamp - the
    same field get_data_size_series() falls back to when a file's mtime
    isn't available (see parsers.py)."""
    dates = set()
    for path in glob.glob(os.path.join(cache_dir_for("gnss", hostname), "*.7z")):
        m = _FILENAME_RE.match(os.path.basename(path))
        if m:
            dates.add(f"{m.group(1)}-{m.group(2)}-{m.group(3)}")
    return sorted(dates)


def _quality_json_path(hostname, date_str):
    return os.path.join(_quality_cache_dir(hostname), f"{date_str}_quality.json")


def _source_file_count(hostname, date_str):
    return len(glob.glob(os.path.join(cache_dir_for("gnss", hostname), f"{date_str}_*.7z")))


def _check_gbm_available(hostname, dates, env):
    """Batch-probes gnssrefl_check_gbm.py (in the gnssrefl env - needs
    gps.py's import chain) for whether the multi-GNSS gbm orbit has been
    published yet for each of the given dates. One subprocess call for
    however many dates need checking, not one per date."""
    work_dir = _work_dir(hostname)
    out_path = os.path.join(work_dir, "gbm_check.json")
    try:
        _run([
            config.GNSSREFL_PYTHON,
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "gnssrefl_check_gbm.py"),
            out_path, *dates,
        ], cwd=work_dir, env=env, timeout=180)
        with open(out_path, encoding="utf-8") as f:
            return json.load(f)
    except (RuntimeError, OSError, json.JSONDecodeError) as exc:
        logger.info("gnssrefl pipeline: gbm availability check failed: %s", exc)
        return {}
    finally:
        if os.path.exists(out_path):
            os.remove(out_path)


def _select_dates_to_process(hostname, dates, force, env):
    """Decides which of the available dates are actually worth
    (re)processing this run - genuinely incremental, not "redo everything
    in the reprocess window every time":

    - no result yet, or -force: always process.
    - outside the reprocess window (see config.GNSSREFL_REPROCESS_WINDOW_DAYS):
      never - treated as final, whatever orbit it ended up with.
    - within the window: only if new .7z files arrived since the last
      pass for that date (a real reason to redo it), or - checked in one
      batch up front, since it's much cheaper than a full reprocess - a
      day still on the GPS-only nav fallback now has the multi-GNSS gbm
      orbit available. A day that's already on gbm with no new files is
      left alone even if it's inside the window.
    """
    today = datetime.now(timezone.utc).date()
    to_process = []
    gbm_candidates = []
    for date_str in dates:
        path = _quality_json_path(hostname, date_str)
        if force or not os.path.exists(path):
            to_process.append(date_str)
            continue
        day = datetime.strptime(date_str, "%Y-%m-%d").date()
        if (today - day).days > config.GNSSREFL_REPROCESS_WINDOW_DAYS:
            continue
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
        if record.get("source_file_count") != _source_file_count(hostname, date_str):
            to_process.append(date_str)  # new upload(s) since last pass
        elif record.get("orbit_source") == "nav":
            gbm_candidates.append(date_str)  # worth a cheap gbm check

    if gbm_candidates:
        newly_available = _check_gbm_available(hostname, gbm_candidates, env)
        to_process.extend(d for d in gbm_candidates if newly_available.get(d))

    return sorted(set(to_process))


def _extract_day_raw(hostname, date_str, work_dir):
    """7z-extracts every archive whose filename starts with date_str into
    work_dir, then concatenates them (sorted by filename, i.e. by the
    window-start time each one covers) into one merged raw file. UBX and
    RTCM3 are both streams of self-delimited, checksummed messages, so
    concatenating whole files in chronological order reconstructs the same
    stream the receiver would have produced running continuously.

    Returns {"path", "format", "start"} (format is convbin's -r value,
    start the first file's window-start time as a UTC datetime), or None.
    A day holding both formats (the receiver was swapped that day) keeps
    only whichever has more files - convbin reads one format per run."""
    archives = sorted(glob.glob(os.path.join(cache_dir_for("gnss", hostname), f"{date_str}_*.7z")))
    if not archives:
        return None
    for archive in archives:
        subprocess.run(
            ["7z.exe", "e", "-y", f"-o{work_dir}", archive],
            capture_output=True, text=True, timeout=120, check=True,
        )
    by_ext = {}
    for path in sorted(glob.glob(os.path.join(work_dir, f"{date_str}_*_GNSS.*"))):
        ext = os.path.splitext(path)[1].lstrip(".").lower()
        if ext in _RAW_FORMATS:
            by_ext.setdefault(ext, []).append(path)
    if not by_ext:
        return None
    ext = max(by_ext, key=lambda e: len(by_ext[e]))
    if len(by_ext) > 1:
        logger.warning("gnssrefl pipeline: %s/%s has mixed raw formats %s, using only .%s",
                       hostname, date_str, sorted(by_ext), ext)
    raw_files = by_ext[ext]
    m = _FILENAME_RE.match(os.path.basename(raw_files[0]))
    start = datetime(*map(int, m.groups()), tzinfo=timezone.utc)
    merged_path = os.path.join(work_dir, f"merged.{ext}")
    with open(merged_path, "wb") as out:
        for path in raw_files:
            with open(path, "rb") as f:
                shutil.copyfileobj(f, out)
    for paths in by_ext.values():
        for path in paths:
            os.remove(path)
    return {"path": merged_path, "format": _RAW_FORMATS[ext], "start": start}


def _convbin_input_args(raw, receiver):
    """convbin's input format and receiver header options for one merged
    raw file. RTCM3 MSM carries only time-of-week, not the GPS week, so
    convbin needs -tr (an approximate start time) to place it in the
    right week - the first file's window-start time from its own filename
    is close enough, and RTKLIB keeps following the stream's own time
    from there on, so one -tr covers a whole day."""
    args = ["-r", raw["format"]]
    if raw["format"] == "rtcm3":
        args += ["-tr", raw["start"].strftime("%Y/%m/%d"), raw["start"].strftime("%H:%M:%S")]
    return args + ["-hr", f"1/{receiver}"]


# RINEX 3.04 IGS long-filename convention rinex2snr.py's nolook mode
# expects for a 9-character station (see its NS==9 branch): a fixed "R"
# stream and "01D" (1-day) period, with the sample rate baked into the
# name itself. Declared as 1 Hz / "R" since that's what the receiver's raw
# log actually is - no resampling happens in _convert_to_rinex_v3.
_RINEX3_SAMPLERATE = 1
_RINEX3_STREAM = "R"


def _rinex3_obs_filename(station9, year, doy):
    return f"{station9.upper()}_{_RINEX3_STREAM}_{year}{doy:03d}0000_01D_{_RINEX3_SAMPLERATE:02d}S_MO.rnx"


def _rinex3_nav_filename(station9, year, doy):
    return f"{station9.upper()}_{_RINEX3_STREAM}_{year}{doy:03d}0000_01D_MN.rnx"


def _convert_to_rinex_v2(raw, receiver, station, year, doy, work_dir):
    """RINEX 2.11 conversion: GPS/GLONASS/Galileo only, since RINEX 2.11
    has no navigation-message slot for BeiDou at all (it predates BeiDou's
    maturity) - no amount of receiver support or orbit availability can
    get around that at this format layer. Simpler station naming (plain
    4-char) than v3.04 though, so kept working as an option (see
    stations.py's "rinex_version") rather than replaced outright."""
    obs_path, nav_path = _extract_v2_obs_and_nav(raw, receiver, station, year, doy, work_dir)
    return {"rinex_version": "2.11", "obs_path": obs_path, "nav_path": nav_path, "rinex2snr_station": station}


def _convert_to_rinex_v3(raw, receiver, station, station9, year, doy, work_dir):
    """RINEX 3.04 conversion: adds BeiDou (a single mixed-system obs file
    covers GPS/GLONASS/Galileo/BeiDou together in this version) at the
    cost of needing gnssrefl's 9-character IGS-style station naming
    convention for the obs file it expects to find locally.

    Still runs the v2.11 GPS-only extraction too, purely for its nav
    output: gnssrefl's -orb nav fallback (see _run_rinex2snr_with_fallback)
    always wants a GPS-only, RINEX-2.11-format broadcast nav file
    regardless of what RINEX version the actual observation data is in
    (gps.nav_name() hardcodes that filename/format), so reusing
    _extract_v2_obs_and_nav here - rather than teaching _seed_local_nav
    to understand a RINEX 3 mixed nav file - is the "don't rebuild what
    already works" option.
    """
    _, nav_path = _extract_v2_obs_and_nav(raw, receiver, station, year, doy, work_dir)

    tmp_obs, tmp_nav = "tmp3.rnx", "tmp3.nav"
    _run([
        "convbin.exe", *_convbin_input_args(raw, receiver), "-v", "3.04", "-os", "-od", "-oi", "-ot", "-ol",
        "-hm", station.upper(), "-ha", "0/GNSS",
        "-o", tmp_obs, "-n", tmp_nav, raw["path"],
    ], cwd=work_dir, env=os.environ, timeout=300)
    tmp_obs_path = os.path.join(work_dir, tmp_obs)
    if not os.path.exists(tmp_obs_path):
        raise RuntimeError(f"convbin did not produce {tmp_obs}")
    # The mixed (all-constellation) nav file isn't used by gnssrefl -
    # nav_path (GPS-only, from the v2.11 pass above) is what -orb nav
    # seeds - but it's the one worth keeping alongside the obs file when
    # config.GNSSREFL_KEEP_RINEX is on (see _keep_rinex).
    tmp_nav_path = os.path.join(work_dir, tmp_nav)
    mixed_nav_path = None
    if os.path.exists(tmp_nav_path):
        mixed_nav_path = os.path.join(work_dir, _rinex3_nav_filename(station9, year, doy))
        shutil.move(tmp_nav_path, mixed_nav_path)

    obs_path = os.path.join(work_dir, _rinex3_obs_filename(station9, year, doy))
    shutil.move(tmp_obs_path, obs_path)
    return {"rinex_version": "3.04", "obs_path": obs_path, "nav_path": nav_path,
            "mixed_nav_path": mixed_nav_path, "rinex2snr_station": station9}


def _convert_to_rinex(raw, receiver, station, station9, rinex_version, year, doy, work_dir):
    if rinex_version == "3.04":
        return _convert_to_rinex_v3(raw, receiver, station, station9, year, doy, work_dir)
    return _convert_to_rinex_v2(raw, receiver, station, year, doy, work_dir)


def _extract_v2_obs_and_nav(raw, receiver, station, year, doy, work_dir):
    yy = f"{year % 100:02d}"
    obs_name = f"{station}{doy:03d}0.{yy}o"
    nav_name = f"{station}{doy:03d}0.{yy}n"
    _run([
        "convbin.exe", *_convbin_input_args(raw, receiver), "-v", "2.11", "-os", "-od", "-oi", "-ot", "-ol",
        "-hm", station.upper(), "-ha", "0/GNSS",
        "-o", obs_name, "-n", nav_name, raw["path"],
    ], cwd=work_dir, env=os.environ, timeout=300)
    obs_path = os.path.join(work_dir, obs_name)
    nav_path = os.path.join(work_dir, nav_name)
    if not os.path.exists(obs_path):
        raise RuntimeError(f"convbin did not produce {obs_name}")
    return obs_path, nav_path


def _keep_rinex(hostname, conv):
    """Moves a day's RINEX obs file and its nav file (the mixed one in
    3.04 mode, else the GPS-only 2.11 one) into config.RINEX_DIR/<hostname>/,
    replacing any copy from an earlier pass over the same day. Called only
    after rinex2snr has finished with them (see _process_day)."""
    dest_dir = os.path.join(config.RINEX_DIR, hostname)
    os.makedirs(dest_dir, exist_ok=True)
    for path in (conv["obs_path"], conv.get("mixed_nav_path") or conv["nav_path"]):
        if not path or not os.path.exists(path):
            continue
        target = os.path.join(dest_dir, os.path.basename(path))
        if os.path.exists(target):
            os.remove(target)
        shutil.move(path, target)


def rinex_storage_warning():
    """A warning for the dashboard once config.RINEX_DIR (every station's
    kept RINEX together) has grown past config.RINEX_WARN_GB, else None.
    Nothing is pruned automatically - which days to delete is the user's
    call. Checked whether or not GNSSREFL_KEEP_RINEX is currently on, so
    switching it off doesn't hide a folder that's already full."""
    total = 0
    for root, _, files in os.walk(config.RINEX_DIR):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    gb = total / 1024 ** 3
    if gb <= config.RINEX_WARN_GB:
        return None
    return (f"Kept RINEX in {config.RINEX_DIR} is {gb:.1f} GB, over the {config.RINEX_WARN_GB:g} GB "
            f"warning threshold - delete days you no longer need, or turn off RTKDASH_GNSSREFL_KEEP_RINEX.")


def _seed_local_nav(nav_path, year, doy, work_dir):
    """gnssrefl's -orb nav looks for a file named auto<doy>0.<yy>n in the
    current directory (or its own ORBITS store) before trying to download
    one from SOPAC/CDDIS (see gps.nav_name/navfile_retrieve). Pre-seeding
    it with the nav message extracted from the station's own raw file
    means it's always "found" locally and gnssrefl never needs to reach an
    external orbit archive at all."""
    yy = f"{year % 100:02d}"
    auto_name = f"auto{doy:03d}0.{yy}n"
    shutil.copyfile(nav_path, os.path.join(work_dir, auto_name))


def _mean_snr(snr_path):
    total = 0.0
    count = 0
    with open(snr_path, encoding="utf-8") as f:
        for line in f:
            parts = line.split()
            if len(parts) <= _SNR_S2_COL:
                continue
            for col in (_SNR_S1_COL, _SNR_S2_COL):
                try:
                    v = float(parts[col])
                except ValueError:
                    continue
                if v > 0:
                    total += v
                    count += 1
    return (round(total / count, 3), count) if count else (None, 0)


def _arc_timestamp(date_str, utc_hours):
    day = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (day + timedelta(hours=utc_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _process_results_file(results_path, date_str):
    """One point per successful gnssir arc (a single satellite rise or
    set), not aggregated - this is what lets the dashboard plot reflector
    height the way gnssrefl's own real-time demos do (a scatter over time,
    colored by constellation), rather than a single per-day average."""
    arcs = []
    with open(results_path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("%") or not line.strip():
                continue
            parts = line.split()
            if len(parts) <= _RES_PK2NOISE_COL:
                continue
            try:
                rh = float(parts[_RES_RH_COL])
                sat = int(float(parts[_RES_SAT_COL]))
                utc_hours = float(parts[_RES_UTCTIME_COL])
                azim = float(parts[_RES_AZIM_COL])
                amp = float(parts[_RES_AMP_COL])
                emin = float(parts[_RES_EMIN_COL])
                emax = float(parts[_RES_EMAX_COL])
                freq = int(float(parts[_RES_FREQ_COL]))
                rise_set = int(float(parts[_RES_RISE_COL]))
                pk2noise = float(parts[_RES_PK2NOISE_COL])
            except ValueError:
                continue
            system = _FREQ_SYSTEM.get(freq)
            if system is None:
                continue
            arcs.append({
                "t": _arc_timestamp(date_str, utc_hours),
                "sat": sat,
                "freq": freq,
                "system": system,
                "freq_label": _FREQ_LABEL.get(freq, str(freq)),
                "rh_m": round(rh, 3),
                "azimuth_deg": round(azim, 1),
                "amplitude": round(amp, 2),
                "peak2noise": round(pk2noise, 2),
                # observed elevation-angle range swept during this arc (a
                # rise or set, not an instant) - lets the dashboard filter
                # "signals seen between elevation X-Y deg" the same way it
                # already can by azimuth.
                "elevation_min_deg": round(emin, 1),
                "elevation_max_deg": round(emax, 1),
                # -1 (set) / 1 (rise) - a satellite's rising and setting
                # passes normally sweep very different azimuths, but not
                # always, so this rides along specifically so
                # parsers.get_gnss_track_candidates can tell them apart
                # rather than relying on azimuth alone to disambiguate
                # "same track" across days.
                "rise_set": rise_set,
            })
    return arcs


def _process_failqc_file(failqc_path):
    """gnssir always writes results/<station>/failQC/<doy>.txt alongside
    the main results file - same column layout, but RH/amplitude/peak2noise
    are NaN for every row: these arcs never got a trustworthy periodogram
    peak at all, not "computed but below threshold". Investigating a run of
    real failQC data (2026-07-28 through 2026-08-23, both stations) found
    the dominant cause is directional: arcs that never swept down near the
    station's own elevation mask (see stations.py's "-e1 5") before the
    satellite set again read as a short, incomplete SNR oscillation gnssir
    can't extract a peak from - and which azimuths that happens at is
    static per site (whatever blocks the antenna's low-elevation view in
    that direction - terrain, a building, a mast). Azimuth/elevation are
    kept per rejected arc (not just a count) specifically so
    parsers.get_gnss_rejected_arcs can surface that pattern back to
    the dashboard, rather than a single opaque "how many rejected" number
    that can't distinguish a site-wide QC threshold issue from a
    site-specific obstructed direction."""
    arcs = []
    with open(failqc_path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("%") or not line.strip():
                continue
            parts = line.split()
            if len(parts) <= _RES_FREQ_COL:
                continue
            try:
                sat = int(float(parts[_RES_SAT_COL]))
                azim = float(parts[_RES_AZIM_COL])
                emin = float(parts[_RES_EMIN_COL])
                emax = float(parts[_RES_EMAX_COL])
                freq = int(float(parts[_RES_FREQ_COL]))
            except ValueError:
                continue
            system = _FREQ_SYSTEM.get(freq)
            if system is None:
                continue
            arcs.append({
                "sat": sat,
                "system": system,
                "azimuth_deg": round(azim, 1),
                "elevation_min_deg": round(emin, 1),
                "elevation_max_deg": round(emax, 1),
            })
    return arcs


def yy_of(year):
    return f"{year % 100:02d}"


def _write_arc_details(hostname, station, year, doy, date_str, env):
    """Runs gnssrefl_arc_detail.py (in the gnssrefl env, which has the
    numpy/astropy this needs - see config.GNSSREFL_PYTHON) to reconstruct
    each kept arc's raw SNR curve and Lomb-Scargle periodogram from
    gnssir's -savearcs output, and cache them per-arc so the dashboard can
    show the same two-panel plot gnssrefl's own diagnostics do when a
    point on the RH chart is clicked. Mutates _quality_json_path(hostname,
    date_str) in place, adding an "arc_key" to each arc that got a detail
    file.

    Best-effort: a failure here shouldn't cost the day's actual RH results,
    which are already written by the time this runs - so it logs and
    moves on rather than propagating, and the frontend already handles a
    missing arc_key (no popup for that point) as the normal case for
    older, not-yet-reprocessed days.
    """
    detail_dir = _arc_detail_cache_dir(hostname, date_str)
    if os.path.exists(detail_dir):
        shutil.rmtree(detail_dir)
    try:
        _run([
            config.GNSSREFL_PYTHON, os.path.join(os.path.dirname(os.path.abspath(__file__)), "gnssrefl_arc_detail.py"),
            station, str(year), str(doy), _quality_json_path(hostname, date_str), detail_dir,
        ], cwd=_work_dir(hostname), env=env, timeout=300)
    except RuntimeError as exc:
        logger.info("gnssrefl pipeline: arc detail generation failed for %s: %s", date_str, exc)
    finally:
        arcs_dir = os.path.join(config.REFL_CODE, str(year), "arcs", station, f"{doy:03d}")
        shutil.rmtree(arcs_dir, ignore_errors=True)


def _run_rinex2snr_with_fallback(rinex2snr_station, station, year, doy, day_work, env, extra_args=()):
    """Tries the multi-GNSS "gbm" orbit first (needed for GLONASS/Galileo/
    BeiDou arcs, not just GPS), falls back to the always-available
    GPS-only broadcast nav if gbm isn't published yet. rinex2snr's CLI
    exits 0 either way (see gps.get_orbits_setexe - it just prints "does
    not exist" and moves on), so the only reliable signal is whether it
    actually produced a .snr99(.gz) file.

    rinex2snr_station is what gets passed on the command line (4-char for
    RINEX 2.11, 9-char for RINEX 3.04 - see _convert_to_rinex); station is
    always the plain 4-char form, since that's what rinex2snr itself uses
    for the resulting SNR/results file paths regardless of which RINEX
    version fed it (it truncates a 9-char name internally for those - see
    rinex2snr.py's `station = station[0:4].lower()`).

    Returns the orbit source that worked ("gbm" or "nav"), or raises if
    neither did.
    """
    yy = yy_of(year)
    snr_path = os.path.join(config.REFL_CODE, str(year), "snr", station, f"{station}{doy:03d}0.{yy}.snr99")
    for orbit in ("gbm", "nav"):
        for p in (snr_path, snr_path + ".gz"):
            if os.path.exists(p):
                os.remove(p)
        try:
            _run(["rinex2snr.exe", rinex2snr_station, str(year), str(doy),
                  "-nolook", "T", "-orb", orbit, "-snr", "99", "-overwrite", "T", *extra_args],
                 cwd=day_work, env=env, timeout=600)
        except RuntimeError as exc:
            logger.info("gnssrefl pipeline: rinex2snr -orb %s failed for doy %s: %s", orbit, doy, exc)
            continue
        if os.path.exists(snr_path) or os.path.exists(snr_path + ".gz"):
            return orbit
    raise RuntimeError(f"rinex2snr produced no SNR file for doy {doy} with any orbit source")


def _process_day(hostname, date_str, station, station9, rinex_version, receiver, work_dir):
    year = int(date_str[0:4])
    doy = datetime.strptime(date_str, "%Y-%m-%d").timetuple().tm_yday

    day_work = os.path.join(work_dir, date_str)
    os.makedirs(day_work, exist_ok=True)
    try:
        raw = _extract_day_raw(hostname, date_str, day_work)
        if raw is None:
            return

        conv = _convert_to_rinex(raw, receiver, station, station9, rinex_version, year, doy, day_work)
        os.remove(raw["path"])
        _seed_local_nav(conv["nav_path"], year, doy, day_work)

        env = _gnssrefl_env()
        extra_args = ("-samplerate", str(_RINEX3_SAMPLERATE), "-stream", _RINEX3_STREAM) \
            if conv["rinex_version"] == "3.04" else ()
        orbit_source = _run_rinex2snr_with_fallback(
            conv["rinex2snr_station"], station, year, doy, day_work, env, extra_args)
        # The multi-GB RINEX obs file has done its job feeding rinex2snr;
        # nothing downstream needs it and it's cheap to regenerate from the
        # (still-cached) source .7z archives if it's ever needed again -
        # unless it's been asked to be kept for use outside this dashboard.
        if config.GNSSREFL_KEEP_RINEX:
            _keep_rinex(hostname, conv)
        elif os.path.exists(conv["obs_path"]):
            os.remove(conv["obs_path"])

        _run(["gnssir.exe", station, str(year), str(doy),
              "-snr", "99", "-screenstats", "F",
              "-savearcs", "T", "-savearcs_format", "txt"], cwd=day_work, env=env, timeout=600)

        snr_path = os.path.join(config.REFL_CODE, str(year), "snr", station, f"{station}{doy:03d}0.{yy_of(year)}.snr99")
        mean_snr, snr_count = (None, 0)
        gz_path = snr_path + ".gz"
        if os.path.exists(gz_path):
            import gzip as _gzip
            tmp_path = snr_path
            with _gzip.open(gz_path, "rt", encoding="utf-8") as gz_in, open(tmp_path, "w", encoding="utf-8") as plain_out:
                shutil.copyfileobj(gz_in, plain_out)
            mean_snr, snr_count = _mean_snr(tmp_path)
            os.remove(tmp_path)
        elif os.path.exists(snr_path):
            mean_snr, snr_count = _mean_snr(snr_path)

        results_path = os.path.join(config.REFL_CODE, str(year), "results", station, f"{doy:03d}.txt")
        arcs = []
        if os.path.exists(results_path):
            arcs = _process_results_file(results_path, date_str)

        arc_counts = {}
        for arc in arcs:
            arc_counts[arc["system"]] = arc_counts.get(arc["system"], 0) + 1

        failqc_path = os.path.join(config.REFL_CODE, str(year), "results", station, "failQC", f"{doy:03d}.txt")
        rejected_arcs = _process_failqc_file(failqc_path) if os.path.exists(failqc_path) else []

        record = {
            "date": date_str,
            "year": year,
            "doy": doy,
            "orbit_source": orbit_source,
            # Snapshot of how many source .7z archives fed this result -
            # see _select_dates_to_process, which compares this against
            # the current count to tell "new upload arrived" apart from
            # "nothing changed, don't bother redoing this day".
            "source_file_count": _source_file_count(hostname, date_str),
            "mean_snr_dbhz": mean_snr,
            "snr_sample_count": snr_count,
            "arc_counts": arc_counts,
            "rejected_total": len(rejected_arcs),
            "arcs": arcs,
            "rejected_arcs": rejected_arcs,
        }
        with open(_quality_json_path(hostname, date_str), "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2)
        logger.info("gnssrefl pipeline: wrote quality summary for %s/%s (orbit=%s, %d arcs)", hostname, date_str, orbit_source, len(arcs))

        _write_arc_details(hostname, station, year, doy, date_str, env)
    finally:
        shutil.rmtree(day_work, ignore_errors=True)


def _ensure_station_registered(hostname, station, lat, lon, height_m, e1_deg, e2_deg):
    json_path = os.path.join(config.REFL_CODE, "input", station, f"{station}.json")
    if os.path.exists(json_path):
        # Re-register if the configured elevation mask or position has
        # drifted from what's actually on disk (e.g. stations.py's
        # "e1_deg"/"e2_deg" was widened for a horizontal-antenna station
        # after its first run, or a provisional lat/lon was surveyed) -
        # gnssir_input.exe only writes these into this json once, so
        # without this check a changed config would silently keep using
        # whatever was registered the very first time.
        with open(json_path, encoding="utf-8") as f:
            existing = json.load(f)
        registered = (existing.get("e1"), existing.get("e2"), existing.get("lat"), existing.get("lon"), existing.get("ht"))
        configured = (e1_deg, e2_deg, lat, lon, height_m)
        if registered == configured:
            return
        logger.info(
            "gnssrefl pipeline: %s e1/e2/lat/lon/height changed (%s -> %s), re-registering",
            station, registered, configured)
    env = _gnssrefl_env()
    _run([
        "gnssir_input.exe", station,
        "-lat", str(lat), "-lon", str(lon),
        "-height", str(height_m), "-allfreq", "T",
        "-e1", str(e1_deg), "-e2", str(e2_deg),
    ], cwd=_work_dir(hostname), env=env, timeout=120)


def _update_soil_moisture(hostname, station):
    dates = _available_dates(hostname)
    if not dates:
        return
    start_year = int(dates[0][0:4])
    env = _gnssrefl_env()

    # vwc_input reuses Files/<station>/tracks.json if it already exists
    # rather than rebuilding it from the current results/ directory, so a
    # stale file left over from an earlier (smaller) run would silently
    # keep VWC blind to every day processed since. Since this whole
    # function already recomputes VWC from scratch each call (see the
    # module docstring), dropping the cache here is just closing that gap,
    # not adding new cost.
    tracks_json = os.path.join(config.REFL_CODE, "Files", station, "tracks.json")
    if os.path.exists(tracks_json):
        os.remove(tracks_json)

    try:
        _run([
            "vwc_input.exe", station, str(start_year),
            "-min_req_pts_track", str(config.GNSSREFL_VWC_MIN_TRACK_PTS),
            "-minvalperday", "5",
        ], cwd=_work_dir(hostname), env=env, timeout=300)
        _run(["vwc.exe", station, str(start_year), "-plt", "F"],
             cwd=_work_dir(hostname), env=env, timeout=300)
    except RuntimeError as exc:
        logger.info("gnssrefl pipeline: vwc not ready yet: %s", exc)

    vwc_txt = os.path.join(config.REFL_CODE, "Files", station, f"{station}_vwc.txt")
    points = []
    if os.path.exists(vwc_txt):
        with open(vwc_txt, encoding="utf-8") as f:
            for line in f:
                if line.startswith("%") or not line.strip():
                    continue
                parts = line.split()
                # FracYr Year DOY VWC Month Day
                if len(parts) < 6:
                    continue
                try:
                    year, doy, vwc, month, day = (
                        int(float(parts[1])), int(float(parts[2])), float(parts[3]),
                        int(float(parts[4])), int(float(parts[5])),
                    )
                except ValueError:
                    continue
                date_str = f"{year:04d}-{month:02d}-{day:02d}"
                points.append({"date": date_str, "date_display": date_str, "vwc_percent": round(vwc * 100, 2)})

    out_path = os.path.join(_soil_moisture_cache_dir(hostname), f"{station}_vwc.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "station": station,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "points": points,
        }, f, indent=2)
    logger.info("gnssrefl pipeline: soil moisture series now has %d point(s)", len(points))


def run_pipeline(hostname, force=False):
    """Runs the full pipeline synchronously for one station: convert any
    not-yet-processed (or still-fresh) days to RINEX/SNR/gnssir results,
    then refresh the soil-moisture series from the accumulated history.
    Meant to be called from a background thread (see app.py) since a
    multi-day backlog can take several minutes.

    A no-op (not an error) for a station with no gnssrefl_station
    configured in stations.py - Station-Overview-only machines have
    nothing for this pipeline to do."""
    station_record = stations.get_station(hostname)
    if not station_record or not station_record.get("gnssrefl_station"):
        return {"started": False, "reason": "no gnssrefl station configured"}

    run_lock = _run_locks.get(hostname)
    if run_lock is None or not run_lock.acquire(blocking=False):
        return {"started": False, "reason": "already running"}
    try:
        _set_status(hostname, True, "starting")
        station = station_record["gnssrefl_station"]
        station9 = stations.station9(station_record)
        rinex_version = station_record.get("rinex_version", "3.04")
        receiver = station_record.get("receiver", "u-blox/F9P")
        work_dir = _work_dir(hostname)
        os.makedirs(work_dir, exist_ok=True)

        _ensure_station_registered(
            hostname, station, station_record["lat"], station_record["lon"], station_record["height_m"],
            station_record.get("e1_deg", 5), station_record.get("e2_deg", 25))

        env = _gnssrefl_env()
        dates = _select_dates_to_process(hostname, _available_dates(hostname), force, env)
        processed, failed = [], []
        for date_str in dates:
            _set_status(hostname, True, f"processing {date_str}")
            try:
                _process_day(hostname, date_str, station, station9, rinex_version, receiver, work_dir)
                processed.append(date_str)
            except Exception as exc:
                logger.warning("gnssrefl pipeline: %s/%s failed: %s", hostname, date_str, exc)
                failed.append(date_str)

        _set_status(hostname, True, "updating soil moisture")
        try:
            _update_soil_moisture(hostname, station)
        except Exception as exc:
            logger.warning("gnssrefl pipeline: soil moisture update failed for %s: %s", hostname, exc)

        rinex_warning = rinex_storage_warning()
        if rinex_warning:
            logger.warning("gnssrefl pipeline: %s", rinex_warning)

        return {"started": True, "processed": processed, "failed": failed}
    finally:
        _set_status(hostname, False, "idle")
        run_lock.release()


def run_pipeline_async(hostname, force=False):
    if status(hostname)["running"]:
        return False
    thread = threading.Thread(target=run_pipeline, args=(hostname,), kwargs={"force": force}, daemon=True)
    thread.start()
    return True

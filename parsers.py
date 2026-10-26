# Turns the local rclone cache (see rclone_sync.py) into chart/table-ready
# data. Nothing here touches the network - it only reads files already
# synced to disk.

import csv
import glob
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import NamedTuple, Optional
from zoneinfo import ZoneInfo

import config
import stations
from rclone_sync import cache_dir_for, ground_truth_cache_dir

_DISPLAY_TZ = ZoneInfo(config.DISPLAY_TZ_NAME)

# journalctl --output=short-iso lines start with an ISO-8601 timestamp that
# includes an explicit UTC offset, e.g.
# "2026-07-23T00:44:12+12:00 gripi001 kernel: ...".
_LINE_TIMESTAMP = re.compile(r"^(\S+)")

# Raw data filenames start with the receiver-side rotation timestamp, e.g.
# "2026-07-22_12-00-00_GNSS_gripi001.7z", in UTC (confirmed against real
# synced files: their mtime, itself a real UTC-comparable instant, sits a
# clean ~file_rotate_time + a minute or two after this parsed as UTC - see
# get_data_size_series). Used to *compute* each file's expected upload
# time deterministically rather than trust the locally-synced copy's
# mtime; see that function's docstring for why.
_GNSS_FILENAME_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})")

# archive_and_clean.sh's systemd timer fires this long after each rotation
# boundary (see rtkbase/tools/copy_unit.sh: FILE_ROTATE_INTERVAL="*-*-*
# 00/${FILE_ROTATE_TIME}:01:02" - literally 1 minute 2 seconds past the
# hour), not a guess.
_ARCHIVE_LAG_SECONDS = 62

# (category label, severity for the chart marker/legend, pattern to match
# against a journal line's message). This is the supplementary,
# hand-curated half of alert detection - see _priority_info() below for the
# general half, which catches problems this list was never told about.
# Deliberately excludes routine/benign noise seen throughout normal
# operation: "Suppressed N messages from ..." rate-limit notices, sshd
# "Broken pipe"/"banner exchange" lines from Tailscale's own health probes,
# and "disabling MPPT charging" (the low-temp guard working as designed,
# not a fault). severity is deliberately just two tiers (critical/warning),
# matching the chart's red/yellow marker distinction - not the same axis as
# journald's own 5-level priority scale used below.
ALERT_PATTERNS = [
    ("service_failed", "critical", re.compile(r"Failed with result|Main process exited, code=exited, status=")),
    ("usb_disconnect", "critical", re.compile(r"USB disconnect|clear tt \d+.*error")),
    ("undervoltage", "warning", re.compile(r"Under[- ]?voltage", re.IGNORECASE)),
    ("python_exception", "critical", re.compile(r"Traceback \(most recent call last\)|NameError|ValueError|ConnectionError")),
    ("modem_problem", "critical", re.compile(r"Modem do not respond|Modem problem")),
]

# journald syslog priority levels (0=most severe) -> (category label,
# severity). Anything at "warning" (4) or worse is treated as alert-worthy
# on its own, with no keyword needed - this is what catches problems
# ALERT_PATTERNS was never told about. notice/info/debug (5-7) are left
# out; way too much routine stuff logs at those levels for that to be a
# useful signal on its own.
_PRIORITY_INFO = {
    0: ("emerg", "critical"),
    1: ("alert", "critical"),
    2: ("crit", "critical"),
    3: ("err", "critical"),
    4: ("warning", "warning"),
}

# Overrides the priority check above: known-benign lines that happen to log
# at "err" or worse anyway, so the general mechanism would otherwise flag
# them every single time. Confirmed against real production data
# (2026-07-29 export) - sshd-session logs "error: send_error: write:
# Broken pipe" at PRIORITY=3 (err) purely because Tailscale's own health
# probes open a TCP connection to port 22 and close it without completing
# a handshake; that's expected traffic, not a real SSH problem. Add to this
# list (not ALERT_PATTERNS) for anything else that turns out to be "reports
# as an error but isn't one" rather than genuinely not-severe.
IGNORE_PATTERNS = [
    re.compile(r"send_error: write: Broken pipe"),
    # Boot-time-only OS/hardware boilerplate, confirmed against real
    # production data (2026-08-16 export, and 8-9 older boots): each of
    # these logs at err/warning exactly once per boot, never repeats within
    # a boot, and has no bearing on GNSS/upload health - a Pi with no audio
    # or Bluetooth peripheral in use, package-shipped udev/service files
    # slightly out of sync with each other, and normal boot-time races
    # that self-resolve on retry (the messages say so themselves).
    re.compile(r"alsa-restore\.rules.*(has no effect any more|has no matching label)"),
    re.compile(r"hci_uart_bcm .*supply (vddio|vbat) not found, using dummy regulator"),
    re.compile(r"Permissions for .*00-network-manager-all\.yaml are too open"),
    re.compile(r"gpio-\d+ \(onewire@\d+\): enforced open drain"),
    re.compile(r"bluetooth\.service: ConfigurationDirectory .* already exists but the mode is different"),
    re.compile(r"gpsd\.service: Referenced but unset environment variable evaluates to an empty string: OPTIONS"),
    re.compile(r"nl80211: kernel reports: Registration to specific type not supported"),
    re.compile(r"no servers found in /etc/resolv\.conf, will retry"),
]

# A flat cap, applied *after* sorting newest-first - not "up to N per day
# within the requested range". On a quiet day this was loose enough to
# never matter, but a single unusually noisy day (many reboots in a row,
# each contributing its own boot-time udev/gpio noise plus whatever real
# issue prompted the reboots) can fill this on its own and silently hide
# every older day regardless of how large a "days" range was requested -
# confirmed against real data: 2026-08-10 alone produced enough
# alert-worthy lines to consume the old cap of 200, burying 2026-07-28
# through 2026-08-09 entirely. Raised well past anything one bad day could
# fill; the alerts table is a scrollable div, so a longer list doesn't
# break the page.
MAX_ALERTS = 2000


def _parse_ts(t):
    """Parse any of this project's timestamp formats into an aware UTC
    datetime, or None if t is empty/unparseable. Naive results (shouldn't
    happen given the formats we produce, but just in case) are assumed UTC."""
    if not t:
        return None
    try:
        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class Window(NamedTuple):
    """A [start, end) time range (UTC-aware datetimes). Every get_* function
    below takes "cutoff", which is either a plain datetime (lower bound only:
    "the last N days") or one of these (an explicit date range picked on the
    dashboard); end=None means open-ended."""
    start: datetime
    end: Optional[datetime]


def _in_window(parsed, cutoff):
    if cutoff is None:
        return True
    if isinstance(cutoff, Window):
        return parsed >= cutoff.start and (cutoff.end is None or parsed < cutoff.end)
    return parsed >= cutoff


def _after_cutoff(t, cutoff):
    if cutoff is None:
        return True
    parsed = _parse_ts(t)
    return parsed is not None and _in_window(parsed, cutoff)


def _to_display(t):
    """Render a timestamp (any of this project's formats) as a
    "YYYY-MM-DD HH:MM:SS" string in config.DISPLAY_TZ_NAME, so every
    source lines up on the station's local clock regardless of whether it
    was originally stored as UTC or local-with-offset. Charts use this
    string as a plain category-axis label (not a re-parsed Date), which
    sidesteps the browser's own timezone entirely - what's computed here
    server-side is exactly what gets displayed."""
    parsed = _parse_ts(t)
    if parsed is None:
        return t or ""
    return parsed.astimezone(_DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _priority_info(raw_priority):
    """Map a journald PRIORITY value to (category, severity), or None if
    it's not severe enough to be alert-worthy on priority alone."""
    try:
        priority = int(raw_priority)
    except (TypeError, ValueError):
        return None
    return _PRIORITY_INFO.get(priority)  # missing key (5-7, or out of range) -> None


def _epoch_micros_to_iso(raw_micros):
    """journalctl --output=json timestamps are microseconds since the Unix
    epoch (always UTC by definition - journald doesn't store a zone at all
    for these), as a string."""
    try:
        micros = int(raw_micros)
    except (TypeError, ValueError):
        return None
    dt = datetime.fromtimestamp(micros / 1_000_000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json_files(source, hostname):
    records = []
    for path in glob.glob(os.path.join(cache_dir_for(source, hostname), "*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                records.append(json.load(f))
        except (OSError, json.JSONDecodeError):
            continue  # skip partial/corrupt downloads rather than fail the whole page
    return records


def get_temperature_series(hostname, cutoff=None):
    # Prefer the dedicated DS18B20 wherever it actually reported (more
    # accurate - mounted on the battery itself, vs the MPPT controller's
    # own probe near the controller box) and fill in MPPT's
    # battery_temperature_c only for stretches with no thermal.json at
    # all, rather than an all-or-nothing choice for the whole chart. A
    # station whose sensor died on some date should show real readings up
    # to that date and MPPT readings after it, not lose the real history
    # just because the sensor isn't reporting today, and not lose the
    # MPPT fallback just because a few old thermal.json stragglers happen
    # to fall inside whatever range is currently being viewed (both were
    # real bugs here - the former from a naive "any thermal record ->
    # never fall back" check, the latter from that same check keying off
    # the chart's own display cutoff instead of the sensor's actual
    # coverage).
    thermal_points = [
        {"t": r["timestamp_utc"], "t_display": _to_display(r["timestamp_utc"]), "temperature_c": r["temperature_c"]}
        for r in _read_json_files("thermal", hostname)
        if "timestamp_utc" in r and "temperature_c" in r and _after_cutoff(r["timestamp_utc"], cutoff)
    ]
    # ISO-8601 UTC strings ("YYYY-MM-DDTHH:MM:SSZ") sort lexicographically
    # the same as chronologically, so plain string min/max and comparison
    # below are enough - no need to re-parse what _after_cutoff already
    # validated as parseable.
    thermal_start = min((p["t"] for p in thermal_points), default=None)
    thermal_end = max((p["t"] for p in thermal_points), default=None)
    mppt_points = [
        {
            "t": r["timestamp_utc"],
            "t_display": _to_display(r["timestamp_utc"]),
            "temperature_c": r["battery_temperature_c"],
        }
        for r in _read_json_files("mppt", hostname)
        if "timestamp_utc" in r
        and "battery_temperature_c" in r
        and _after_cutoff(r["timestamp_utc"], cutoff)
        and (thermal_start is None or not (thermal_start <= r["timestamp_utc"] <= thermal_end))
    ]
    points = thermal_points + mppt_points
    points.sort(key=lambda p: p["t"])
    return points


def get_voltage_series(hostname, cutoff=None):
    records = _read_json_files("mppt", hostname)
    points = [
        {
            "t": r["timestamp_utc"],
            "t_display": _to_display(r["timestamp_utc"]),
            "pv_voltage_v": r.get("pv_voltage_v"),
            "battery_voltage_v": r.get("battery_voltage_v"),
            "load_voltage_v": r.get("load_voltage_v"),
            # measured by the controller itself (register 0x310E), not
            # recomputed from V*I here - the controller samples both
            # synchronously so its own product is the more faithful figure
            "load_power_w": r.get("load_power_w"),
        }
        for r in records
        if "timestamp_utc" in r and _after_cutoff(r["timestamp_utc"], cutoff)
    ]
    points.sort(key=lambda p: p["t"])
    return points


def _next_rotation_boundary(window_start, rotate_hours):
    """The smallest UTC instant strictly after window_start that's aligned
    to this station's rotation grid (00:00, rotate_hours:00, 2x, ... each
    UTC day, resetting at midnight - matching rtkbase_archive.timer's own
    "00/${FILE_ROTATE_TIME}:01:02" systemd calendar expression).

    A window that starts exactly on the grid (a normal, complete
    recording window) lands on the boundary rotate_hours later - the
    grid's own next slot. A window that starts off-grid (a stub: e.g. a
    mid-window USB reconnect forced a new file to start early, at
    "02-11-56" rather than "02-00-00") isn't waiting a full rotate_hours
    to close - str2str's own rotation is on the same fixed grid regardless
    of when the current file started, so it closes (and gets archived)
    at the very next boundary, which can be less than rotate_hours away.
    Both cases are this same "next boundary" rule - no need to branch on
    which case a given file is.
    """
    day_start = window_start.replace(hour=0, minute=0, second=0, microsecond=0)
    hours_since_midnight = (window_start - day_start).total_seconds() / 3600
    next_slot = int(hours_since_midnight // rotate_hours) + 1
    return day_start + timedelta(hours=next_slot * rotate_hours)


def get_data_size_series(hostname, cutoff=None):
    """Each point's x-position is *computed*, not read off the local
    file's mtime: the next rotation boundary after the filename's own
    window-start timestamp (see _next_rotation_boundary), plus
    archive_and_clean.sh's fixed processing lag past that boundary (see
    _ARCHIVE_LAG_SECONDS) - i.e. deterministically "when this file should
    have been archived and uploaded", per this station's configured
    upload_rotate_hours (see stations.py).

    mtime was tried first and seemed reasonable (it's genuinely when the
    file was compressed and uploaded, in the common case) but turned out
    unreliable in a way no amount of heuristics fixed cleanly: rclone only
    preserves the source's mtime through a copy in the *steady*
    incremental case, where each file is downloaded roughly when it's
    uploaded. A bulk catch-up sync (a station that hadn't been synced in a
    while, or a newly added one, pulling days of backlog in one rclone
    run) can leave several files' local mtimes clustered within the same
    handful of seconds regardless of which real window each one covers -
    and a cluster-size heuristic to detect that ("a real single
    archive_and_clean.sh run only uploads a couple of files at once") has
    a real false-negative: a *small* batch of 2-3 files can just as easily
    be an accidental sync-timing coincidence as the deterministic
    computation here has no such failure mode at all - it never looks at
    local sync timing, so it can't be fooled by it.
    """
    station = stations.get_station(hostname)
    rotate_hours = (station or {}).get("upload_rotate_hours", 2)

    points = []
    for path in glob.glob(os.path.join(cache_dir_for("gnss", hostname), "*.7z")):
        name = os.path.basename(path)
        match = _GNSS_FILENAME_TIMESTAMP.match(name)
        if not match:
            continue  # no reliable timestamp to plot this file at
        try:
            size_bytes = os.path.getsize(path)
        except OSError:
            continue
        window_start = datetime(
            int(match.group(1)[0:4]), int(match.group(1)[5:7]), int(match.group(1)[8:10]),
            int(match.group(2)), int(match.group(3)), int(match.group(4)), tzinfo=timezone.utc,
        )
        uploaded_at = _next_rotation_boundary(window_start, rotate_hours) + timedelta(seconds=_ARCHIVE_LAG_SECONDS)
        t = uploaded_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        if not _after_cutoff(t, cutoff):
            continue
        points.append({"t": t, "t_display": _to_display(t), "file": name, "size_bytes": size_bytes})
    points.sort(key=lambda p: p["t"])
    return points


def _match_keyword(message):
    """Returns (category, severity) for the first matching pattern, or None."""
    for label, severity, pattern in ALERT_PATTERNS:
        if pattern.search(message):
            return (label, severity)
    return None


def _is_ignored(message):
    return any(pattern.search(message) for pattern in IGNORE_PATTERNS)


def _alert_from_json_line(entry):
    """New-format line (journalctl --output=json, see archive_and_clean.sh
    STEP 2): has a real PRIORITY field, so this is where the general
    "unpredictable alert" detection actually happens. A keyword match still
    wins the category label when both apply, since it's more specific."""
    # `or ""`, not .get's default: journald can emit "MESSAGE": null, which
    # .get("MESSAGE", "") passes through as None and crashes the regex below.
    message = entry.get("MESSAGE") or ""
    if isinstance(message, list):
        # journald emits non-UTF8 payloads as raw byte arrays instead of a
        # string - rare, but don't let it crash the page.
        message = "".join(chr(b) for b in message)

    if not message or _is_ignored(message):
        return None  # empty (nothing to show) or known-benign even though it happens to log at err+

    info = _match_keyword(message) or _priority_info(entry.get("PRIORITY"))
    if info is None:
        return None  # notice/info/debug with no keyword match: not alert-worthy
    category, severity = info

    identifier = entry.get("SYSLOG_IDENTIFIER") or entry.get("_COMM") or ""
    t = _epoch_micros_to_iso(entry.get("__REALTIME_TIMESTAMP"))
    return {
        "t": t or "",
        "category": category,
        "severity": severity,
        "message": f"{identifier}: {message}" if identifier else message,
    }


def _alert_from_text_line(line):
    """Old-format line (plain journalctl --output=short-iso text, from
    before the json switch) - no PRIORITY available, so this can only ever
    catch what's in ALERT_PATTERNS. Kept so already-synced logs still work."""
    if _is_ignored(line):
        return None
    info = _match_keyword(line)
    if info is None:
        return None
    category, severity = info
    ts_match = _LINE_TIMESTAMP.match(line)
    return {
        "t": ts_match.group(1) if ts_match else "",
        "category": category,
        "severity": severity,
        "message": line,
    }


def get_load_power_stats(hostname, cutoff=None, bucket_seconds=3600):
    """Aggregate the station's 1-minute load power samples (mppt_power CSVs,
    lines of "timestamp_utc,watts") into per-hour avg/min/max. The 2-hourly
    point snapshots can't represent a load that jumps around; this can.
    Returns [] when no sample files have been synced yet, letting the chart
    fall back to the snapshot-based series."""
    buckets = {}
    for path in glob.glob(os.path.join(cache_dir_for("mppt_power", hostname), "*.csv")):
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in lines:
            parts = line.strip().split(",")
            if len(parts) != 2:
                continue
            t, raw_w = parts
            parsed = _parse_ts(t)
            if parsed is None:
                continue
            if not _in_window(parsed, cutoff):
                continue
            try:
                watts = float(raw_w)
            except ValueError:
                continue
            buckets.setdefault(int(parsed.timestamp()) // bucket_seconds, []).append(watts)

    stats = []
    for bucket, watts in sorted(buckets.items()):
        t = datetime.fromtimestamp(bucket * bucket_seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        stats.append({
            "t": t,
            "t_display": _to_display(t),
            "avg_w": round(sum(watts) / len(watts), 2),
            "min_w": min(watts),
            "max_w": max(watts),
            "samples": len(watts),
        })
    return stats


def _day_noon_iso(date_str):
    """Represents a whole-day gnssrefl summary as a single point at that
    day's local noon (config.DISPLAY_TZ_NAME), expressed as a UTC ISO
    string so it round-trips correctly through _to_display() later.
    Deliberately *local* noon, not UTC noon: for a station at UTC+12/13
    (Pacific/Auckland), "UTC noon" lands within an hour of *local
    midnight* once converted for display - the exact opposite of the
    "roughly mid-day" placement this is meant to give day-level points."""
    local_noon = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=12, tzinfo=_DISPLAY_TZ)
    return local_noon.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_gnss_quality_series(hostname, cutoff=None):
    """One point per successful gnssir arc (a single satellite rise or
    set - see gnssrefl_pipeline._process_results_file), across every
    processed day, flattened into one chronological list so the dashboard
    can plot reflector height as a scatter over time the way gnssrefl's
    own real-time demos do: colored by GNSS constellation, one dot per
    arc, rather than collapsed into a single per-day average."""
    points = []
    for r in _read_json_files("gnss_quality", hostname):
        date_str = r.get("date")
        if not date_str:
            continue
        orbit_source = r.get("orbit_source")
        for arc in r.get("arcs", []):
            t = arc.get("t")
            if not t or not _after_cutoff(t, cutoff):
                continue
            # orbit_source (the whole day's, not per-arc) rides along so
            # the chart can flag which stretches of the smoothed line
            # came from the GPS-only nav fallback rather than the full
            # multi-GNSS gbm orbit - see gnssrefl_pipeline.py's module
            # docstring on why that fallback happens at all.
            points.append({**arc, "t_display": _to_display(t), "orbit_source": orbit_source})
    points.sort(key=lambda p: p["t"])
    return points


def get_gnss_daily_arc_counts(hostname, cutoff=None):
    """Per-day arc totals backing the RH chart's background bars: how many
    of a day's raw candidate arcs gnssir actually kept (broken down by
    constellation, from the day's arc_counts) versus rejected during QC
    (a single total - a rejected arc has no RH value worth attributing to
    one constellation's color, just a count - see
    gnssrefl_pipeline._process_failqc_file). Day-level, unlike
    get_gnss_quality_series's per-arc points."""
    points = []
    for r in _read_json_files("gnss_quality", hostname):
        date_str = r.get("date")
        if not date_str:
            continue
        t = _day_noon_iso(date_str)
        if not _after_cutoff(t, cutoff):
            continue
        points.append({
            "date": date_str,
            "t": t,
            "t_display": _to_display(t),
            "kept": r.get("arc_counts", {}),
            "rejected_total": r.get("rejected_total", 0),
        })
    points.sort(key=lambda p: p["t"])
    return points


def get_gnss_rejected_arcs(hostname, cutoff=None):
    """Arcs gnssir's QC rejected (no usable periodogram peak), across every
    processed day in range - only what a sky plot needs to draw them as
    the "no good data here" counterpart to get_gnss_quality_series's kept
    arcs: constellation, satellite, azimuth and the elevation span the arc
    actually covered. See gnssrefl_pipeline._process_failqc_file for where
    "rejected_arcs" comes from.

    Days processed before "rejected_arcs" existed contribute nothing here -
    same "needs reprocessing to backfill" situation as arc_key/elevation
    elsewhere in this pipeline, not a real zero-rejection day.
    """
    arcs = []
    for r in _read_json_files("gnss_quality", hostname):
        date_str = r.get("date")
        if not date_str:
            continue
        if not _after_cutoff(_day_noon_iso(date_str), cutoff):
            continue
        for arc in r.get("rejected_arcs", []):
            if arc.get("azimuth_deg") is not None:
                arcs.append({**arc, "date": date_str})
    return arcs


def get_soil_moisture_series(hostname, cutoff=None):
    """Volumetric water content estimated by gnssrefl's vwc module from
    the same GPS L1/L2C reflectometry tracks as get_gnss_quality_series(),
    but leveled against the whole accumulated record rather than computed
    per-day - see gnssrefl_pipeline.py's _update_soil_moisture(). Returns
    [] until enough repeat-track history has built up for gnssrefl to
    trust a track (station is new; see GNSSREFL_VWC_MIN_TRACK_PTS), or if
    this station has no gnssrefl_station configured at all (see
    stations.py)."""
    station = stations.get_station(hostname)
    if not station or not station.get("gnssrefl_station"):
        return []
    path = os.path.join(cache_dir_for("soil_moisture", hostname), f"{station['gnssrefl_station']}_vwc.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    points = []
    for p in data.get("points", []):
        date_str = p.get("date")
        if not date_str:
            continue
        t = _day_noon_iso(date_str)
        if not _after_cutoff(t, cutoff):
            continue
        points.append({
            "t": t, "t_display": _to_display(t),
            "date": date_str, "vwc_percent": p.get("vwc_percent"),
        })
    points.sort(key=lambda pt: pt["t"])
    return points


def get_alerts(hostname, cutoff=None):
    alerts = []
    for path in glob.glob(os.path.join(cache_dir_for("journal", hostname), "*.log")):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue
        for raw_line in lines:
            line = raw_line.rstrip("\n")
            if not line:
                continue

            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                entry = None

            alert = _alert_from_json_line(entry) if entry is not None else _alert_from_text_line(line)
            if alert is None or not _after_cutoff(alert["t"], cutoff):
                continue
            alert["t_display"] = _to_display(alert["t"])
            alerts.append(alert)

    alerts.sort(key=lambda a: a["t"], reverse=True)
    return alerts[:MAX_ALERTS]


def get_latest_data_timestamp(hostname):
    """Most recent timestamp across every synced data source for this
    station, ignoring the page's own display cutoff entirely (calls each
    get_*_series with no cutoff). A chart that looks wrong or empty for
    the selected date range is often just rclone_sync not having pulled a
    recent upload yet, not a real display bug - shown next to "All times
    shown in ..." this lets that be read off directly (a stale timestamp
    here) instead of guessed at from the chart's shape. None if this
    station has never synced anything at all.
    """
    candidates = [
        p["t"]
        for series in (
            get_temperature_series(hostname),
            get_voltage_series(hostname),
            get_data_size_series(hostname),
            get_gnss_quality_series(hostname),
        )
        for p in series
    ]
    return _to_display(max(candidates)) if candidates else None


_TRACK_AZIMUTH_TOLERANCE_DEG = 3.0


def get_gnss_track_candidates(hostname, sat, freq, rise_set, azimuth_deg,
                                azimuth_tolerance_deg=_TRACK_AZIMUTH_TOLERANCE_DEG):
    """Every cached arc for this station that's plausibly "the same track"
    as (sat, freq, rise_set, azimuth_deg) - same satellite, same frequency
    (which already implies constellation - see gnssrefl_pipeline.
    _FREQ_SYSTEM - so system isn't a separate filter here), same
    rise-vs-set direction, azimuth within tolerance - across this
    station's *entire* processed history, not just the dashboard's
    current "days" range: picking a before/after-rain pair can mean going
    back weeks, and a repeating track's azimuth only drifts a degree or
    two day to day. rise_set may be None (an arc processed before
    gnssrefl_pipeline._process_results_file started capturing it) - in
    that case the direction filter is skipped entirely rather than
    excluding everything, since there's nothing to compare it against.

    Only arcs with a cached "arc_key" (see
    gnssrefl_pipeline._write_arc_details) are returned - no raw
    SNR/periodogram detail cached means nothing to actually overlay, so
    there's nothing useful to offer as a comparison candidate.
    "rain_event_delta_pct" (see get_rain_event_dates) rides along per
    candidate so the UI can flag likely before/after-rain dates inline,
    rather than needing a separate recommendation flow.
    """
    rain_events = get_rain_event_dates()
    candidates = []
    for r in _read_json_files("gnss_quality", hostname):
        date_str = r.get("date")
        if not date_str:
            continue
        for arc in r.get("arcs", []):
            if not arc.get("arc_key"):
                continue
            if arc.get("sat") != sat or arc.get("freq") != freq:
                continue
            if rise_set is not None and arc.get("rise_set") != rise_set:
                continue
            az = arc.get("azimuth_deg")
            if az is None or abs(((az - azimuth_deg + 180) % 360) - 180) > azimuth_tolerance_deg:
                continue
            candidates.append({
                "date": date_str,
                "arc_key": arc["arc_key"],
                "t": arc["t"],
                "t_display": _to_display(arc["t"]),
                "rh_m": arc.get("rh_m"),
                "azimuth_deg": az,
                "rain_event_delta_pct": rain_events.get(date_str),
            })
    candidates.sort(key=lambda c: c["t"])
    return candidates


# Day-over-day rise in TOMST ground-truth soil moisture (see
# get_ground_truth_soil_moisture) big enough to flag as a likely rain
# event - a loose, unvalidated threshold (there's no independent rainfall
# record to tune it against yet), meant only to annotate candidate dates
# in the track-comparison picker, not as a hard rain/no-rain classifier.
_RAIN_EVENT_MIN_DELTA_PCT = 3.0


def get_rain_event_dates():
    """Site-wide dates (YYYY-MM-DD) where the ground-truth sensor's daily
    average VWC rose by at least _RAIN_EVENT_MIN_DELTA_PCT percentage
    points versus the previous day - see get_gnss_track_candidates, the
    only current caller. {} if no ground-truth data (or no calibration
    file) has synced yet - see get_ground_truth_soil_moisture."""
    daily = {}
    for p in get_ground_truth_soil_moisture():
        vwc = p.get("vwc_percent")
        if vwc is None:
            continue
        daily.setdefault(p["t"][:10], []).append(vwc)
    daily_avg = sorted((date_str, sum(vs) / len(vs)) for date_str, vs in daily.items())
    events = {}
    for (_, prev_avg), (date_str, avg) in zip(daily_avg, daily_avg[1:]):
        delta = avg - prev_avg
        if delta >= _RAIN_EVENT_MIN_DELTA_PCT:
            events[date_str] = round(delta, 1)
    return events


# TOMST's TMS Calibr utility writes this as the first line of a plain-text
# calibration file, one per soil/site: a quadratic mapping from raw TMS
# moisture signal to volumetric water content (0-1). The file's own name
# isn't something to rely on (it's whatever the person who ran the
# calibration tool called it), just this line's content.
_GT_CALIBRATION_RE = re.compile(
    r"FA\s*=\s*(?P<fa>-?[\d.]+)\s*,\s*FB\s*=\s*(?P<fb>-?[\d.]+)\s*,\s*FC\s*=\s*(?P<fc>-?[\d.]+)"
)


def _ground_truth_calibration():
    """(a, b, c) quadratic coefficients (raw -> VWC 0-1), or None if no
    calibration file has synced yet - see _GT_CALIBRATION_RE."""
    for path in glob.glob(os.path.join(ground_truth_cache_dir(), "*.txt")):
        try:
            with open(path, encoding="utf-8") as f:
                first_line = f.readline()
        except OSError:
            continue
        m = _GT_CALIBRATION_RE.search(first_line)
        if m:
            return float(m["fa"]), float(m["fb"]), float(m["fc"])
    return None


def get_ground_truth_soil_moisture(cutoff=None):
    """Real (not gnssrefl-estimated) soil moisture from a TOMST TMS-4
    datalogger physically installed at the site - lets the RH chart show
    actual ground truth alongside (or instead of) gnssrefl's own
    reflectometry-derived VWC estimate (see get_soil_moisture_series).
    Optional: [] if no TMS data has been uploaded to the GNSS_IR_GT Drive
    folder for this deployment.

    vwc_percent here is only the *uncalibrated-for-temperature* quadratic
    estimate (vwc = a*raw^2 + b*raw + c, using the site's own FA/FB/FC) -
    TOMST's full calibration also corrects the raw signal against the
    probe's own soil-temperature reading before this step, since water's
    dielectric constant shifts with temperature (see myClim's
    mc_calc_vwc, https://rdrr.io/cran/myClim/src/R/calc.R). That
    correction needs additional constants (a reference temperature plus
    per-generation and per-device drift/calibration factors) that aren't
    in the FA/FB/FC file alone, so vwc_percent is missing here entirely -
    absent, not a wrong value - until those are available. Good enough to
    see the shape of moisture change over time; not a precise absolute
    percentage yet.
    """
    calibration = _ground_truth_calibration()
    points = []
    for path in glob.glob(os.path.join(ground_truth_cache_dir(), "data_*.csv")):
        try:
            f = open(path, encoding="utf-8", newline="")
        except OSError:
            continue
        with f:
            for row in csv.reader(f, delimiter=";"):
                # Columns (TOMST TMS-4 manual): index; date/time (UTC);
                # time zone config; T1 (soil, bottom); T2 (surface,
                # middle); T3 (air, top); raw moisture count; a fixed
                # legacy value (always 202 on this device - "developed
                # for a previous generation of sensors and is no longer
                # relevant" per TOMST's own docs); errFlag (1 = the
                # device couldn't read its own clock chip, so the
                # timestamp on this row isn't trustworthy).
                if len(row) < 9:
                    continue
                try:
                    dt = datetime.strptime(row[1].strip(), "%Y.%m.%d %H:%M").replace(tzinfo=timezone.utc)
                    soil_temp_c = float(row[3])
                    surface_temp_c = float(row[4])
                    air_temp_c = float(row[5])
                    raw = float(row[6])
                    err_flag = int(row[8])
                except (ValueError, IndexError):
                    continue  # header row, blank line, or a field TOMST's export didn't fill in
                if err_flag:
                    continue
                t = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                if not _after_cutoff(t, cutoff):
                    continue
                point = {
                    "t": t, "t_display": _to_display(t),
                    "soil_temp_c": soil_temp_c, "surface_temp_c": surface_temp_c, "air_temp_c": air_temp_c,
                    "moisture_raw": raw,
                }
                if calibration is not None:
                    a, b, c = calibration
                    vwc = a * raw * raw + b * raw + c
                    point["vwc_percent"] = round(min(max(vwc, 0.0), 1.0) * 100, 2)
                points.append(point)

    points.sort(key=lambda p: p["t"])
    return points

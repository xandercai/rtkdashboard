# RTKBase field station dashboard: temperature, MPPT voltages, GNSS raw
# data volume, and journal alerts pulled from the station's Google Drive
# uploads via rclone. See rclone_sync.py / parsers.py for the actual work;
# this file is just routes + rendering.

import logging
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import Flask, abort, flash, redirect, render_template, request, send_from_directory, url_for

import config
import gnssrefl_pipeline
import parsers
import rclone_sync
import stations
from rclone_sync import cache_dir_for

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.secret_key = "rtkdashboard-local-only"  # no auth/sessions of real consequence; local tool only


def _requested_days():
    """Read "days" from the query string (GET, e.g. the range picker) or
    the form body (POST, e.g. the refresh button's hidden field), falling
    back to the configured default. Silently ignores garbage input rather
    than 500ing on a bad URL."""
    raw = request.values.get("days")
    if raw is None:
        return config.DEFAULT_HISTORY_DAYS
    try:
        days = int(raw)
    except ValueError:
        return config.DEFAULT_HISTORY_DAYS
    return days if days > 0 else config.DEFAULT_HISTORY_DAYS


def _requested_hostname():
    """Read "station" from the query string or form body (same dual-source
    pattern as _requested_days), falling back to the first registered
    station. A hostname not in stations.py is a real error (a bad/stale
    link), unlike a garbage "days" value - 404 rather than silently
    falling back, so it's obvious rather than quietly showing the wrong
    station's data."""
    raw = request.values.get("station")
    if raw is None:
        return stations.default_hostname()
    if stations.get_station(raw) is None:
        abort(404, description=f"Unknown station {raw!r}")
    return raw


def _parse_local_date(raw):
    """"YYYY-MM-DD" -> that calendar day's local midnight (config.DISPLAY_TZ_NAME)
    as a UTC-aware datetime, or None for empty/garbage input."""
    if not raw:
        return None
    try:
        d = datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None
    return d.replace(tzinfo=ZoneInfo(config.DISPLAY_TZ_NAME)).astimezone(timezone.utc)


def _requested_range():
    """What time range the page should show: either an explicit start/end
    date (query string or form body, local calendar days, both inclusive) or,
    if neither is given, the last N days. Returns (days, start_str, end_str,
    cutoff) where start_str/end_str are the validated "YYYY-MM-DD" strings
    ("" when not in use) for redisplaying/forwarding, and cutoff is what the
    parsers.get_* functions take (a datetime, a parsers.Window, or - never
    here - None). An end before the start is swapped rather than rejected."""
    days = _requested_days()
    start = _parse_local_date(request.values.get("start"))
    end = _parse_local_date(request.values.get("end"))
    if start is None and end is None:
        return days, "", "", datetime.now(timezone.utc) - timedelta(days=days)
    tz = ZoneInfo(config.DISPLAY_TZ_NAME)
    if start is None:
        start = end - timedelta(days=days - 1)
    if end is not None and end < start:
        start, end = end, start
    start_str = start.astimezone(tz).strftime("%Y-%m-%d")
    end_str = end.astimezone(tz).strftime("%Y-%m-%d") if end is not None else ""
    # "end" is an inclusive calendar day, so the exclusive bound is the next
    # local midnight (computed on the local date, not +24h, to survive DST).
    end_excl = None
    if end is not None:
        end_local = end.astimezone(tz).date() + timedelta(days=1)
        end_excl = datetime(end_local.year, end_local.month, end_local.day, tzinfo=tz).astimezone(timezone.utc)
    return days, start_str, end_str, parsers.Window(start, end_excl)


def _range_args(start_str, end_str, days):
    """Query args that reproduce the current range in a link/redirect."""
    if start_str or end_str:
        return {k: v for k, v in (("start", start_str), ("end", end_str), ("days", days)) if v}
    return {"days": days}


@app.route("/")
def dashboard():
    hostname = _requested_hostname()
    # A plain elapsed-time cutoff (now - N days) is timezone-invariant - it's
    # the same instant no matter what zone you compute "now" in. What
    # actually needed fixing was display (see parsers._to_display), not this.
    days, start_str, end_str, cutoff = _requested_range()

    voltage_series = parsers.get_voltage_series(hostname, cutoff)
    power_stats = parsers.get_load_power_stats(hostname, cutoff)
    if power_stats:
        # Once the station's minute-level CSV samples exist, the register
        # point-snapshot power is abandoned entirely (stripped server-side,
        # not just hidden by the template) - a 2-hourly instantaneous sample
        # of a jumpy load adds nothing next to real avg/min/max and the two
        # must never appear together.
        for p in voltage_series:
            p.pop("load_power_w", None)

    temperature_series = parsers.get_temperature_series(hostname, cutoff)
    data_size_series = parsers.get_data_size_series(hostname, cutoff)
    alerts = parsers.get_alerts(hostname, cutoff)

    gnss_quality_series = parsers.get_gnss_quality_series(hostname, cutoff)
    # Header-line stat for the GNSS Signal Quality card (arcs by
    # constellation, distinct satellites) - mirrors the "tracked sats /
    # arcs" summary gnssrefl's own real-time demo shows above its RH plot.
    gnss_summary = {"total_arcs": len(gnss_quality_series), "by_system": {}}
    satellites = set()
    for p in gnss_quality_series:
        gnss_summary["by_system"][p["system"]] = gnss_summary["by_system"].get(p["system"], 0) + 1
        satellites.add((p["system"], p["sat"]))
    gnss_summary["satellite_count"] = len(satellites)

    return render_template(
        "dashboard.html",
        hostname=hostname,
        all_stations=stations.list_stations(),
        display_tz=config.DISPLAY_TZ_NAME,
        days=days,
        range_start=start_str,
        range_end=end_str,
        temperature_series=temperature_series,
        voltage_series=voltage_series,
        data_size_series=data_size_series,
        power_stats=power_stats,
        alerts=alerts,
        gnss_quality_series=gnss_quality_series,
        gnss_summary=gnss_summary,
        gnss_daily_arc_counts=parsers.get_gnss_daily_arc_counts(hostname, cutoff),
        gnss_rejected_arcs=parsers.get_gnss_rejected_arcs(hostname, cutoff),
        gnssrefl_status=gnssrefl_pipeline.status(hostname),
        rinex_warning=gnssrefl_pipeline.rinex_storage_warning(),
        latest_data_t_display=parsers.get_latest_data_timestamp(hostname),
        # Site-wide, not per-station (see parsers.get_ground_truth_soil_moisture) -
        # every station's dashboard can show it, since it's the same physical sensor.
        ground_truth_series=parsers.get_ground_truth_soil_moisture(cutoff),
    )


@app.route("/refresh", methods=["POST"])
def refresh():
    hostname = _requested_hostname()
    days, start_str, end_str, _ = _requested_range()
    full = request.values.get("force") == "1"
    results = rclone_sync.sync_all(hostname, full=full)
    failures = {source: err for source, err in results.items() if err}
    if failures:
        flash(f"Sync had errors: {failures}", "error")
    elif full:
        flash("Full sync from Google Drive completed.", "success")
    else:
        flash("Synced successfully.", "success")
    return redirect(url_for("dashboard", station=hostname, **_range_args(start_str, end_str, days)))


@app.route("/refresh_gnssrefl", methods=["POST"])
def refresh_gnssrefl():
    hostname = _requested_hostname()
    days, start_str, end_str, _ = _requested_range()
    # Raw GNSS->RINEX conversion plus gnssrefl's rinex2snr/gnssir/vwc steps take
    # minutes for a multi-day backlog, so this runs in a background thread
    # rather than blocking the request like /refresh does - the dashboard
    # just re-reads whatever's in cache/gnss_quality and cache/soil_moisture
    # on its next load (see gnssrefl_status_json for polling).
    #
    # force=True bypasses the incremental "only reprocess if new files
    # arrived or gbm just became available" check (see
    # _select_dates_to_process) - needed any time the *output* a day
    # already has on disk is stale for a reason that logic can't see, e.g.
    # a field the code newly extracts from the same, unchanged results
    # file. Without this there'd be no self-service way to backfill a
    # schema change onto already-processed recent days.
    force = request.values.get("force") == "1"
    started = gnssrefl_pipeline.run_pipeline_async(hostname, force=force)
    if started:
        msg = "GNSS-IR analysis (forced full reprocess) started in the background" if force \
            else "GNSS-IR analysis started in the background"
        flash(f"{msg} - this can take a few minutes for a multi-day backlog.", "success")
    else:
        flash("GNSS-IR analysis is already running.", "error")
    return redirect(url_for("dashboard", station=hostname, **_range_args(start_str, end_str, days)))


@app.route("/gnssrefl_status")
def gnssrefl_status_json():
    return gnssrefl_pipeline.status(_requested_hostname())


@app.route("/compare")
def compare():
    """Overlays two stations' GNSS-IR reflector-height series on one shared
    time axis (see templates/compare.html) - a separate page rather than a
    mode of the main dashboard, since it's a fundamentally different view
    (two stations at once, no Station Overview half) opened from the main
    page's "Compare stations" picker."""
    hostname_a = request.args.get("a")
    hostname_b = request.args.get("b")
    if not hostname_a or not hostname_b:
        abort(400, description="Both ?a= and ?b= station hostnames are required")
    if stations.get_station(hostname_a) is None:
        abort(404, description=f"Unknown station {hostname_a!r}")
    if stations.get_station(hostname_b) is None:
        abort(404, description=f"Unknown station {hostname_b!r}")

    days, start_str, end_str, cutoff = _requested_range()

    return render_template(
        "compare.html",
        hostname_a=hostname_a,
        hostname_b=hostname_b,
        display_tz=config.DISPLAY_TZ_NAME,
        days=days,
        range_args=_range_args(start_str, end_str, days),
        gnss_quality_series_a=parsers.get_gnss_quality_series(hostname_a, cutoff),
        gnss_quality_series_b=parsers.get_gnss_quality_series(hostname_b, cutoff),
        latest_data_t_display_a=parsers.get_latest_data_timestamp(hostname_a),
        latest_data_t_display_b=parsers.get_latest_data_timestamp(hostname_b),
        # Site-wide, not per-station (see parsers.get_ground_truth_soil_moisture) -
        # one shared line, not "A's ground truth" vs "B's" (same physical sensor).
        ground_truth_series=parsers.get_ground_truth_soil_moisture(cutoff),
    )


@app.route("/gnss_track_candidates")
def gnss_track_candidates():
    """Candidate arcs to overlay against a point already open in the arc-
    detail modal (see templates/dashboard.html's "compare with..." panel):
    same satellite/frequency/direction, azimuth within tolerance, on
    *this* request's station - which can be a different station than the
    one the original point came from (see _requested_hostname; the
    "station" query param, same as everywhere else), since comparing two
    antennas at the same physical site is exactly as valid a use case as
    comparing two dates on the same antenna."""
    hostname = _requested_hostname()
    try:
        sat = int(request.args["sat"])
        freq = int(request.args["freq"])
        azimuth = float(request.args["azimuth"])
    except (KeyError, ValueError):
        abort(400, description="sat, freq and azimuth query params are required")
    raw_rise_set = request.args.get("rise_set", "")
    rise_set = int(raw_rise_set) if raw_rise_set else None
    return parsers.get_gnss_track_candidates(hostname, sat, freq, rise_set, azimuth)


@app.route("/gnss_arc_detail/<date>/<key>")
def gnss_arc_detail(date, key):
    # One point on the GNSS Signal Quality chart clicked -> the raw
    # SNR-vs-elevation curve and Lomb-Scargle periodogram behind that
    # arc's RH value, precomputed by gnssrefl_arc_detail.py alongside the
    # day's other results (see gnssrefl_pipeline._write_arc_details).
    # send_from_directory 404s on a missing file (and rejects path
    # traversal in date/key via its own safe_join) - that just means "no
    # cached detail for this point" (e.g. an older day not yet
    # reprocessed with -savearcs), which the frontend already handles.
    hostname = _requested_hostname()
    directory = os.path.join(cache_dir_for("gnss_arc_detail", hostname), date)
    return send_from_directory(directory, f"{key}.json")


if __name__ == "__main__":
    # PORT env var lets a second instance run alongside the default one
    # (e.g. a test instance while the real dashboard stays on 5000).
    app.run(debug=True, port=int(os.environ.get("PORT", "5000")))

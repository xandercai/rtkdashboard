# Pulls new files from the four rtkbase Google Drive remotes into a local
# cache directory via `rclone copy`. Parsers (parsers.py) only ever read
# from that local cache - nothing parses directly out of Drive.
#
# rclone must be installed and have a "gdrive" remote configured (the same
# remote rtkbase's archive_and_clean.sh uses on the station itself - copy
# that station's rclone.conf here, or point RTKDASH_RCLONE_CONF at it).

import logging
import os
import subprocess

import config

logger = logging.getLogger(__name__)

REMOTES = {
    "gnss": (config.GDRIVE_GNSS_REMOTE, "*_{hostname}.7z"),
    "journal": (config.GDRIVE_JOURNAL_REMOTE, "*_{hostname}_journal.log"),
    "mppt": (config.GDRIVE_MPPT_REMOTE, "*_{hostname}_mppt.json"),
    # 1-min load power series (see rtkbase tools/mppt_power_sample.sh);
    # same Drive folder as the snapshots, distinct filename suffix
    "mppt_power": (config.GDRIVE_MPPT_REMOTE, "*_{hostname}_mppt_power.csv"),
    "thermal": (config.GDRIVE_THERMAL_REMOTE, "*_{hostname}_thermal.json"),
}


def cache_dir_for(source, hostname):
    # Every source is partitioned by hostname, not just filtered by
    # filename - two stations processing/uploading for the same calendar
    # date would otherwise silently collide in a shared directory (this
    # bit gnss_quality's date-only filenames in particular, before
    # multi-station support existed).
    path = os.path.join(config.CACHE_DIR, source, hostname)
    os.makedirs(path, exist_ok=True)
    return path


def _run_rclone_copy(remote, local_dir, include_pattern, max_age_days, timeout):
    cmd = [
        "rclone", "copy", remote, local_dir,
        "--include", include_pattern,
        "--no-update-modtime",
        # --include/--max-age are applied client-side after listing the
        # remote directory - Drive's API has no server-side glob/mtime
        # filter rclone can push this down into, so every file in the
        # shared per-source Drive folder (all stations uploading into the
        # same "GNSS_IR_Data" etc. folder, distinguished only by filename)
        # gets listed regardless of whether it matches. --fast-list trades
        # memory for fewer, larger paginated listing requests, which is
        # rclone's own documented recommendation for exactly this shape of
        # problem on Drive - a flat folder with many thousands of files.
        "--fast-list",
        # Tighter than rclone's own defaults (3 / 10) - a run stuck retrying
        # a Drive API rate limit (see sync_all's docstring) should give up
        # and report the failure well before eating the whole timeout
        # budget, rather than silently retrying until the clock runs out.
        "--retries", "2",
        "--low-level-retries", "3",
    ]
    # None means a full sync - no age cutoff, pull/overwrite everything.
    if max_age_days is not None:
        # rclone's duration syntax wants a unit; hours give finer
        # granularity than "Nd" without changing the config's day-based unit.
        cmd += ["--max-age", f"{max_age_days * 24}h"]
    if config.RCLONE_CONF:
        cmd += ["--config", config.RCLONE_CONF]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"rclone copy {remote} failed: {result.stderr.strip()}")


def sync_all(hostname, full=False):
    """Sync all four remotes for one station into its cache subdirectory.
    Returns {source: error_or_None}. Scoped to a single hostname - called
    once per station (see app.py's /refresh, which syncs whichever station
    is currently selected) rather than looping over every registered
    station on every click, since that could mean dozens of stations'
    worth of rclone calls for a button the user pressed to refresh the one
    they're looking at.

    A routine sync (full=False) always pulls just config.SYNC_WINDOW_DAYS of
    recent history, independent of how many days the dashboard's chart
    happens to be showing right now - older files were already synced by an
    earlier call and don't change once uploaded, so widening the chart's
    date range shouldn't also widen (and slow down) the sync. full=True (the
    "Force full reprocess" checkbox, reused here) instead pulls every file
    ever uploaded with no age cutoff, backfilling/overwriting the whole
    local cache, and gets a much larger timeout budget to actually finish -
    both because it's a rare, explicitly-requested operation rather than
    something that has to stay snappy on every click.

    A slow/failing sync here isn't necessarily this machine's network - the
    "gdrive" remote (see rclone.conf) authenticates via rclone's own shared
    default OAuth client unless reconfigured with a personal Google Cloud
    client_id/client_secret, and that shared client's Drive API quota is
    pooled across every rclone user using the default, so it can trip
    rateLimitExceeded under normal-looking local conditions. --retries/
    --low-level-retries (see _run_rclone_copy) just make a rate-limited run
    fail fast instead of silently retrying through the whole timeout budget -
    they don't fix the underlying quota contention.
    """
    if full:
        max_age_days = None
        timeout = config.SYNC_FULL_TIMEOUT_SECONDS
    else:
        max_age_days = config.SYNC_WINDOW_DAYS
        timeout = config.SYNC_TIMEOUT_SECONDS

    results = {}
    for source, (remote, pattern_template) in REMOTES.items():
        local_dir = cache_dir_for(source, hostname)
        pattern = pattern_template.format(hostname=hostname)
        try:
            _run_rclone_copy(remote, local_dir, pattern, max_age_days, timeout)
            results[source] = None
        except Exception as exc:
            logger.warning("sync failed for %s (%s): %s", source, hostname, exc)
            results[source] = str(exc)
    return results

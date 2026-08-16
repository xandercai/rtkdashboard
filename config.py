# Central configuration. Override any of these with environment variables
# of the same name (see the os.environ.get() defaults below).

import os

from dotenv import load_dotenv

# Loads a .env file (see .env.example) from the current working directory
# if one exists, populating os.environ before anything below reads it -
# machine-specific settings (RTKDASH_GNSSREFL_PYTHON above all, which has
# no portable default at all) then live in one gitignored file instead of
# needing to be set at the OS/conda-environment level and remembered
# every time a new shell is opened. Real OS env vars still take priority
# where both are set (load_dotenv's default override=False) - .env is a
# convenience default, not a hard override.
load_dotenv()

# rclone remotes, matching the names used in rtkbase/archive_and_clean.sh.
GDRIVE_GNSS_REMOTE = "gdrive:GNSS_IR_Data"
GDRIVE_JOURNAL_REMOTE = "gdrive:GNSS_IR_JOURNAL"
GDRIVE_MPPT_REMOTE = "gdrive:GNSS_IR_MPPT"
GDRIVE_THERMAL_REMOTE = "gdrive:GNSS_IR_THERMAL"

# Path to the rclone config file holding the "gdrive" remote's credentials.
# Defaults to rclone's own default location; override if you copied the
# rtkbase station's rclone.conf somewhere else.
RCLONE_CONF = os.environ.get("RTKDASH_RCLONE_CONF", "")

# Local cache directory that rclone copies files into. Parsers read from
# here; nothing is parsed directly out of Drive.
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")

# How many days of history to sync/display by default (elapsed days back
# from right now, not calendar-day-aligned). Keeps the sync fast and the
# charts readable as data accumulates over months of unattended operation.
# Overridable per-request via the "days" query param on the dashboard page,
# or globally via this env var.
DEFAULT_HISTORY_DAYS = int(os.environ.get("RTKDASH_HISTORY_DAYS", "3"))

# --- Sync from Google Drive -----------------------------------------------
# A routine sync only needs to pull the last few days - anything older was
# already synced by a previous click and doesn't change once uploaded, so
# there's no reason to re-list/re-check however many days the chart happens
# to be showing (that coupling is what let a 30-day chart view drag the GNSS
# sync past its timeout: the shared Drive folder plus everything gnssrefl's
# fixed 2-hourly uploads had accumulated no longer fit in 300s). A few days
# of slack past the upload cycle is enough to catch a station back up after
# a brief outage without needing the full-history option below.
SYNC_WINDOW_DAYS = int(os.environ.get("RTKDASH_SYNC_WINDOW_DAYS", "5"))
SYNC_TIMEOUT_SECONDS = int(os.environ.get("RTKDASH_SYNC_TIMEOUT_SECONDS", "300"))

# Reuses the "Force full reprocess" checkbox: when checked, "Sync from
# Google Drive" instead pulls every file ever uploaded (no --max-age cutoff,
# so it also backfills/overwrites anything the local cache is missing or has
# stale) and gets a much longer timeout budget to actually finish that.
SYNC_FULL_TIMEOUT_SECONDS = int(os.environ.get("RTKDASH_SYNC_FULL_TIMEOUT_SECONDS", "3600"))

# The station is physically in New Zealand. All timestamps are stored as
# UTC (GNSS filenames, thermal/mppt JSON) or local-with-offset (journal
# lines from journalctl) - inconsistent to look at side by side. The
# dashboard converts everything to this zone for display so every chart
# and the alert table read on one consistent clock, regardless of what
# timezone the browser viewing it happens to be in.
DISPLAY_TZ_NAME = os.environ.get("RTKDASH_DISPLAY_TZ", "Pacific/Auckland")

# --- GNSS-IR (gnssrefl) reflectometry pipeline ---------------------------
# Turns the same raw .ubx files rclone already syncs (see rclone_sync.py's
# "gnss" remote) into signal-quality and soil-moisture estimates using
# gnssrefl (https://github.com/kristinemlarson/gnssrefl). See
# gnssrefl_pipeline.py for the actual UBX -> RINEX -> SNR -> gnssir/vwc work.
#
# Per-station identity (gnssrefl site code, lat/lon/height, RINEX version)
# lives in stations.py, not here - see its module docstring. Everything
# below is shared across every station.

# gnssrefl's own working directories (SNR files, orbit/nav files, daily
# results, VWC output). Matches the REFL_CODE/ORBITS/EXE environment
# variables gnssrefl itself expects - if those are already set machine-wide,
# this just mirrors them; otherwise it defaults to a sibling directory next
# to (but outside of) this repo, since it can grow to several GB and has no
# business being committed or synced.
REFL_CODE = os.environ.get(
    "REFL_CODE", os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "refl_data"))
ORBITS_DIR = os.environ.get("ORBITS", os.path.join(REFL_CODE, "orbits"))
EXE_DIR = os.environ.get("EXE", os.path.join(REFL_CODE, "exe"))

# python.exe of the "gnssrefl" conda env (see environment-gnssrefl.yml) -
# needed as an explicit path (rather than relying on PATH, as the rest of
# this module does for gnssrefl's own console-script exes) to run
# gnssrefl_arc_detail.py, a plain script rather than an installed entry
# point. It needs numpy/astropy for the Lomb-Scargle periodogram
# reconstruction, which is exactly why that work happens in this env via
# subprocess rather than in-process - the Flask app's own env stays light
# (see environment.yml's own comment on the same tradeoff for rclone). No
# portable default exists (the env lives wherever conda/mamba put it on
# each machine) - must be set via RTKDASH_GNSSREFL_PYTHON, e.g. the output
# of `conda run -n gnssrefl python -c "import sys; print(sys.executable)"`.
# Left unset, subprocess calls into it fail fast with a clear
# FileNotFoundError rather than silently doing nothing.
GNSSREFL_PYTHON = os.environ.get("RTKDASH_GNSSREFL_PYTHON", "")

# A day is reprocessed on every pipeline run as long as it's within this
# many days of today, even if it already has a result - both because it
# can still gain new 2-hourly uploads, and because the multi-GNSS "gbm"
# orbit gnssrefl_pipeline.py prefers (GLONASS/Galileo arcs, not just GPS)
# lags real time by ~2 days, so an early pass that fell back to GPS-only
# results should get a chance to be redone with the full multi-GNSS orbit
# once it's published. Anything older than this window is treated as
# final once processed.
GNSSREFL_REPROCESS_WINDOW_DAYS = int(os.environ.get("RTKDASH_GNSSREFL_REPROCESS_WINDOW_DAYS", "5"))

# Minimum number of same-track repeat points gnssrefl requires before it
# will trust a soil-moisture track (its own default is 30, tuned for
# established stations with a year+ of history). Lowered for a station
# that's only had a few days of uploads so far; raise back towards the
# gnssrefl default once enough history has accumulated for it to matter.
GNSSREFL_VWC_MIN_TRACK_PTS = int(os.environ.get("RTKDASH_GNSSREFL_VWC_MIN_TRACK_PTS", "5"))

# Per-station registry. Add an entry here for each rtkbase machine this
# dashboard should be able to show - the dashboard's station picker lists
# exactly what's in STATIONS below, nothing is auto-discovered.
#
# Only "hostname" is required (it's how all four rtkbase upload sources are
# distinguished in their uploaded filenames, e.g. "..._gripi001_thermal.json"
# - see rclone_sync.py). A station with no "gnssrefl_station" configured
# shows only the Station Overview half of the dashboard (whatever of
# journal/mppt/thermal it has uploaded); a station whose raw GNSS .7z
# uploads exist but has no journal/mppt/thermal data shows only the GNSS-IR
# half instead. Nothing here assumes every station has both.

STATIONS = [
    {
        "hostname": "gripi001",
        # 4-character (lowercase) gnssrefl site code - distinct from
        # hostname, which identifies the rtkbase machine rather than the
        # survey mark. Must be unique across all stations below (it's how
        # gnssrefl's own REFL_CODE tree keeps stations apart on disk).
        "gnssrefl_station": "wai1",
        # Antenna position, shared with WAI2 (same physical site, Waiau).
        # gnssrefl only uses lat/lon/height for the RINEX header and initial
        # satellite geometry, not for the reflectometry result itself (that
        # comes from the SNR interference pattern) - height here is an
        # approximate orthometric height (open-elevation.com SRTM lookup
        # for this lat/lon: 133.0m, verified 2026-08-10), not the antenna's
        # physical mounting height above ground.
        "lat": -42.663495,
        "lon": 173.041693,
        "height_m": 133.0,
        # Physical antenna mounting height above ground (m) - not currently
        # used by any gnssrefl parameter (reflector height is derived from
        # the SNR interferometry itself, not a config input); kept here
        # purely for reference.
        "antenna_height_m": 2.3,
        # "3.04" carries all four constellations (incl. BeiDou) in one mixed
        # obs file; "2.11" has no navigation-message slot for BeiDou at all
        # but is kept fully working as a fallback - see
        # gnssrefl_pipeline.py's module docstring.
        "rinex_version": "3.04",
        # Matches this station's rtkbase settings.conf "file_rotate_time"
        # (hours) - how often archive_and_clean.sh rotates and uploads a
        # new raw GNSS file. parsers.get_data_size_series() uses this to
        # *compute* each file's expected upload time from its filename
        # rather than trust the locally-synced copy's mtime, which a bulk
        # catch-up sync can leave meaningless (see that function's
        # docstring). Change this if a station's settings.conf ever uses a
        # different rotation period.
        "upload_rotate_hours": 2,
        # "e1_deg"/"e2_deg": gnssir's elevation mask (min/max degrees above
        # horizon) - optional, defaults to gnssrefl's own 5/25 (see
        # gnssrefl_pipeline._ensure_station_registered) if omitted, which
        # is what both stations below still use after an experiment
        # (2026-08-25) that widened it and made things *worse*, worth
        # recording here so it isn't retried blind:
        #
        # extract_arcs.py masks the raw elevation data to [e1, e2] *before*
        # an arc's observed start/end is ever measured ("e_mask = (arc_ele
        # > e1) & (arc_ele <= e2)"), and utils.pre_check_arc/
        # check_arc_quality then reject any arc whose *masked* start/end
        # isn't within ediff (2deg, unchanged default) of that *entire*
        # requested window - not "accept anything inside this range" but
        # "only accept a sweep that completes this whole range". Raising
        # e2 therefore raises the bar an arc has to clear (e.g. e2=25
        # needs the arc to reach >=23deg; e2=30 needs >=28deg) - it can
        # only ever shrink the accepted set, never grow it, since nothing
        # here observes above ~30deg regardless of what's requested (and
        # 0-45, tried first, asked for an unsatisfiable 0-43deg sweep -
        # 0 kept arcs across 27 days/15000 candidates). Testing whether
        # there's real signal above 25deg needs a separate side-by-side
        # run compared against this default, not a changed default -
        # gnssir's own QC design has no way to widen a range without
        # losing some of what the narrower one already had.
    },
    {
        "hostname": "gripi002",
        "gnssrefl_station": "wai2",
        "lat": -42.663495,
        "lon": 173.041693,
        "height_m": 133.0,
        "antenna_height_m": 1.9,
        "rinex_version": "3.04",
        "upload_rotate_hours": 2,
    },
    {
        "hostname": "gripi003",
        "gnssrefl_station": "ilam",
        # Provisional: convbin's own single-point position from a short
        # indoor (window-side) test recording on 2026-10-14 - good to tens
        # of metres, which is all gnssrefl needs, but replace with the
        # real antenna position once it's installed outdoors. Height is
        # ellipsoidal.
        "lat": -43.5225,
        "lon": 172.5835,
        "height_m": 36.0,
        "rinex_version": "3.04",
        "upload_rotate_hours": 2,
        # RINEX header receiver (convbin -hr) - optional, defaults to
        # "u-blox/F9P". The raw format itself (.rtcm3 here) is detected
        # from the archived file's extension, not configured - see
        # gnssrefl_pipeline._RAW_FORMATS.
        "receiver": "Unicore/UM980",
    },
]


def list_stations():
    return STATIONS


def get_station(hostname):
    for s in STATIONS:
        if s["hostname"] == hostname:
            return s
    return None


def default_hostname():
    return STATIONS[0]["hostname"]


def station9(station):
    """9-character IGS-style station name RINEX 3.04 mode needs (see
    gnssrefl_pipeline.py's _convert_to_rinex_v3: rinex2snr.py recognizes
    "this is RINEX 3" by a 9-char station name). Only used locally to
    name/find files gnssrefl never uploads anywhere, so it doesn't need to
    resolve to anything in a real archive - "00" is the conventional
    placeholder monument/receiver number, "nzl" the station's ISO country
    code. Override per-station with an explicit "gnssrefl_station9" key if
    that ever stops being good enough."""
    return station.get("gnssrefl_station9") or f"{station['gnssrefl_station']}00nzl"

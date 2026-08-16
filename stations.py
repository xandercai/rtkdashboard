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

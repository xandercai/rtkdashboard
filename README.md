# rtkdashboard

A Flask dashboard for one or more [rtkbase](https://github.com/xandercai/rtkbase)
GNSS base stations: station health (temperature, MPPT solar/battery
voltages, load power, raw data volume, system journal alerts) plus
GNSS-IR reflectometry results (signal quality, reflector height, soil
moisture) computed from the same raw observation files via
[gnssrefl](https://github.com/kristinemlarson/gnssrefl).

Nothing here talks to a station directly. `rtkbase`'s own
`archive_and_clean.sh` uploads each station's raw GNSS files (`.ubx` from a u-blox F9P, `.rtcm3` from a Unicore UM980) and small
JSON/log snapshots to Google Drive; this dashboard pulls them down with
`rclone` into a local cache and reads exclusively from that cache - so it
can run anywhere with network access to Drive, independent of the
stations themselves.

## Features

- **Station Overview** - temperature (dedicated DS18B20 sensor if wired,
  falling back to the MPPT controller's own `battery_temperature_c`
  otherwise), MPPT PV/battery/load voltages and load power, raw GNSS
  upload volume over time, and a journal-derived alert timeline.
- **GNSS Signal Quality (gnssrefl)** - reflector height per satellite
  arc, colored by constellation/frequency, with interactive
  elevation/azimuth/satellite filters, a smoothed trend line, and daily
  kept-vs-rejected arc counts.
- **Soil moisture (VWC)** - once enough repeat-track history has built up
  for a station, via gnssrefl's `vwc` module.
- **Compare** - overlay two stations' reflector-height series on one
  shared time axis.
- A station with no `journal`/`mppt`/`thermal` uploads shows only the
  GNSS-IR half; a station with no `gnssrefl_station` configured shows
  only the Station Overview half. Nothing assumes a station has both.

## Setup

Two separate conda environments, kept apart deliberately - `gnssrefl`
pulls in a heavy scientific stack (numpy/scipy/astropy/...) the Flask app
itself never imports; `app.py` only ever shells out to that env's
console-script executables.

```bash
mamba env create -f environment.yml            # "rtk": flask, rclone
mamba env create -f environment-gnssrefl.yml    # "gnssrefl": the GNSS-IR pipeline
```

`rclone` needs a `gdrive` remote configured, pointing at the same Google
Drive account/folders the target rtkbase station(s) upload to - see
[rclone's Google Drive docs](https://rclone.org/drive/). Using a personal
OAuth client (`rclone config update gdrive client_id=... client_secret=...`
+ `rclone config reconnect gdrive:`) rather than rclone's shared default
one is strongly recommended: the default's API quota is pooled across
every rclone user in the world and can trip rate limits under
normal-looking local conditions.

The GNSS-IR pipeline also needs, all invoked by bare filename so they
must be on `PATH`:

- [RTKLIB](https://rtklib.com/)'s `convbin` - UBX / RTCM3 -> RINEX conversion
- `7z` (7-Zip) - extracting the `.7z` archives rclone syncs
- `gzip` / `gunzip` - gnssrefl compresses/decompresses its own SNR and
  orbit files with these. **Windows gotcha**: Git for Windows' `gzip.exe`
  does not implement the classic Unix "behave as gunzip when invoked
  under that name" dispatch, so a naive `copy gzip.exe gunzip.exe`
  silently no-ops instead of decompressing - this fails *silently*
  (GLONASS/Galileo arcs quietly vanish; GPS-only broadcast nav still
  works since it never needs decompression). `tools/gunzip.cs` is a real
  ~20-line decompressor that fixes this:
  ```
  csc.exe /nologo /out:"<some-dir-on-PATH>\gunzip.exe" tools\gunzip.cs
  ```
  `csc.exe` ships with .NET Framework (typically
  `C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe`), no separate
  install needed. `%CONDA_PREFIX%\envs\gnssrefl\` is a convenient place
  for the output since it's already on `PATH` for the same reason
  gnssrefl's own executables are.

## Configuration

Everything is a plain environment variable, read once in `config.py` with
sensible defaults - see that file for the full list and reasoning behind
each default. Copy `.env.example` to `.env` and fill in what your machine
needs (`config.py` loads it automatically via `python-dotenv`) - this
keeps machine-specific settings in one gitignored file instead of having
to set OS/conda-environment variables by hand and remember to redo it in
every new shell. Real OS env vars still win if both are set. The ones
you're most likely to need:

| Variable | Purpose | Default |
|---|---|---|
| `RTKDASH_RCLONE_CONF` | path to the rclone config holding the `gdrive` remote | rclone's own default location |
| `RTKDASH_HISTORY_DAYS` | days of history to sync/display by default | `3` |
| `RTKDASH_DISPLAY_TZ` | IANA zone every chart/timestamp is rendered in | `Pacific/Auckland` |
| `REFL_CODE`, `ORBITS`, `EXE` | gnssrefl's own working directories | a `refl_data/` sibling next to this repo |
| `RTKDASH_GNSSREFL_KEEP_RINEX` | keep each processed day's RINEX obs/nav in `RTKDASH_RINEX_DIR/<hostname>/` instead of deleting it; the dashboard warns once that folder passes `RTKDASH_RINEX_WARN_GB` | off (`RINEX_DIR`: `REFL_CODE/rinex`, warning at `20` GB) |
| `RTKDASH_GNSSREFL_PYTHON` | `python.exe` of the `gnssrefl` conda env | **must be set** - no portable default (see setup above); e.g. `` `conda run -n gnssrefl python -c "import sys; print(sys.executable)"` `` |

## Adding a station

Add an entry to `STATIONS` in `stations.py`: `hostname` is required (it's
how each of the four rtkbase upload sources is distinguished in its
uploaded filenames); `gnssrefl_station` plus the antenna
position/RINEX-version fields are only needed if this station should get
the GNSS-IR half of the dashboard.

## Running

```bash
conda activate rtk
python app.py
```

Serves on `http://127.0.0.1:5000` (override with the `PORT` env var).
Development server only - see Flask's own docs for a production WSGI
deployment if this is ever exposed beyond localhost.

## License

MIT - see `LICENSE`.

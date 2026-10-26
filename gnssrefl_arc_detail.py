# Runs inside the "gnssrefl" conda env (see environment-gnssrefl.yml) -
# needs numpy/astropy, which the lightweight Flask env deliberately does
# not have (see config.GNSSREFL_PYTHON's comment). gnssrefl_pipeline.py
# invokes this as a subprocess right after gnssir.exe, once per processed
# day.
#
# Purpose: reconstruct, for each arc gnssir.exe just kept, the same two
# curves its own "-plt T" diagnostic plot would show - detrended SNR vs
# elevation angle, and the Lomb-Scargle periodogram (amplitude vs
# candidate reflector height) whose peak IS the arc's reported RH - and
# cache them as small per-arc JSON files. This is what backs the
# dashboard's "click a point on the RH chart" popup: gnssir.exe only ever
# writes the *peak* of the periodogram (one RH + one amplitude number) to
# its results file, not the full curve, so seeing the curve itself means
# recomputing it.
#
# Rather than re-deriving gnssrefl's LSP math, this reuses its own
# gps.strip_compute() - the exact function retrieve_rh.py itself calls -
# fed with the same (elevation, detrended SNR) data via gnssir's own
# -savearcs T output, so the reconstructed curve is byte-for-byte the same
# computation gnssir did internally, not an approximation of it.

import json
import os
import re
import sys

import numpy as np
from gnssrefl import gps as g
from gnssrefl.gnss_frequencies import get_file_suffix, get_scale_factor, get_wavelength

# Captures the constellation+band suffix (group 2, e.g. "G_L1") along with
# satellite/azimuth/time - this needs to be an exact match against
# get_file_suffix(freq), not just a nearby one. Two different frequencies
# for the same satellite pass (e.g. GLONASS L1 and L2 for the same rise)
# share the same azimuth and nearly the same timestamp, so without this,
# azimuth+time-only matching could silently return the wrong frequency's
# data - it did, in testing (L2's cf applied to L1's SNR data, producing a
# real-looking but wrong reflector height on the popup).
_ARC_FILENAME_RE = re.compile(r"^sat(\d{3})_([A-Za-z]_[A-Za-z0-9]+)_az(\d{3})_(\d{2})(\d{2})z\.txt$")

# Points to keep per curve after decimation. A single arc's raw file can
# have several thousand samples (elevation moves through the whole 5-25deg
# window at ~1Hz) and the periodogram has one point per desiredP (station
# json's precision setting, e.g. every 5mm of RH) - both far denser than a
# small popup chart needs or benefits from.
_MAX_POINTS = 400


def _decimate(*arrays):
    n = len(arrays[0])
    step = max(1, n // _MAX_POINTS)
    return [np.round(a[::step], 4).tolist() for a in arrays]


def _fit_multipath(elevation, dsnr, cf, rh_m):
    """Least-squares fit of the classic single-reflector multipath model
    dSNR = A*sin(2*pi*(RH/cf)*sin(e) + phi) with RH pinned to the
    periodogram's own peak (so the fit shows how well *that* RH explains
    the data, rather than re-deriving a new one). Linear in (a, b) =
    (A*cos(phi), A*sin(phi)), so a plain lstsq - no iteration. Returns
    (model, amplitude, phase_deg, r2); r2 is the fraction of dSNR variance
    the sinusoid explains (0 = no better than a flat line)."""
    arg = 2 * np.pi * (rh_m / cf) * np.sin(np.radians(elevation))
    basis = np.column_stack((np.sin(arg), np.cos(arg)))
    (a, b), *_ = np.linalg.lstsq(basis, dsnr, rcond=None)
    model = basis @ np.array([a, b])
    ss_tot = float(np.sum((dsnr - dsnr.mean()) ** 2))
    r2 = 1 - float(np.sum((dsnr - model) ** 2)) / ss_tot if ss_tot > 0 else 0.0
    return model, float(np.hypot(a, b)), float(np.degrees(np.arctan2(b, a))), r2


# First Fresnel zone on a horizontal reflector - same geometry as
# gnssrefl's own refl_zones.FresnelZone (Larson & Nievinski 2013,
# appendix). Reimplemented rather than imported: refl_zones pulls in
# wget/simplekml at import time, neither of which this needs.
_FRESNEL_ELLIPSE_POINTS = 48
_FRESNEL_ZONE_COUNT = 5


def _fresnel_geometry(wavelength, h, azimuth_deg, emin, emax):
    """Plan-view (east/north metres from the antenna) first-Fresnel-zone
    ellipses at _FRESNEL_ZONE_COUNT elevations spanning the arc's observed
    range, plus the specular reflection point's ground track across that
    range. An arc's azimuth drifts a few degrees while the satellite
    moves, but the SNR arc file has no per-sample azimuth - the single
    arc-level azimuth from the results file is used throughout, the same
    simplification gnssrefl's own zone maps make."""
    az = np.radians(azimuth_deg)
    delta = wavelength / 2
    t = np.linspace(0, 2 * np.pi, _FRESNEL_ELLIPSE_POINTS)

    def ellipse(e_deg):
        s = np.sin(np.radians(e_deg))
        b = np.sqrt(2 * delta * h / s + (delta / s) ** 2)
        a = b / s
        centre = (h + delta / s) / np.tan(np.radians(e_deg))
        u = centre + a * np.cos(t)  # along the satellite azimuth
        v = b * np.sin(t)           # perpendicular to it
        return {
            "elevation_deg": round(float(e_deg), 1),
            "east_m": np.round(u * np.sin(az) + v * np.cos(az), 2).tolist(),
            "north_m": np.round(u * np.cos(az) - v * np.sin(az), 2).tolist(),
        }

    track_e = np.linspace(emin, emax, 30)
    track_d = h / np.tan(np.radians(track_e))
    return {
        "ellipses": [ellipse(e) for e in np.linspace(emin, emax, _FRESNEL_ZONE_COUNT)],
        "specular_east_m": np.round(track_d * np.sin(az), 2).tolist(),
        "specular_north_m": np.round(track_d * np.cos(az), 2).tolist(),
    }


def _index_arc_files(arcs_dir):
    files = []
    for name in os.listdir(arcs_dir):
        m = _ARC_FILENAME_RE.match(name)
        if not m:
            continue
        sat, suffix, az, hh, mm = m.groups()
        files.append({
            "path": os.path.join(arcs_dir, name),
            "sat": int(sat), "suffix": suffix,
            "az": int(az), "minutes": int(hh) * 60 + int(mm),
        })
    return files


def _find_arc_file(files, sat, suffix, az, minutes):
    # Exact match required on sat + constellation/band suffix - see the
    # module docstring's note above _ARC_FILENAME_RE on why azimuth/time
    # proximity alone isn't enough to disambiguate frequencies.
    candidates = [f for f in files if f["sat"] == sat and f["suffix"] == suffix]
    if not candidates:
        return None
    best = min(candidates, key=lambda f: abs(f["az"] - az) * 4 + abs(f["minutes"] - minutes))
    # A real match should be close on both; anything else means this arc's
    # -savearcs file wasn't found (e.g. it got rejected differently between
    # runs) - skip it rather than serve the wrong arc's curve.
    if abs(best["az"] - az) > 5 or abs(best["minutes"] - minutes) > 10:
        return None
    return best


def main():
    station, year, doy, quality_json_path, output_dir = sys.argv[1:6]
    year, doy = int(year), int(doy)

    with open(quality_json_path, encoding="utf-8") as f:
        record = json.load(f)
    arcs = record.get("arcs", [])
    if not arcs:
        return

    arcs_dir = os.path.join(os.environ["REFL_CODE"], str(year), "arcs", station, f"{doy:03d}")
    if not os.path.isdir(arcs_dir):
        return
    files = _index_arc_files(arcs_dir)

    station_json_path = os.path.join(os.environ["REFL_CODE"], "input", station, f"{station}.json")
    with open(station_json_path, encoding="utf-8") as f:
        station_config = json.load(f)
    minH, maxH = station_config["minH"], station_config["maxH"]
    desiredP = station_config["desiredP"]
    lsp_method = station_config.get("lsp_method", "fast")

    os.makedirs(output_dir, exist_ok=True)

    for arc in arcs:
        freq, sat = arc.get("freq"), arc.get("sat")
        if freq is None or sat is None:
            continue
        hh, mm = int(arc["t"][11:13]), int(arc["t"][14:16])
        az = round(arc["azimuth_deg"])
        suffix = get_file_suffix(freq).lstrip("_")

        match = _find_arc_file(files, sat, suffix, az, hh * 60 + mm)
        if match is None:
            continue

        data = np.loadtxt(match["path"], comments="%")
        if data.ndim != 2 or len(data) < 5:
            continue
        elevation, dsnr = data[:, 0], data[:, 1]

        cf = get_scale_factor(freq, sat)
        maxF, maxAmp, eminObs, emaxObs, riseSet, px, pz = g.strip_compute(
            elevation, dsnr, cf, maxH, desiredP, minH, lsp_method)
        if len(px) == 0:
            continue

        model, fit_amp, fit_phase, fit_r2 = _fit_multipath(elevation, dsnr, cf, maxF)
        elevation_out, dsnr_out, model_out = _decimate(elevation, dsnr, model)
        rh_grid_out, amplitude_out = _decimate(px, pz)
        fresnel = _fresnel_geometry(
            get_wavelength(freq, sat), maxF, arc["azimuth_deg"], float(elevation.min()), float(elevation.max()))

        key = f"{sat}_{freq}_{az}_{hh:02d}{mm:02d}"
        arc["arc_key"] = key
        with open(os.path.join(output_dir, f"{key}.json"), "w", encoding="utf-8") as f:
            json.dump({
                "sat": sat, "system": arc["system"], "freq_label": arc["freq_label"],
                "rh_m": arc["rh_m"], "azimuth_deg": arc["azimuth_deg"],
                "elevation_deg": elevation_out, "dsnr": dsnr_out,
                "fit_dsnr": model_out, "fit_rh_m": round(float(maxF), 3),
                "fit_amplitude": round(fit_amp, 3), "fit_phase_deg": round(fit_phase, 1),
                "fit_r2": round(fit_r2, 3), "fresnel": fresnel,
                "periodogram_rh_m": rh_grid_out, "periodogram_amplitude": amplitude_out,
            }, f)

    with open(quality_json_path, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)


if __name__ == "__main__":
    main()

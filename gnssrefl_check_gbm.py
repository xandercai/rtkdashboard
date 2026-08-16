# Runs inside the "gnssrefl" conda env (needs gnssrefl's own gps module,
# which pulls in astropy at import time - see config.GNSSREFL_PYTHON).
#
# A cheap probe for whether GFZ's multi-GNSS rapid ("gbm") orbit has been
# published yet for a set of dates that previously had to fall back to
# the GPS-only broadcast nav orbit (see gnssrefl_pipeline.py's
# _run_rinex2snr_with_fallback). Used to decide which "nav" days are
# actually worth a full reprocess on a given pipeline run, instead of
# blindly redoing every day in the reprocess window every time - a full
# reprocess (RINEX conversion, rinex2snr, gnssir, arc detail) is minutes
# per day, while this is just an orbit lookup/download attempt.
#
# gnssrefl's own orbit functions print a lot of their own progress noise
# to stdout, so the result is written to a file (path given as the first
# argument) rather than printed, to keep it unambiguous to parse.

import json
import sys

from gnssrefl import gps as g


def main():
    output_path, *dates = sys.argv[1:]
    result = {}
    for date_str in dates:
        year, month, day = (int(x) for x in date_str.split("-"))
        try:
            foundit, _, _ = g.get_orbits_setexe(year, month, day, "gbm")
        except Exception:
            foundit = False
        result[date_str] = bool(foundit)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f)


if __name__ == "__main__":
    main()

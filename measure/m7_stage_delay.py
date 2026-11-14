"""M7: per-forecast-step publication delay on each source (Last-Modified of the step's .index file).

Answers: does any source publish the early forecast steps before the whole run is available?
"""

from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import email.utils
import statistics

import requests

from measure.common import CFG, guarded, md_table, now_utc, save
from measure.m1_sources import TIMEOUT, file_url

SOURCES = ["ecmwf", "google", "azure", "aws"]
STEPS_MAIN = [0, 24, 48, 72, 90, 120, 144, 180, 240]
STEPS_SHORT = [0, 24, 48, 72, 90, 120, 144]


def delay_h(source, run, step):
    try:
        url = file_url(source, run, step).replace(".grib2", ".index")
        r = requests.head(url, timeout=TIMEOUT, allow_redirects=True)
    except Exception:  # noqa: BLE001
        return None
    if r.status_code != 200 or "Last-Modified" not in r.headers:
        return None
    lm = email.utils.parsedate_to_datetime(r.headers["Last-Modified"]).replace(tzinfo=None)
    return (lm - run).total_seconds() / 3600


@guarded("m7", "M7 per-step publication delay")
def main():
    now = now_utc()
    base = now.replace(minute=0, second=0, microsecond=0, hour=(now.hour // 6) * 6)
    runs = [base - dt.timedelta(hours=6 * i) for i in range(1, 13)]   # portal keeps ~12 runs
    jobs = {}
    with cf.ThreadPoolExecutor(12) as ex:
        for src in SOURCES:
            for r in runs:
                for s in (STEPS_MAIN if r.hour in (0, 12) else STEPS_SHORT):
                    jobs[(src, r, s)] = ex.submit(delay_h, src, r, s)
    res = {k: f.result() for k, f in jobs.items()}
    rows, data = [], {}
    for src in SOURCES:
        for grp, hours, steps in (("00/12Z", (0, 12), STEPS_MAIN), ("06/18Z", (6, 18), STEPS_SHORT)):
            cells = []
            for s in steps:
                v = [res[(src, r, s)] for r in runs if r.hour in hours and res.get((src, r, s)) is not None]
                data[f"{src}_{grp}_{s}"] = v
                cells.append(f"{statistics.median(v):.2f} (n={len(v)})" if v else "-")
            rows.append([src, grp, *cells[:len(STEPS_SHORT)], *(cells[len(STEPS_SHORT):] or ["", ""])])
    md = ["## M7 per-step publication delay (measured, hours after nominal run time, median)\n",
          f"Measured at {now:%Y-%m-%d %H:%M} UTC over the 12 cycles before it. "
          "Value = Last-Modified of the step's .index file minus run time.\n",
          md_table(["source", "runs", *[f"+{s}h" for s in STEPS_MAIN]], rows)]
    save("m7", {"status": "PASS", "measured_at": now.isoformat(), "delays": data}, "\n".join(md))


if __name__ == "__main__":
    main()

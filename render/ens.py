"""AIFS-ENS plume charts: every member's 850 hPa and 2 m temperature at a point, one graph per run.

Data: ECMWF open data `aifs-ens/0p25/enfo` (control `cf` + 50 perturbed members `pf`, steps 0-360 h every 6 h,
measured 2026-11-24). Only the needed messages are downloaded with HTTP range requests from the step's index
(t 850 hPa and 2t: about 0.3 MB per member and field, about 30 MB per step, about 1.9 GB per run, measured).
"""

from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import json
import logging
import pathlib
import time

import eccodes
import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import requests  # noqa: E402

from . import encode  # noqa: E402

log = logging.getLogger(__name__)
TIMEOUT = 60
URLS = {"google": "https://storage.googleapis.com/ecmwf-open-data", "ecmwf": "https://data.ecmwf.int/forecasts"}


def is_ens(cfg: dict) -> bool:
    return cfg["data"].get("source") == "ens"


def steps(cfg: dict) -> list[int]:
    e = cfg["ens"]
    return list(range(0, e["max_step"] + 1, e["every_h"]))


def bilinear(values: np.ndarray, ni: int, nj: int, lat: float, lon: float, lat0: float = 90.0,
             lon0: float = 0.0, dlat: float | None = None, dlon: float | None = None) -> float:
    """Value at (lat, lon) on a global regular grid stored from (lat0, lon0) southwards (dlat > 0 steps south) and
    eastwards. The open-data grids start at 90N, 180W (measured 2026-11-25: assuming 0E put Seoul in the Atlantic)."""
    a = values.reshape(nj, ni)
    dy = dlat if dlat else 180.0 / (nj - 1)
    dx = dlon if dlon else 360.0 / ni
    y, x = (lat0 - lat) / dy, ((lon - lon0) % 360.0) / dx
    j0, i0 = int(np.floor(y)), int(np.floor(x))
    fy, fx = y - j0, x - i0
    i1 = (i0 + 1) % ni
    return float((1 - fy) * ((1 - fx) * a[j0, i0] + fx * a[j0, i1]) + fy * ((1 - fx) * a[j0 + 1, i0] + fx * a[j0 + 1, i1]))


class EnsSource:
    """Completeness check and point extraction for AIFS-ENS (same run_complete interface as sources.Sources)."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.e = cfg["ens"]
        self.order = self.e.get("source_order", ["google", "ecmwf"])
        self.session = requests.Session()

    def url(self, source: str, run: dt.datetime, step: int, typ: str, ext: str) -> str:
        return (f"{URLS[source]}/{run:%Y%m%d}/{run:%H}z/{self.cfg['data']['model']}/0p25/enfo/"
                f"{run:%Y%m%d%H}0000-{step}h-enfo-{typ}.{ext}")

    def run_complete(self, run: dt.datetime) -> str | None:
        if run.hour not in self.cfg["data"].get("run_hours", [0, 6, 12, 18]):
            return None
        for src in self.order:
            try:
                r = self.session.head(self.url(src, run, self.e["max_step"], "pf", "index"), timeout=TIMEOUT,
                                      allow_redirects=True)
                if r.status_code == 200:
                    return src
            except Exception as e:  # noqa: BLE001
                log.warning("HEAD %s %s: %r", src, f"{run:%Y%m%d%H}", e)
        return None

    def _wanted(self, msg: dict) -> str | None:
        for key, (param, level) in (("t850", ("t", "850")), ("t2m", ("2t", None))):
            if msg.get("param") == param and msg.get("levelist") == level:
                return key
        return None

    def point_step(self, run: dt.datetime, step: int) -> dict:
        """{field: {member: value (deg C)}} for one step; member 0 = control."""
        last = None
        for src in self.order:
            try:
                out: dict = {"t850": {}, "t2m": {}}
                for typ in ("cf", "pf"):
                    idx = self.session.get(self.url(src, run, step, typ, "index"), timeout=TIMEOUT)
                    idx.raise_for_status()
                    msgs = [json.loads(x) for x in idx.text.splitlines() if x.strip()]
                    for m in msgs:
                        key = self._wanted(m)
                        if key is None:
                            continue
                        o, n = m["_offset"], m["_length"]
                        r = self.session.get(self.url(src, run, step, typ, "grib2"), timeout=TIMEOUT,
                                             headers={"Range": f"bytes={o}-{o + n - 1}"})
                        if r.status_code != 206 or len(r.content) != n:
                            raise RuntimeError(f"range {r.status_code} {len(r.content)}/{n}")
                        h = eccodes.codes_new_from_message(r.content)
                        try:
                            g = {k: eccodes.codes_get(h, k) for k in (
                                "Ni", "Nj", "latitudeOfFirstGridPointInDegrees", "longitudeOfFirstGridPointInDegrees",
                                "jDirectionIncrementInDegrees", "iDirectionIncrementInDegrees", "jScansPositively")}
                            if g["jScansPositively"]:
                                raise RuntimeError("unexpected S->N grid")
                            vals = eccodes.codes_get_values(h)
                        finally:
                            eccodes.codes_release(h)
                        member = int(m.get("number", 0)) if typ == "pf" else 0
                        out[key][member] = bilinear(vals, g["Ni"], g["Nj"], self.e["lat"], self.e["lon"],
                                                    g["latitudeOfFirstGridPointInDegrees"],
                                                    g["longitudeOfFirstGridPointInDegrees"],
                                                    g["jDirectionIncrementInDegrees"],
                                                    g["iDirectionIncrementInDegrees"]) - 273.15
                return out
            except Exception as e:  # noqa: BLE001
                last = e
                log.warning("%s %s +%d: %r", src, f"{run:%Y%m%d%H}", step, e)
        raise RuntimeError(f"AIFS-ENS step {step} failed on every source: {last!r}")


def plume(cfg: dict, run: dt.datetime, series: dict) -> bytes:
    """Two panels (850 hPa T, 2 m T): every member grey, control blue, mean black, 10-90 % band."""
    e = cfg["ens"]
    st = sorted(series)
    kst = dt.timedelta(hours=cfg["style"]["kst_offset_h"])
    x = [run + dt.timedelta(hours=s) + kst for s in st]
    fig, axes = plt.subplots(2, 1, figsize=(10.56, 8.44), dpi=100, sharex=True)
    for ax, key, label in zip(axes, ("t850", "t2m"), ("850 hPa temperature (C)", "2 m temperature (C)")):
        members = sorted({m for s in st for m in series[s][key]})
        a = np.array([[series[s][key].get(m, np.nan) for s in st] for m in members])
        pf = a[[i for i, m in enumerate(members) if m != 0]]
        for row in pf:
            ax.plot(x, row, color="#9a9a9a", lw=0.5, alpha=0.6)
        ax.fill_between(x, np.nanpercentile(pf, 10, axis=0), np.nanpercentile(pf, 90, axis=0),
                        color="#7fb3e6", alpha=0.35, lw=0, label="10-90 %")
        if 0 in members:
            ax.plot(x, a[members.index(0)], color="#1f5fbf", lw=1.4, label="control")
        ax.plot(x, np.nanmean(pf, axis=0), color="k", lw=2.0, label="mean")
        for ref in e.get("ref_lines", {}).get(key, []):
            ax.axhline(ref, color="#e00000", lw=0.9, ls="--")
        ax.set_ylabel(label)
        ax.grid(alpha=0.3)
        ax.legend(loc="upper right", fontsize=8, ncol=3)
    axes[-1].xaxis.set_major_locator(mdates.DayLocator())
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    axes[-1].set_xlabel("valid time (KST, 00 KST ticks)")
    n = max(len(series[s]["t850"]) for s in st) - 1
    fig.suptitle(f"{e['title'].format(name=e['name'])}   {n} members + control\n"
                 f"ECMWF AIFS ENS 0.25deg   run {run:%Y-%m-%d %H}UTC ({run + kst:%m-%d %H}KST)", fontsize=11)
    fig.text(0.01, 0.005, cfg["style"]["credit"], fontsize=6.5, color="#555")
    fig.tight_layout(rect=(0, 0.015, 1, 1))
    import io
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def render_run(cfg: dict, src: EnsSource, run: dt.datetime, site: pathlib.Path, work: pathlib.Path,
               **_ignored) -> dict:
    """Download the point values of every step, draw the plume, write the image and meta.json (last)."""
    run_s = f"{run:%Y%m%d%H}"
    model = cfg["model_id"]
    rdir = site / model / run_s
    rdir.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    series: dict = {}
    with cf.ThreadPoolExecutor(cfg["ens"].get("parallel_steps", 6)) as ex:
        for s, vals in zip(steps(cfg), ex.map(lambda s: src.point_step(run, s), steps(cfg))):
            series[s] = vals
    dl = time.monotonic() - t0
    cid = cfg["site"]["charts"][0]["id"]
    out = cfg["output"]
    data = encode.encode(plume(cfg, run, series), out["format"], out["png8_colors"], out["webp_quality"])
    (rdir / f"{cid}_000.png").write_bytes(data)
    (rdir / "series.json").write_text(json.dumps({str(s): v for s, v in series.items()}))
    meta = {"model": model, "run": run_s, "steps": [0], "missing_steps": [], "wpsh": None,
            "render_version": cfg["site"]["render_version"],
            "rendered_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sources": sorted(src.order), "images": 1, "bytes": len(data),
            "download_seconds": round(dl, 1), "wall_seconds": round(time.monotonic() - t0, 1),
            "render_seconds_sum": 0.0, "point": [cfg["ens"]["lat"], cfg["ens"]["lon"]]}
    (rdir / "meta.json").write_text(json.dumps(meta, indent=1))
    return meta

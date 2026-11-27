"""Ensemble plume charts (AIFS-ENS, ECMWF ENS) at points, two graphs per point and run:
    plume_<point>  every member's 500 hPa height, 850 hPa and 2 m temperature ("기온")
    rain_<point>   every member's 850 / 925 hPa equivalent potential temperature and 6 h precipitation ("강수")

Both models carry t and q at 850 / 925 hPa and tp (accumulated from the run start) in every step file, measured
2026-11-26 in the 00Z indexes; tp units differ (AIFS-ENS kg m-2 = mm, IFS ENS m), so tp is converted by its GRIB
`units` key. The new fields add about 1.5 MB per member and step (measured message sizes), about doubling the
download (estimated: AIFS-ENS ~18 min, ENS ~25 min per run).

ECMWF ENS: `ifs/0p25/enfo`, one `ef` file per step with 50 perturbed members (no control at 0.25 deg), 3-hourly to
+144 h then 6-hourly to +360 h (00/12Z; 06/18Z to +144 h), measured 2026-11-25.
AIFS-ENS: ECMWF open data `aifs-ens/0p25/enfo` (control `cf` + 50 perturbed members `pf`, steps 0-360 h every 6 h,
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

from . import clim as clim_mod, config, derive, encode  # noqa: E402

log = logging.getLogger(__name__)
TIMEOUT = 60
G = 9.80665
KEYS = ("z500", "t850", "t2m", "t925", "q850", "q925", "tp")
# chart prefix -> (title key in [ens], panels (series key, axis label))
CHARTS = {
    "plume": ("title", (("z500", "500 hPa height (gpm)"), ("t850", "850 hPa temperature (C)"),
                        ("t2m", "2 m temperature (C)"))),
    "rain": ("title_rain", (("te850", "850 hPa theta-e (K)"), ("te925", "925 hPa theta-e (K)"),
                            ("pr6", "6 h precipitation (mm)"))),
}
PANELS = CHARTS["plume"][1]


def _tp_mm(v, units: str = "m"):
    if units == "m":                       # IFS ENS (measured)
        return v * 1000.0
    if units in ("kg m**-2", "kg m-2", "mm"):  # AIFS-ENS (measured)
        return v
    raise RuntimeError(f"tp in unexpected units {units!r}")


def derived(series: dict) -> dict:
    """series + te850 / te925 (K, Bolton) and pr6 (mm in the 6 h ending at the step, only at steps that are a
    multiple of 6 h with the step 6 h earlier present; small negative packing noise clipped to 0)."""
    out = {}
    for s, f in series.items():
        g = dict(f)
        for lev, tk in (("850", lambda m: f["t850"][m] + 273.15), ("925", lambda m: f["t925"][m])):
            q = f.get("q" + lev, {})
            if q and f.get("t" + lev):
                g["te" + lev] = {m: float(derive.theta_e(np.float64(tk(m)), np.float64(q[m]), float(lev)))
                                 for m in q if m in f["t" + lev]}
        prev = series.get(s - 6, {}).get("tp")
        if s % 6 == 0 and prev and f.get("tp"):
            g["pr6"] = {m: max(0.0, f["tp"][m] - prev[m]) for m in f["tp"] if m in prev}
        out[s] = g
    return out
URLS = {"google": "https://storage.googleapis.com/ecmwf-open-data", "ecmwf": "https://data.ecmwf.int/forecasts"}


def is_ens(cfg: dict) -> bool:
    return cfg["data"].get("source") == "ens"


def steps(cfg: dict, run_hour: int = 0) -> list[int]:
    """Steps of a run: every `every_h` to `switch_h`, then every `every_h_long` (IFS ENS: 3 h to +144 h, then 6 h);
    06/18Z runs stop at `max_step_short`."""
    e = cfg["ens"]
    last = config.ens_last_step(cfg, run_hour)
    sw = min(e.get("switch_h", last), last)
    out = list(range(0, sw + 1, e["every_h"]))
    long_every = e.get("every_h_long", e["every_h"])
    return out + [s for s in range(sw + long_every, last + 1, long_every)]


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
                r = self.session.head(self.url(src, run, steps(self.cfg, run.hour)[-1], self.e["files"][-1], "index"),
                                      timeout=TIMEOUT,
                                      allow_redirects=True)
                if r.status_code == 200:
                    return src
            except Exception as e:  # noqa: BLE001
                log.warning("HEAD %s %s: %r", src, f"{run:%Y%m%d%H}", e)
        return None

    # (key, GRIB param, level, value -> plotted unit). 500 hPa height: IFS ENS "gh" (gpm), AIFS-ENS "z"
    # (geopotential, m2 s-2), measured in the step indexes 2026-11-25.
    FIELDS = (("t850", "t", "850", lambda v, u="": v - 273.15), ("t2m", "2t", None, lambda v, u="": v - 273.15),
              ("z500", "gh", "500", lambda v, u="": v), ("z500", "z", "500", lambda v, u="": v / G),
              ("t925", "t", "925", lambda v, u="": v), ("q850", "q", "850", lambda v, u="": v),
              ("q925", "q", "925", lambda v, u="": v), ("tp", "tp", None, _tp_mm))

    def _wanted(self, msg: dict):
        for key, param, level, conv in self.FIELDS:
            if msg.get("param") == param and msg.get("levelist") == level:
                return key, conv
        return None

    def point_step(self, run: dt.datetime, step: int) -> dict:
        """{point id: {field: {member: value (deg C)}}} for one step; member 0 = control."""
        last = None
        pts = self.e["points"]
        for src in self.order:
            try:
                out: dict = {p["id"]: {k: {} for k in KEYS} for p in pts}
                for typ in self.e["files"]:          # AIFS-ENS: cf + pf files; IFS ENS: one "ef" file
                    idx = self.session.get(self.url(src, run, step, typ, "index"), timeout=TIMEOUT)
                    idx.raise_for_status()
                    msgs = [json.loads(x) for x in idx.text.splitlines() if x.strip()]
                    for m in msgs:
                        w = self._wanted(m)
                        if w is None:
                            continue
                        key, conv = w
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
                            units = eccodes.codes_get(h, "units")
                            vals = eccodes.codes_get_values(h)
                        finally:
                            eccodes.codes_release(h)
                        member = 0 if m.get("type") == "cf" else int(m.get("number", 0))
                        for pt in pts:
                            out[pt["id"]][key][member] = conv(bilinear(
                                vals, g["Ni"], g["Nj"], pt["lat"], pt["lon"], g["latitudeOfFirstGridPointInDegrees"],
                                g["longitudeOfFirstGridPointInDegrees"], g["jDirectionIncrementInDegrees"],
                                g["iDirectionIncrementInDegrees"]), units)
                return out
            except Exception as e:  # noqa: BLE001
                last = e
                log.warning("%s %s +%d: %r", src, f"{run:%Y%m%d%H}", step, e)
        raise RuntimeError(f"AIFS-ENS step {step} failed on every source: {last!r}")


def plume(cfg: dict, run: dt.datetime, series: dict, name: str, chart: str = "plume", clim: dict | None = None) -> bytes:
    """Three panels of one chart (CHARTS): every member grey, control blue, mean black, 10-90 % band.
    plume: 500 hPa height, 850 hPa T, 2 m T (series without z500, stored before 2026-11-25: the two temperature
    panels only); rain: 850 / 925 hPa theta-e and 6 h precipitation (series needs t925 / q / tp, see derived)."""
    e = cfg["ens"]
    title_key, all_panels = CHARTS[chart]
    st = sorted(series)
    kst = dt.timedelta(hours=cfg["style"]["kst_offset_h"])
    panels = [p for p in all_panels if any(series[s].get(p[0]) for s in st)]
    if not panels:
        raise ValueError(f"{chart}: no data for any panel")
    fig, axes = plt.subplots(len(panels), 1, figsize=(10.56, 4.0 + 2.6 * len(panels)), dpi=100, sharex=True,
                             squeeze=False)
    axes = axes[:, 0]
    xlim = (run + kst, run + dt.timedelta(hours=st[-1]) + kst)
    ccol = cfg.get("clim", {}).get("color", "#ff5a1f")
    clim_drawn = False
    for ax, (key, label) in zip(axes, panels):
        ks = [s for s in st if series[s].get(key)]            # pr6: 6-hourly steps only
        x = [run + dt.timedelta(hours=s) + kst for s in ks]
        members = sorted({m for s in ks for m in series[s][key]})
        a = np.array([[series[s][key].get(m, np.nan) for s in ks] for m in members])
        pf = a[[i for i, m in enumerate(members) if m != 0]]
        for row in pf:
            ax.plot(x, row, color="#9a9a9a", lw=0.5, alpha=0.6)
        ax.fill_between(x, np.nanpercentile(pf, 10, axis=0), np.nanpercentile(pf, 90, axis=0),
                        color="#7fb3e6", alpha=0.35, lw=0, label="10-90 %")
        if 0 in members:
            ax.plot(x, a[members.index(0)], color="#1f5fbf", lw=1.4, label="control")
        ax.plot(x, np.nanmean(pf, axis=0), color="k", lw=2.0, label="mean")
        if clim and key in ("z500", "t850"):              # ERA5 1990-2019 at the valid times
            cv = clim_mod.era5_at(clim, key, [run + dt.timedelta(hours=s) for s in ks])
            if cv:
                ax.plot(x, cv, color=ccol, lw=1.6, label="clim (ERA5 1990-2019)")
                clim_drawn = True
        if clim and key == "t2m":                          # KMA daily normal max / min, per KST day
            days = [x[0].replace(hour=0) + dt.timedelta(days=i) for i in range((x[-1] - x[0]).days + 2)]
            kv = clim_mod.kma_at(clim, days)
            if kv:
                mx, mn, tmst = kv
                xs = [d + dt.timedelta(days=k) for d in days for k in (0, 1)]
                period = f"{tmst - 30}-{tmst - 1}"
                ax.plot(xs, [v for v in mx for _ in (0, 1)], color=ccol, lw=1.3, label=f"clim max/min (KMA {period})")
                ax.plot(xs, [v for v in mn for _ in (0, 1)], color=ccol, lw=1.3)
                clim_drawn = True
        if key == "pr6":
            ax.set_ylim(0, max(1.0, ax.get_ylim()[1]))           # dry runs: a 0-1 mm axis, not a flat line at 0
        lo, hi = ax.get_ylim()                 # reference lines only where the data already reaches
        for ref, alpha, *col in e.get("ref_lines", {}).get(key, []):
            if lo <= ref <= hi:
                ax.axhline(ref, color=col[0] if col else "#e00000", lw=0.9, ls="--", alpha=alpha)
        ax.set_ylim(lo, hi)
        ax.set_ylabel(label)
        ax.grid(alpha=0.3)
        ax.legend(loc="upper right", fontsize=8, ncol=4)
    axes[-1].set_xlim(*xlim)
    axes[-1].xaxis.set_major_locator(mdates.DayLocator())
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    axes[-1].set_xlabel("valid time (KST, 00 KST ticks)")
    mem = {m for s in st for m in series[s].get(panels[0][0], {})}
    n = len(mem - {0})
    ctl = " + control" if 0 in mem else ""
    fig.suptitle(f"{e[title_key].format(name=name)}   {n} members{ctl}\n"
                 f"{cfg['style']['model_label']}   run {run:%Y-%m-%d %H}UTC ({run + kst:%m-%d %H}KST)", fontsize=11)
    credit = cfg["style"]["credit"] + ("\n" + cfg["clim"]["credit"] if clim_drawn else "")
    fig.text(0.01, 0.005, credit, fontsize=6.5, color="#555", linespacing=1.3)
    fig.tight_layout(rect=(0, 0.025 if clim_drawn else 0.015, 1, 1))
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
        st = steps(cfg, run.hour)
        for s, vals in zip(st, ex.map(lambda s: src.point_step(run, s), st)):
            series[s] = vals
    dl = time.monotonic() - t0
    out = cfg["output"]
    nbytes = 0
    want = {c["id"] for c in cfg["site"]["charts"]}
    charts = []
    for pt in cfg["ens"]["points"]:
        ps = derived({s: v[pt["id"]] for s, v in series.items()})
        cl = cfg["ens"].get("clim") or {}                 # clim/points.json, attached by the poll (if built)
        pc = {"era5": cl.get("era5", {}).get("points", {}).get(pt["id"]),
              "kma": cl.get("kma", {}).get("points", {}).get(pt["id"])}
        for chart in CHARTS:
            cid = f"{chart}_{pt['id']}"
            if cid not in want:
                continue
            data = encode.encode(plume(cfg, run, ps, pt["name"], chart, pc), out["format"], out["png8_colors"],
                                 out["webp_quality"])
            (rdir / f"{cid}_000.png").write_bytes(data)
            nbytes += len(data)
            charts.append(cid)
    (rdir / "series.json").write_text(json.dumps({str(s): v for s, v in series.items()}))
    meta = {"model": model, "run": run_s, "steps": [0], "missing_steps": [], "wpsh": None,
            "render_version": cfg["site"]["render_version"],
            "rendered_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sources": sorted(src.order), "images": len(charts), "charts": charts, "bytes": nbytes,
            "download_seconds": round(dl, 1), "wall_seconds": round(time.monotonic() - t0, 1),
            "render_seconds_sum": 0.0,
            "points": {p["id"]: [p["lat"], p["lon"]] for p in cfg["ens"]["points"]}}
    (rdir / "meta.json").write_text(json.dumps(meta, indent=1))
    return meta

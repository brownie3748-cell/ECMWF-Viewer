"""강수 비교: the KMA forecast's rainfall (단기예보 PCP, 초단기예보 RN1) as numbers from the API Hub grid service, drawn
on the KIM Korea map in the radar's colours, per issuance (= the forecast's "run") and valid hour.

Grid (measured M10 round 2, 2026-11-28): 149 x 253 comma-separated values, row 0 = south, -99 = no value (the
forecast covers land and the near coast only); the 5 km KMA forecast grid, LCC 30/60N 126E, origin 38N 126E at
x = 43, y = 136 (1-based), earth radius 6371.00877 km. Values are KMA's precipitation classes (only 0 and 1 were seen
on the measurement night; how the classes map to mm is UNVERIFIED and they are drawn as given).
The hub is slow and flaky (measured: 2-75 s per call, 504 / 503 on about half the calls), hence retries and a time
budget; every grid is cached in the store (compare/grid/...) so 3 h sums and later runs do not ask again.
"""

from __future__ import annotations

import datetime as dt
import io
import logging
import os
import time

import numpy as np
import requests

from . import encode, kim, plot

log = logging.getLogger(__name__)
NX, NY = 149, 253
PRODUCTS = {"shrt": ("PCP", "short-range"), "vsrt": ("RN1", "ultra-short-range")}   # chart text: English (font)


def grid_crs():
    import cartopy.crs as ccrs
    return ccrs.LambertConformal(central_longitude=126.0, central_latitude=38.0, standard_parallels=(30.0, 60.0),
                                 globe=ccrs.Globe(semimajor_axis=6371008.77, semiminor_axis=6371008.77,
                                                  ellipse=None))


def grid_xy():
    """Cell centres (m) of the forecast grid in grid_crs()."""
    return (np.arange(NX) - 42) * 5000.0, (np.arange(NY) - 135) * 5000.0


def parse(text: bytes) -> np.ndarray | None:
    try:
        v = np.array([float(x) for x in text.decode("ascii", "replace").replace("\n", ",").split(",") if x.strip()])
    except ValueError:
        return None
    return v.reshape(NY, NX) if v.size == NX * NY else None


def issuances(cfg: dict, product: str, hours: list[dt.datetime]) -> list[dt.datetime]:
    """Issuances (KST) whose forecast reaches into the radar hours: 단기 at its 8 issue hours, 초단기 every hour (HH:30)."""
    c = cfg["compare"]["forecast"][product]
    first, last = hours[0] - dt.timedelta(hours=c["horizon_h"]), hours[-1]
    out, t = [], first.replace(minute=0)
    while t < last:
        if product == "shrt" and t.hour in cfg["compare"]["forecast"]["shrt_issue_h"]:
            out.append(t)
        elif product == "vsrt":
            out.append(t + dt.timedelta(minutes=30))
        t += dt.timedelta(hours=1)
    return out


def valid_hours(cfg: dict, product: str, fc: dt.datetime, hours: list[dt.datetime]) -> list[dt.datetime]:
    h = cfg["compare"]["forecast"][product]["horizon_h"]
    return [v for v in hours if fc < v <= fc + dt.timedelta(hours=h)]


def url(cfg: dict, product: str, fc: dt.datetime, ef: dt.datetime) -> str:
    var = PRODUCTS[product][0]
    tf = f"{fc:%Y%m%d%H}" if product == "shrt" else f"{fc:%Y%m%d%H%M}"
    return cfg["compare"]["forecast"]["url"].format(cgi=cfg["compare"]["forecast"][product]["cgi"], tmfc=tf,
                                                    tmef=f"{ef:%Y%m%d%H}", var=var)


def fetch(cfg, session, usage, product, fc, ef) -> np.ndarray | None:
    key = os.environ.get("KMA_APIHUB_KEY", "")
    if not key:
        raise RuntimeError("KMA_APIHUB_KEY is not set")
    u = url(cfg, product, fc, ef) + "&authKey=" + key
    for attempt in range(cfg["compare"]["forecast"]["retries"]):
        try:
            r = session.get(u, timeout=cfg["compare"]["forecast"]["timeout_s"])
            usage["calls"] += 1
            usage["bytes"] += len(r.content)
            if r.status_code == 403 and "제한".encode() in r.content:
                raise kim.QuotaError("KMA API Hub daily limit reached (HTTP 403)")
            if r.status_code == 200:
                g = parse(r.content)
                if g is not None:
                    return g
            log.warning("grid %s %s %s: HTTP %s %r", product, f"{fc:%m%d%H%M}", f"{ef:%m%d%H}", r.status_code,
                        r.content[:80])
        except kim.QuotaError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("grid %s %s %s: %r", product, f"{fc:%m%d%H%M}", f"{ef:%m%d%H}", e)
        time.sleep(3 * (attempt + 1))
    return None


def frame(cfg: dict, product: str, fc: dt.datetime, ef: dt.datetime, window: int, acc: np.ndarray) -> bytes:
    c = cfg["compare"]
    fr = plot.Frame(cfg, c["domain"], strong_coast=True)
    cmap, norm, lv = plot.compare_cmap(cfg)
    x, y = grid_xy()
    a = np.where(acc >= 0, acc, np.nan)
    a = np.where(a > 0, a, np.nan)                       # 0 mm: no colour (as the radar image)
    m = fr.ax.pcolormesh(x, y, a, cmap=cmap, norm=norm, transform=grid_crs(), zorder=1, shading="nearest")
    fr.colorbar(m, label="mm", ticks=lv)
    from .compare import _header
    name = PRODUCTS[product][1]
    _header(cfg, fr, c["forecast"]["title"].format(window=window),
            f"KMA {name} forecast {fc:%m/%d %H:%M} KST", ef, c["forecast"]["credit"])
    fmt = cfg["output"]["format"]
    return encode.encode(fr.png_bytes(), fmt, cfg["output"]["png8_colors"], cfg["output"]["webp_quality"])


def _grid_key(cfg, product, fc, ef):
    return f"{cfg['compare']['prefix']}grid/{product}/{fc:%Y%m%d%H%M}/{ef:%Y%m%d%H}.npz"


def _load_grid(st, cfg, product, fc, ef):
    raw = st.get_bytes(_grid_key(cfg, product, fc, ef))
    return np.load(io.BytesIO(raw))["g"].astype(np.float32) / 10.0 if raw else None


def _save_grid(st, cfg, product, fc, ef, g, sizes):
    buf = io.BytesIO()
    np.savez_compressed(buf, g=np.round(np.where(g < -90, -990, g * 10)).astype(np.int16))
    k = _grid_key(cfg, product, fc, ef)
    st.put_bytes(k, buf.getvalue(), "no-cache")
    sizes[k] = len(buf.getvalue())


def run(cfg: dict, st, prev: dict, hours: list[dt.datetime], sizes: dict) -> tuple[list[str], dict]:
    """Forecast frames for the radar hours: returns (summary lines, {product: {fc: {window: [ef, ...]}}}).
    prev is the index's forecast part; issuances that no longer reach the radar hours are dropped (and deleted)."""
    from . import dfs
    c = cfg["compare"]
    fcfg = c["forecast"]
    day = f"{hours[-1]:%Y-%m-%d}"
    usage = st.get_json(dfs.usage_key(day)) or {}
    if usage.get("day") != day:
        usage = {"day": day, "calls": 0, "bytes": 0}
    calls0 = usage["calls"]
    session = requests.Session()
    t0 = time.monotonic()
    out: dict = {}
    lines, new, stopped = [], 0, False
    ext = encode.EXT[cfg["output"]["format"]]
    for product in PRODUCTS:
        out[product] = {}
        for fc in issuances(cfg, product, hours):
            fcs = f"{fc:%Y%m%d%H%M}"
            have = {w: set(v) for w, v in prev.get(product, {}).get(fcs, {}).items()}
            ent = {str(w): sorted(have.get(str(w), set())) for w in c["windows"]}
            grids: dict = {}
            for ef in valid_hours(cfg, product, fc, hours):
                efs = f"{ef:%Y%m%d%H}"
                for w in c["windows"]:
                    if efs in have.get(str(w), set()):
                        continue
                    if time.monotonic() - t0 > fcfg["time_budget_min"] * 60 or \
                            usage["calls"] + w > fcfg["max_calls_per_run"] + calls0:
                        stopped = True
                        break
                    span = [ef - dt.timedelta(hours=i) for i in range(w)]
                    if any(s <= fc for s in span):
                        continue                       # the window starts before the issuance
                    acc = np.zeros((NY, NX), np.float32)
                    ok = True
                    for s in span:
                        if s not in grids:
                            g = _load_grid(st, cfg, product, fc, s)
                            if g is None:
                                g = fetch(cfg, session, usage, product, fc, s)
                                if g is not None:
                                    _save_grid(st, cfg, product, fc, s, g, sizes)
                            grids[s] = g
                        if grids[s] is None:
                            ok = False
                            break
                        acc = np.where((grids[s] < -90) | (acc < 0), -99.0, acc + np.maximum(grids[s], 0))
                    if not ok:
                        continue
                    data = frame(cfg, product, fc, ef, w, acc)
                    k = f"{c['prefix']}fcst/{product}/{fcs}/{w}h_{efs}.{ext}"
                    st.put_bytes(k, data, cfg["storage"]["image_cache_control"])
                    sizes[k] = len(data)
                    ent[str(w)] = sorted(set(ent[str(w)]) | {efs})
                    new += 1
                if stopped:
                    break
            if any(ent.values()):
                out[product][fcs] = ent
            if stopped:
                break
        if stopped:
            # keep what the index had for the issuances not reached this time
            for fcs, e in prev.get(product, {}).items():
                out[product].setdefault(fcs, e)
            break
    for product in PRODUCTS:
        out.setdefault(product, dict(prev.get(product, {})) if stopped else {})
    # issuances that left the window: delete their frames and grids
    gone = [p for p in PRODUCTS for fcs in prev.get(p, {}) if fcs not in out.get(p, {})]
    if gone and not stopped:
        for product in PRODUCTS:
            for fcs in prev.get(product, {}):
                if fcs not in out[product]:
                    for pre in (f"{c['prefix']}fcst/{product}/{fcs}/", f"{c['prefix']}grid/{product}/{fcs}/"):
                        st.delete_prefix(pre, sizes)
    st.put_bytes(dfs.usage_key(day), __import__("json").dumps(usage).encode(), "no-cache")
    lines.append(f"- forecast: {new} new frames, {usage['calls'] - calls0} hub calls, {time.monotonic() - t0:.0f} s "
                 f"(measured)" + ("; stopped at the time / call budget (resumes next run)" if stopped else ""))
    return lines, out

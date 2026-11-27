"""Climate lines for the ENS 기온 graphs, built once into the store (clim.yml: `python -m render clim`).

    clim/points.json = {
      "era5": {"period": "1990-2019", "hours_utc": [0, 6, 12, 18],
               "points": {point: {"t850": [[4 values] x 366 days of year], "z500": [...]}}},
      "kma":  {"points": {point: {"stn": 108, "tmst": 2021, "days": {"MMDD": [TA, TA_MAX, TA_MIN]}}}}}

ERA5: the WeatherBench2 6-hourly climatology (zarr v2, blosc/lz4 chunks of 3 hours x 3 days x the whole globe,
latitude 90..-90, longitude 0..359.75, measured 2026-11-27), read chunk by chunk over HTTP and interpolated
bilinearly to the points. KMA: daily normals of every ASOS station in one request per normal period.
"""

from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import logging
import os
import time

import numpy as np
import requests

log = logging.getLogger(__name__)
NLAT, NLON, NDOY, NHOUR, CH = 721, 1440, 366, 4, 3


def points(cfg: dict) -> dict:
    """Every ENS point (the same ids in each ENS model), from the configured models."""
    from . import config, ens
    out = {}
    for mid in config.model_ids(cfg):
        mc = config.for_model(cfg, mid)
        if ens.is_ens(mc):
            for p in mc["ens"]["points"]:
                out.setdefault(p["id"], p)
    return out


def _codec(meta: dict):
    import numcodecs                                   # only clim.yml installs it
    return numcodecs.get_codec(meta["compressor"])


def grid_box(cfg: dict) -> dict:
    """lat / lon box of the map climatology: the union of the map models' default domains plus a margin."""
    from . import config, ens, plot
    boxes = []
    for mid in config.model_ids(cfg):
        mc = config.for_model(cfg, mid)
        if not ens.is_ens(mc) and any(c["id"].startswith("a_") for c in mc["site"]["charts"]):
            boxes.append(plot.domain_geometry(mc["domains"]["default"])["bbox"])
    m = cfg["clim"]["grid_margin_deg"]
    return {"lat_min": min(b["lat_min"] for b in boxes) - m, "lat_max": max(b["lat_max"] for b in boxes) + m,
            "lon_min": min(b["lon_min"] for b in boxes) - m, "lon_max": max(b["lon_max"] for b in boxes) + m}


def era5(cfg: dict, pts: dict, box: dict | None = None) -> tuple[dict, dict | None]:
    """(points part of clim/points.json, map grid {"lat", "lon", field: float32 [366, 4, nlat, nlon]})."""
    from .ens import bilinear
    c = cfg["clim"]
    base = c["era5_base"]
    s = requests.Session()
    meta = s.get(base + ".zmetadata", timeout=60).json()["metadata"]

    def array(name, key):
        a = meta[f"{name}/.zarray"]
        for attempt in range(4):
            try:
                r = s.get(f"{base}{name}/{key}", timeout=120)
                r.raise_for_status()
                return np.frombuffer(_codec(a).decode(r.content), dtype=a["dtype"])
            except Exception as e:  # noqa: BLE001
                log.warning("%s/%s: %r", name, key, e)
        raise RuntimeError(f"{name}/{key} failed 4 times")

    lat, lon = array("latitude", "0"), array("longitude", "0")
    if not (lat[0] == 90 and lat[-1] == -90 and lon[0] == 0 and len(lat) == NLAT and len(lon) == NLON):
        raise RuntimeError("unexpected ERA5 climatology grid")
    hours = [int(h) for h in array("hour", "0")]
    levels = [int(v) for v in array("level", "0")]
    pf = [f for f in c["era5_fields"] if f.get("points", True)]
    gf = [f for f in c["era5_fields"] if f.get("grid")] if box else []
    out = {pid: {f["key"]: [[None] * NHOUR for _ in range(NDOY)] for f in pf} for pid in pts}
    grid = None
    if gf:
        jy = np.where((lat >= box["lat_min"]) & (lat <= box["lat_max"]))[0]
        ix = np.where((lon >= box["lon_min"]) & (lon <= box["lon_max"]))[0]
        grid = {"lat": lat[jy][::-1].astype(np.float32), "lon": lon[ix].astype(np.float32)}   # lat ascending
        for f in gf:
            grid[f["key"]] = np.full((NDOY, NHOUR, len(jy), len(ix)), np.nan, np.float32)

    def job(f, hc, dc):
        lev = f.get("level")
        key = f"{hc}.{dc}.{levels.index(lev)}.0.0" if lev else f"{hc}.{dc}.0.0"
        a = array(f["var"], key).reshape(CH, CH, NLAT, NLON)
        for h in range(CH):
            for d in range(CH):
                hi, di = hc * CH + h, dc * CH + d
                if hi >= NHOUR or di >= NDOY:
                    continue
                v = a[h, d]
                if f.get("points", True):
                    flat = v.ravel()
                    for pid, p in pts.items():
                        x = bilinear(flat, NLON, NLAT, p["lat"], p["lon"], 90.0, 0.0, 0.25, 0.25)
                        y = x * f["scale"] + f["offset"]
                        out[pid][f["key"]][di][hi] = float(f"{y:.5g}") if abs(y) < 1 else round(y, 2)   # q: kg/kg
                if grid is not None and f.get("grid"):
                    grid[f["key"]][di, hi] = v[jy][:, ix][::-1] * f["scale"] + f["offset"]

    used = {f["key"]: f for f in pf + gf}.values()
    jobs = [(f, hc, dc) for f in used for hc in range(-(-NHOUR // CH)) for dc in range(-(-NDOY // CH))]
    with cf.ThreadPoolExecutor(c["parallel"]) as ex:
        list(ex.map(lambda j: job(*j), jobs))
    missing = sum(v is None for p in out.values() for f in p.values() for day in f for v in day)
    if missing or (grid and any(np.isnan(grid[f["key"]]).any() for f in gf)):
        raise RuntimeError(f"ERA5 climatology: values missing ({missing} at points)")
    return {"period": "1990-2019", "hours_utc": hours, "chunks": len(jobs), "points": out}, grid


def grid_files(cfg: dict, grid: dict) -> dict[str, bytes]:
    """{store key: npz bytes}, one file per day of year: per field int16 [4 hours, lat, lon] = value x pack."""
    import io
    out = {}
    gf = [f for f in cfg["clim"]["era5_fields"] if f.get("grid")]
    for d in range(NDOY):
        arrs = {"lat": grid["lat"], "lon": grid["lon"]}
        for f in gf:
            arrs[f["key"]] = np.round(grid[f["key"]][d] * f["pack"]).astype(np.int16)
            arrs[f["key"] + "_pack"] = np.float64(f["pack"])
        buf = io.BytesIO()
        np.savez_compressed(buf, **arrs)
        out[cfg["clim"]["grid_key"].format(doy=d + 1)] = buf.getvalue()
    return out


def parse_kma(text: str) -> dict:
    """{stn: {"MMDD": [TA, TA_MAX, TA_MIN]}} from sfc_norm1.php (comma-separated rows ST, STN, MM, DD, TA, ...)."""
    out: dict = {}
    for line in text.splitlines():
        if not line[:1].isdigit():
            continue
        r = [x.strip() for x in line.split(",")]
        try:
            vals = [float(r[4]), float(r[5]), float(r[6])]
        except ValueError:
            continue
        if any(v < -90 for v in vals):                 # missing-value codes
            continue
        out.setdefault(int(r[1]), {})[f"{int(r[2]):02d}{int(r[3]):02d}"] = vals
    return out


def _get_kma(url: str) -> str:
    """The hub resets connections now and then (measured, 2026-11-26 / 27): 4 tries."""
    for attempt in range(4):
        try:
            r = requests.get(url, timeout=180)
            r.raise_for_status()
            return r.content.decode("euc-kr", errors="replace")
        except Exception as e:  # noqa: BLE001
            log.warning("KMA normals (try %d): %s", attempt + 1, type(e).__name__)
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("KMA normals: 4 failed tries")


def kma(cfg: dict, pts: dict) -> dict:
    c = cfg["clim"]
    key = os.environ.get("KMA_APIHUB_KEY", "")
    if not key:
        raise RuntimeError("KMA_APIHUB_KEY is not set")
    out, by_tmst = {}, {}
    for pid, p in pts.items():
        for tmst in c["kma_tmst"]:
            if tmst not in by_tmst:
                by_tmst[tmst] = parse_kma(_get_kma(c["kma_url"].format(tmst=tmst) + "&authKey=" + key))
            days = by_tmst[tmst].get(p["stn"])
            if days and len(days) >= 365:
                out[pid] = {"stn": p["stn"], "tmst": tmst, "days": days}
                break
        else:
            log.warning("%s (stn %s): no KMA normals", pid, p.get("stn"))
    return {"points": out}


def build(cfg: dict) -> tuple[dict, dict[str, bytes]]:
    """(clim/points.json content, {clim/grid/<doy>.npz: bytes})."""
    pts = points(cfg)
    box = grid_box(cfg)
    e, grid = era5(cfg, pts, box)
    files = grid_files(cfg, grid) if grid else {}
    return ({"schema": 1, "generated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "era5": e, "kma": kma(cfg, pts), "grid": {"box": box, "files": len(files)}}, files)


# ------------------------------------------------------------------ map climatology (편차 maps)
_GRID_CACHE: dict = {}


def fetch_grid(st, cfg: dict, run: dt.datetime, last_step: int, local: "os.PathLike") -> int:
    """Download the day-of-year files a run needs (run .. run + last_step + 6 h, UTC) into `local`.
    Returns how many were missing in the store (0 = all there)."""
    import pathlib
    local = pathlib.Path(local)
    local.mkdir(parents=True, exist_ok=True)
    missing = 0
    t, end = run - dt.timedelta(days=1), run + dt.timedelta(hours=last_step + 6)
    while t <= end + dt.timedelta(days=1):
        key = cfg["clim"]["grid_key"].format(doy=t.timetuple().tm_yday)
        path = local / key.rsplit("/", 1)[-1]
        if not path.exists():
            raw = st.get_bytes(key)
            if raw is None:
                missing += 1
            else:
                path.write_bytes(raw)
        t += dt.timedelta(days=1)
    return missing


def _load_doy(local: str, key_tmpl: str, doy: int) -> dict | None:
    import pathlib
    k = (local, doy)
    if k not in _GRID_CACHE:
        if len(_GRID_CACHE) > 8:
            _GRID_CACHE.clear()
        path = pathlib.Path(local) / key_tmpl.format(doy=doy).rsplit("/", 1)[-1]
        if not path.exists():
            return None
        z = np.load(path)
        _GRID_CACHE[k] = {n: z[n] for n in z.files}
    return _GRID_CACHE[k]


def grid_at(cfg: dict, valid_utc: dt.datetime, local: str | None = None) -> dict | None:
    """Map climatology at a valid time: {"lat", "lon" (ascending), field: float32 [lat, lon]}, linear in time
    between the 6-hourly values; None when the day files are not there (anomaly maps are then skipped)."""
    local = local or os.environ.get("ANOM_CLIM_DIR")
    if not local:
        return None
    a = valid_utc.replace(minute=0, second=0, microsecond=0, hour=(valid_utc.hour // 6) * 6)
    b = a + dt.timedelta(hours=6)
    w = (valid_utc - a).total_seconds() / 21600.0
    za = _load_doy(local, cfg["clim"]["grid_key"], a.timetuple().tm_yday)
    zb = _load_doy(local, cfg["clim"]["grid_key"], b.timetuple().tm_yday)
    if za is None or zb is None:
        return None
    out = {"lat": za["lat"], "lon": za["lon"]}
    for f in cfg["clim"]["era5_fields"]:
        if f.get("grid"):
            k = f["key"]
            va = za[k][a.hour // 6].astype(np.float32) / float(za[k + "_pack"])
            vb = zb[k][b.hour // 6].astype(np.float32) / float(zb[k + "_pack"])
            out[k] = va * (1 - w) + vb * w
    return out


def regrid(g: dict, key: str, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """Bilinear from the climatology grid (0.25 deg, ascending lat / lon) to a model's lat / lon axes."""
    from scipy.interpolate import RegularGridInterpolator
    f = RegularGridInterpolator((g["lat"], g["lon"]), g[key], bounds_error=False, fill_value=np.nan)
    la, lo = np.meshgrid(lat, lon, indexing="ij")
    return f(np.stack([la.ravel(), lo.ravel()], axis=-1)).reshape(la.shape).astype(np.float32)


# ------------------------------------------------------------------ used while drawing (render/ens.py)
def era5_at(clim_pt: dict, key: str, valid_utc: list[dt.datetime]) -> list[float] | None:
    """ERA5 climate at each valid time (UTC): linear in time between the 6-hourly values of its day of year."""
    v = (clim_pt or {}).get("era5", {}).get(key)
    if not v:
        return None
    out = []
    for t in valid_utc:
        a = t.replace(minute=0, second=0, microsecond=0, hour=(t.hour // 6) * 6)
        b = a + dt.timedelta(hours=6)
        w = (t - a).total_seconds() / 21600.0
        va = v[a.timetuple().tm_yday - 1][a.hour // 6]
        vb = v[b.timetuple().tm_yday - 1][b.hour // 6]
        out.append(va * (1 - w) + vb * w)
    return out


def theta_e_at(clim_pt: dict, lev: str, valid_utc: list[dt.datetime]) -> list[float] | None:
    """Theta-e (K) of the climatological mean T and q at 850 / 925 hPa: an approximation of the mean theta-e
    (theta-e is not linear in T and q; estimated error well under 1 K)."""
    from .derive import theta_e
    t, q = era5_at(clim_pt, "t" + lev, valid_utc), era5_at(clim_pt, "q" + lev, valid_utc)
    if not t or not q:
        return None
    return [float(theta_e(np.float64(a + 273.15), np.float64(b), float(lev))) for a, b in zip(t, q)]


def kma_at(clim_pt: dict, valid_kst: list[dt.datetime]) -> tuple[list, list, int] | None:
    """KMA daily normal max / min for the KST date of each valid time, and the normal period's tmst."""
    k = (clim_pt or {}).get("kma")
    if not k:
        return None
    rows = [k["days"].get(f"{t:%m%d}") or k["days"].get("0228") for t in valid_kst]
    return [r[1] for r in rows], [r[2] for r in rows], k["tmst"]

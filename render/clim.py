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


def era5(cfg: dict, pts: dict) -> dict:
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
    out = {pid: {f["key"]: [[None] * NHOUR for _ in range(NDOY)] for f in c["era5_fields"]} for pid in pts}

    def job(f, hc, dc):
        key = f"{hc}.{dc}.{levels.index(f['level'])}.0.0"
        a = array(f["var"], key).reshape(CH, CH, NLAT, NLON)
        for h in range(CH):
            for d in range(CH):
                hi, di = hc * CH + h, dc * CH + d
                if hi >= NHOUR or di >= NDOY:
                    continue
                v = a[h, d].ravel()
                for pid, p in pts.items():
                    x = bilinear(v, NLON, NLAT, p["lat"], p["lon"], 90.0, 0.0, 0.25, 0.25)
                    out[pid][f["key"]][di][hi] = round(x * f["scale"] + f["offset"], 2)

    jobs = [(f, hc, dc) for f in c["era5_fields"] for hc in range(-(-NHOUR // CH)) for dc in range(-(-NDOY // CH))]
    with cf.ThreadPoolExecutor(c["parallel"]) as ex:
        list(ex.map(lambda j: job(*j), jobs))
    missing = sum(v is None for p in out.values() for f in p.values() for day in f for v in day)
    if missing:
        raise RuntimeError(f"ERA5 climatology: {missing} values missing")
    return {"period": "1990-2019", "hours_utc": hours, "chunks": len(jobs), "points": out}


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


def build(cfg: dict) -> dict:
    pts = points(cfg)
    return {"schema": 1, "generated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "era5": era5(cfg, pts), "kma": kma(cfg, pts)}


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


def kma_at(clim_pt: dict, valid_kst: list[dt.datetime]) -> tuple[list, list, int] | None:
    """KMA daily normal max / min for the KST date of each valid time, and the normal period's tmst."""
    k = (clim_pt or {}).get("kma")
    if not k:
        return None
    rows = [k["days"].get(f"{t:%m%d}") or k["days"].get("0228") for t in valid_kst]
    return [r[1] for r in rows], [r[2] for r in rows], k["tmst"]

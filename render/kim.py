"""KIM (KMA) source: downloads from the KMA API Hub, one field per request, into an .npz the renderer reads.

Measured (M8, docs/STAGE3.md):
- `nwp_nc_vars_down.php` returns one field per request (several vars -> text error; several levels -> first only).
- KIM global (KIMG): regular lat/lon 0.083333 deg, cut to a lon/lat box on request.
- KIM regional (KIMR): Lambert conformal 3 km (LoV 126E, Latin 30/60N).
- The GRIB2 headers do not identify the field (always "t", 2007-03-23, step 0): fields are labelled from the
  request, never from the GRIB keys. Only the grid definition and the values are used.

Every field is put on one regular lat/lon grid per model (config `kma.grid`), so the charts use the same map
frame as the ECMWF models: KIMG fields are placed on it by index (same 1/12 deg lattice), KIMR fields are
interpolated bilinearly from the Lambert grid. Points outside the model domain are NaN.

The API key comes from the KMA_APIHUB_KEY environment variable and never appears in logs. A daily quota guard
(calls and bytes per KST day, stored in the site store under _ops/kma-<date>.json) stops downloads before the
API Hub limits (20,000 calls, 5 GB per day).
"""

from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import json
import logging
import os
import pathlib
import tempfile
import threading
import time

import numpy as np
import requests


log = logging.getLogger(__name__)

BASE = "https://apihub.kma.go.kr/api/typ06/url/nwp_nc_vars_down.php"
TIMEOUT = 15                # median success 2-5 s, but a long tail up to 60 s and 504s at 60 s
                            # (CI 2026-11-15): give up early and retry instead of waiting
ATTEMPTS = 8
KST = dt.timezone(dt.timedelta(hours=9))


class QuotaError(RuntimeError):
    """The daily KMA API Hub budget would be exceeded."""


# ------------------------------------------------------------------------------------------- grid helpers
def target_grid(cfg: dict) -> tuple[np.ndarray, np.ndarray]:
    g = cfg["kma"]["grid"]
    lat = np.round(np.arange(g["lat_min"], g["lat_max"] + g["step"] / 2, g["step"]), 6)
    lon = np.round(np.arange(g["lon_min"], g["lon_max"] + g["step"] / 2, g["step"]), 6)
    return lat, lon


def read_single(path: str) -> dict:
    """Values and grid of the single GRIB message in `path` (identity keys are ignored on purpose)."""
    import eccodes

    with open(path, "rb") as fh:
        h = eccodes.codes_grib_new_from_file(fh)
        if h is None:
            raise ValueError("no GRIB message")
        try:
            g = {"gridType": eccodes.codes_get(h, "gridType")}
            ni, nj = eccodes.codes_get(h, "Ni"), eccodes.codes_get(h, "Nj")
            vals = eccodes.codes_get_values(h).astype(np.float64)
            miss = eccodes.codes_get(h, "missingValue")
            vals[vals == miss] = np.nan
            g["lats"] = eccodes.codes_get_array(h, "latitudes").reshape(nj, ni)
            g["lons"] = eccodes.codes_get_array(h, "longitudes").reshape(nj, ni) % 360.0
            if g["gridType"] == "lambert":
                for k in ("LoVInDegrees", "Latin1InDegrees", "Latin2InDegrees", "LaDInDegrees", "DxInMetres",
                          "DyInMetres"):
                    g[k] = eccodes.codes_get(h, k)
                try:
                    g["radius"] = eccodes.codes_get(h, "radius")
                except Exception:  # noqa: BLE001
                    g["radius"] = 6371229.0
        finally:
            eccodes.codes_release(h)
    g["values"] = vals.reshape(nj, ni)
    return g


def place_regular(g: dict, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """Put a regular_ll field on the target lattice by nearest index (both are on the same 1/12 deg lattice)."""
    out = np.full((lat.size, lon.size), np.nan, dtype=np.float32)
    flat, flon, v = g["lats"][:, 0], g["lons"][0, :], g["values"]
    step = lat[1] - lat[0]
    iy = np.rint((flat - lat[0]) / step).astype(int)
    ix = np.rint((flon - lon[0]) / step).astype(int)
    my = (iy >= 0) & (iy < lat.size)
    mx = (ix >= 0) & (ix < lon.size)
    out[np.ix_(iy[my], ix[mx])] = v[np.ix_(my, mx)]
    return out


def lambert_to_regular(g: dict, lat: np.ndarray, lon: np.ndarray, check_tol_m: float = 500.0) -> np.ndarray:
    """Bilinear interpolation of a Lambert-conformal field onto the regular lat/lon target grid."""
    import pyproj
    from scipy import ndimage

    proj = pyproj.Proj(proj="lcc", lat_1=g["Latin1InDegrees"], lat_2=g["Latin2InDegrees"],
                       lat_0=g.get("LaDInDegrees", g["Latin1InDegrees"]), lon_0=g["LoVInDegrees"],
                       R=g.get("radius", 6371229.0))
    x, y = proj(g["lons"], g["lats"])
    nj, ni = x.shape
    # grid origin and spacing from the GRIB's own point coordinates (robust to scanning-mode conventions)
    dx = (x[:, -1] - x[:, 0]).mean() / (ni - 1)
    dy = (y[-1, :] - y[0, :]).mean() / (nj - 1)
    x0, y0 = x[:, 0].mean(), y[0, :].mean()
    ii, jj = np.meshgrid(np.arange(ni), np.arange(nj))
    err = np.hypot(x0 + ii * dx - x, y0 + jj * dy - y).max()
    if err > check_tol_m:
        raise ValueError(f"Lambert grid does not fit a regular x/y lattice (max error {err:.0f} m)")
    LON, LAT = np.meshgrid(lon, lat)
    tx, ty = proj(LON, LAT)
    fi, fj = (tx - x0) / dx, (ty - y0) / dy
    v = g["values"]
    out = ndimage.map_coordinates(np.nan_to_num(v, nan=0.0), [fj, fi], order=1, mode="constant", cval=np.nan)
    bad = ndimage.map_coordinates(np.isnan(v).astype(float), [fj, fi], order=1, mode="constant", cval=1.0) > 0
    out[bad] = np.nan
    return out.astype(np.float32)


def save_npz(path: pathlib.Path, lat, lon, data: dict) -> None:
    arrays = {"lat": lat, "lon": lon}
    for (name, level), a in data.items():
        arrays[f"{name}|{level}"] = a
    np.savez_compressed(path, **arrays)


# ------------------------------------------------------------------------------------------- the source
class KimSource:
    """Same interface as sources.Sources (run_complete, fetch_step) for one KIM model."""

    def __init__(self, cfg: dict, usage: dict | None = None):
        self.cfg = cfg
        self.k = cfg["kma"]
        self.key = os.environ.get("KMA_APIHUB_KEY", "")
        self.lat, self.lon = target_grid(cfg)
        self.usage = usage if usage is not None else new_usage()
        self.lock = threading.Lock()
        self._tp: dict = {}                       # (run, step) -> tp array, so tp windows need no extra request
        self.stats: dict = {}
        self.req_log: list = []                   # (seconds, HTTP status or exception name) per attempt

    # -------------------------------------------------------------------- requests
    def _url(self, run: dt.datetime, step: int, data: str, var: str, level: int | None, box) -> str:
        u = (f"{BASE}?group={self.k['group']}&tmfc={run:%Y%m%d%H}&hf={step}&data={data}&vars={var}"
             f"&dataType=GB2")
        if level is not None:
            u += f"&lvl_lst={level}"
        if box:
            u += "&lon1={}&lat1={}&lon2={}&lat2={}".format(*box)
        return u

    def _charge(self, calls: int, nbytes: int) -> None:
        with self.lock:
            self.usage["calls"] += calls
            self.usage["bytes"] += nbytes

    def check_budget(self, calls: int, nbytes: float) -> None:
        q = self.cfg["kma_quota"]
        if self.usage["calls"] + calls > q["max_calls_per_day"] or self.usage["bytes"] + nbytes > q["max_bytes_per_day"]:
            raise QuotaError(f"KMA API Hub daily budget: used {self.usage['calls']} calls / "
                             f"{self.usage['bytes'] / 1e9:.2f} GB, next needs about {calls} calls / "
                             f"{nbytes / 1e9:.2f} GB (limits {q['max_calls_per_day']} / {q['max_bytes_per_day'] / 1e9:.1f} GB)")

    def _get(self, url: str) -> bytes:
        if not self.key:
            raise RuntimeError("KMA_APIHUB_KEY is not set")
        last = None
        for attempt in range(ATTEMPTS):
            t0 = time.monotonic()
            try:
                self.check_budget(1, 0)
                r = requests.get(url + "&authKey=" + self.key, timeout=TIMEOUT)
                self._charge(1, len(r.content))
                self._log(time.monotonic() - t0, r.status_code)
                if r.status_code == 200 and r.content[:4] == b"GRIB":
                    return r.content
                if r.status_code == 403 and "제한".encode() in r.content:
                    # the hub's own daily limit ("일일 최대 호출 용량 제한"): stop at once, no retries
                    raise QuotaError("KMA API Hub daily limit reached (HTTP 403)")
                last = RuntimeError(f"HTTP {r.status_code}: {r.content[:200]!r}")
            except QuotaError:
                raise
            except Exception as e:  # noqa: BLE001
                last = e
                self._log(time.monotonic() - t0, type(e).__name__)
                # a timed-out request may still have been served (and counted) by the hub: charge it in full
                self._charge(1, int(self.k.get("est_bytes_per_field", 0)))
            time.sleep(min(30, 2 * 2 ** attempt))        # 2, 4, 8, 16, 30 s
        raise RuntimeError(f"KMA request failed: {url} ({last!r})")   # url carries no key

    def _log(self, seconds: float, status) -> None:
        with self.lock:
            self.req_log.append((round(seconds, 2), status))

    def request_summary(self) -> dict:
        """Per-request timing for meta.json: count, failures by status, mean/median/max seconds of 200s."""
        ok = sorted(s for s, st in self.req_log if st == 200)
        bad: dict = {}
        for _, st in self.req_log:
            if st != 200:
                bad[str(st)] = bad.get(str(st), 0) + 1
        return {"requests": len(self.req_log), "failed": bad,
                "ok_mean_s": round(sum(ok) / len(ok), 2) if ok else None,
                "ok_median_s": ok[len(ok) // 2] if ok else None, "ok_max_s": ok[-1] if ok else None,
                "parallel": self.k.get("parallel", 4)}

    def _field(self, run, step, key: tuple[str, int], box) -> np.ndarray:
        name, level = key
        var = self.k["vars"][name]
        data = "U" if level == 0 else "P"
        if name == "gh" and level == 500 and self.k.get("wide_box_gh500"):
            box = self.k["wide_box_gh500"]          # WPSH search box needs gh500 far east of the map
        blob = self._get(self._url(run, step, data, var, None if level == 0 else level, box))
        with tempfile.NamedTemporaryFile(suffix=".grib2") as tf:
            tf.write(blob)
            tf.flush()
            g = read_single(tf.name)
        a = (place_regular(g, self.lat, self.lon) if g["gridType"] == "regular_ll"
             else lambert_to_regular(g, self.lat, self.lon))
        scale = self.k.get("scale", {}).get(name, 1.0)
        if scale != 1.0:
            a = a * scale
        if name == "q" and self.k.get("q_is_mixing_ratio"):
            a = a / (1.0 + a)                         # water-vapour mixing ratio -> specific humidity
        return a

    def field_keys(self, tp_only: bool = False) -> list[tuple[str, int]]:
        if tp_only:
            return [("tp", 0)]
        keys = []
        for g in self.cfg["data"]["groups"]:
            for p in g["param"]:
                for lv in (g.get("levelist") or [0]):
                    keys.append((p, lv if g["levtype"] == "pl" else 0))
        return keys

    # -------------------------------------------------------------------- Sources interface
    def run_complete(self, run: dt.datetime) -> str | None:
        """A run is complete when its last step's MSLP downloads (one small request), 00/12Z only."""
        if run.hour not in self.cfg["data"].get("run_hours", [0, 6, 12, 18]):
            return None
        age_h = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - run).total_seconds() / 3600
        if age_h < self.k["min_age_h"]:
            return None
        if age_h >= self.k.get("assume_complete_after_h", 12):
            return "kma"                              # old enough: no probe (a whole-field probe is 2.7 MB)
        last = self.cfg["data"]["max_step_main"]
        probes = self.usage.setdefault("probes", {})
        pk = f"{self.k['group']}{run:%Y%m%d%H}"
        now = time.time()
        if now - probes.get(pk, 0) < self.k.get("probe_every_min", 30) * 60:
            return None                               # asked recently: wait (each probe costs quota)
        probes[pk] = now
        try:
            self._get(self._url(run, last, "U", self.k["vars"]["msl"], None, self.k.get("probe_box")))   # KIMR: whole field
            return "kma"
        except QuotaError:
            raise
        except Exception as e:  # noqa: BLE001
            log.info("%s %s not complete yet: %s", self.k["group"], f"{run:%Y%m%d%H}", str(e)[:120])
            return None

    def fetch_step(self, run: dt.datetime, step: int, outdir, tp_only: bool = False) -> dict:
        outdir = pathlib.Path(outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()
        target = outdir / f"{run:%Y%m%d%H}_{step:03d}{'_tp' if tp_only else ''}.npz"
        data = {}
        if tp_only and (run, step) in self._tp:
            data[("tp", 0)] = self._tp[(run, step)]
        else:
            keys = self.field_keys(tp_only)
            box = self.k.get("box")
            with cf.ThreadPoolExecutor(self.k.get("parallel", 4)) as ex:
                for key, a in zip(keys, ex.map(lambda k: self._field(run, step, k, box), keys)):
                    data[key] = a
            if ("tp", 0) in data:
                data[("tp", 0)] = data[("tp", 0)] / 1000.0      # kg m-2 (mm) -> m, the IFS unit used downstream
                self._tp[(run, step)] = data[("tp", 0)]
            for (n, lv), a in data.items():
                if np.isfinite(a).any():
                    self.stats[f"{n}{lv or ''}"] = [round(float(np.nanmin(a)), 4), round(float(np.nanmax(a)), 4)]
        save_npz(target, self.lat, self.lon, data)
        return {"path": str(target), "bytes": target.stat().st_size, "seconds": time.monotonic() - t0,
                "source": "kma"}


# ------------------------------------------------------------------------------------------- daily usage
def usage_key(now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(KST)
    return f"_ops/kma-{now.astimezone(KST):%Y-%m-%d}.json"


def new_usage() -> dict:
    return {"calls": 0, "bytes": 0}


def load_usage(store) -> dict:
    u = store.get_json(usage_key())
    return u if u else new_usage()


def save_usage(store, usage: dict) -> None:
    store.put_bytes(usage_key(), json.dumps(usage).encode(), "no-cache")


def run_cost(cfg: dict, steps: int) -> tuple[int, float]:
    """(calls, bytes) estimated for one run, from the config's per-field size."""
    n = 0
    for g in cfg["data"]["groups"]:
        n += len(g["param"]) * len(g.get("levelist") or [0])
    return steps * n, steps * n * cfg["kma"]["est_bytes_per_field"]


def is_kma(cfg: dict) -> bool:
    return cfg["data"].get("source") == "kma"


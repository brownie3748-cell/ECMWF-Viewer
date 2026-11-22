"""Derived quantities: precipitation accumulation, moisture flux, extrema, WPSH centre."""

from __future__ import annotations

import numpy as np
from scipy import ndimage

EARTH_RADIUS_M = 6_371_000.0


def tp_accum_mm(tp_now_m: np.ndarray, tp_prev_m: np.ndarray) -> np.ndarray:
    """Accumulation between two steps of a field cumulative from step 0 (unit m -> mm)."""
    acc = (tp_now_m - tp_prev_m) * 1000.0
    return np.where(acc < 0, 0.0, acc)   # GRIB packing noise can give tiny negatives


def spherical_divergence(fu, fv, lat, lon):
    """div(F) on a regular lat/lon grid, 1/(a cos(phi)) [dFu/dlambda + d(Fv cos(phi))/dphi]."""
    phi = np.deg2rad(lat)[:, None]
    lam = np.deg2rad(lon)
    cos = np.cos(phi)
    dfu_dlam = np.gradient(fu, lam, axis=1)
    dfv_dphi = np.gradient(fv * cos, np.deg2rad(lat), axis=0)
    return (dfu_dlam + dfv_dphi) / (EARTH_RADIUS_M * cos)


def moisture_flux(q, u, v, lat, lon, level_hpa=None, sp_pa=None, smooth_sigma=0.0):
    """Return (qu, qv, div) with q in kg/kg, V in m/s, div in s^-1.

    Points where the level pressure exceeds surface pressure are set to NaN.
    """
    qu = q * u
    qv = q * v
    div = spherical_divergence(qu, qv, lat, lon)
    if smooth_sigma and smooth_sigma > 0:
        div = nan_gaussian(div, smooth_sigma)
    if level_hpa is not None and sp_pa is not None:
        under = sp_pa < level_hpa * 100.0
        div = np.where(under, np.nan, div)
        qu = np.where(under, np.nan, qu)
        qv = np.where(under, np.nan, qv)
    return qu, qv, div


def nan_gaussian(a: np.ndarray, sigma: float) -> np.ndarray:
    """Gaussian smoothing that ignores NaN (normalised convolution): data next to a domain edge or a masked
    area is smoothed with the valid neighbours only, instead of the NaN spreading inwards."""
    a = np.asarray(a, dtype=np.float64)
    ok = np.isfinite(a)
    if ok.all():
        return ndimage.gaussian_filter(a, sigma, mode="nearest")
    num = ndimage.gaussian_filter(np.where(ok, a, 0.0), sigma, mode="nearest")
    den = ndimage.gaussian_filter(ok.astype(np.float64), sigma, mode="nearest")
    with np.errstate(invalid="ignore", divide="ignore"):
        out = num / den
    out[~ok] = np.nan
    return out


def terrain_smooth(a: np.ndarray, msl_pa: np.ndarray, sp_pa: np.ndarray, sigma: float,
                   low_m: float = 150.0, high_m: float = 500.0) -> np.ndarray:
    """Smooth a field over mountains only: blend raw and smoothed by a terrain weight.

    Terrain height is estimated from msl - sp (about 12 Pa per metre near the ground): weight 0 below low_m,
    1 above high_m, linear between (itself smoothed so the blend has no edges). Plains and sea keep the raw
    field, so fronts and troughs there stay sharp; isobars over mountains lose their reduction-to-sea-level noise.
    """
    if sigma <= 0:
        return a
    h = (np.asarray(msl_pa, dtype=np.float64) - np.asarray(sp_pa, dtype=np.float64)) / 12.0
    w = np.clip((h - low_m) / (high_m - low_m), 0.0, 1.0)
    w = nan_gaussian(np.where(np.isfinite(w), w, 0.0), sigma)
    sm = nan_gaussian(a, sigma)
    return np.where(np.isfinite(sm), w * sm + (1.0 - w) * a, a)


def local_extrema(field, lat, lon, window_deg, grid_deg, kind="max", edge_exclude_deg=0.0,
                  box=None):
    """Local maxima/minima: value equals the max/min within +/- window_deg.

    Flat plateaus are reduced to one point. Returns a list of (lat, lon, value)
    sorted by value (descending for max, ascending for min).
    """
    size = int(round(2 * window_deg / grid_deg)) + 1
    filt = ndimage.maximum_filter if kind == "max" else ndimage.minimum_filter
    ext = filt(field, size=size, mode="nearest")
    mask = np.isfinite(field) & (field == ext)
    # a constant neighbourhood is not an extremum
    other = ndimage.minimum_filter if kind == "max" else ndimage.maximum_filter
    mask &= other(field, size=size, mode="nearest") != field
    labels, n = ndimage.label(mask)
    if box is None:
        box = dict(lat_min=lat[0], lat_max=lat[-1], lon_min=lon[0], lon_max=lon[-1])
    e = edge_exclude_deg
    out = []
    for idx in ndimage.find_objects(labels):
        iy = (idx[0].start + idx[0].stop - 1) // 2
        ix = (idx[1].start + idx[1].stop - 1) // 2
        la, lo = float(lat[iy]), float(lon[ix])
        if not (box["lat_min"] + e <= la <= box["lat_max"] - e):
            continue
        if not (box["lon_min"] + e <= lo <= box["lon_max"] - e):
            continue
        out.append((la, lo, float(field[iy, ix])))
    out.sort(key=lambda t: t[2], reverse=(kind == "max"))
    # quantised (equal) values can leave several non-adjacent points of one plateau;
    # keep only the strongest within +/- window_deg
    kept = []
    for c in out:
        if all(abs(c[0] - k[0]) > window_deg or abs(c[1] - k[1]) > window_deg for k in kept):
            kept.append(c)
    return kept


def wpsh_centre(gh500, lat, lon, cfg, grid_deg=0.25):
    """Working definition of the west-Pacific subtropical high edge (see docs/SPEC.md s5).

    `gh500` must cover the search box plus at least `local_window_deg` margin so the
    window test near the box edge sees real data.
    """
    w = cfg["wpsh"]
    box = w["search_box"]
    cands = local_extrema(gh500, lat, lon, w["local_window_deg"], grid_deg, kind="max",
                          edge_exclude_deg=w["edge_exclude_deg"], box=box)
    if not cands:
        return {"candidates": [], "H": None, "centre": None, "line_gpm": None}
    la, lo, h = cands[0]
    return {
        "candidates": cands,
        "H": h,
        "centre": (la, lo),
        "line_gpm": h - w["level_offset_gpm"],
    }


def relative_humidity(q: np.ndarray, t_k: np.ndarray, p_hpa: float) -> np.ndarray:
    """Relative humidity (%) from specific humidity (kg/kg) and temperature (K) on a pressure level.

    Saturation vapour pressure follows the IFS convention: water above 0 C, ice below -23 C and a
    quadratic blend in between (IFS documentation Part IV, section 7.2.1), so values are comparable
    with the IFS `r` field. Used for AIFS, which does not provide `r`.
    """
    t0, tice = 273.16, 250.16
    es_w = 611.21 * np.exp(17.502 * (t_k - t0) / (t_k - 32.19))
    es_i = 611.21 * np.exp(22.587 * (t_k - t0) / (t_k + 0.7))
    alpha = np.clip((t_k - tice) / (t0 - tice), 0.0, 1.0) ** 2
    es = alpha * es_w + (1.0 - alpha) * es_i
    p = p_hpa * 100.0
    e = q * p / (0.622 + 0.378 * q)                 # vapour pressure from specific humidity
    return (100.0 * e / es).astype(np.float32)


def ensure_rh(F, level: int) -> None:
    """Add ("r", level) to a grib.Fields from q and t when the model does not provide it."""
    if ("r", level) not in F.data and ("q", level) in F.data and ("t", level) in F.data:
        F.data[("r", level)] = relative_humidity(F.data[("q", level)], F.data[("t", level)], level)


def _es_hpa(t_c):
    """Saturation vapour pressure over water (Bolton 1980), hPa."""
    return 6.112 * np.exp(17.67 * t_c / (t_c + 243.5))


def wet_bulb_c(t_c: np.ndarray, td_c: np.ndarray, p_hpa: np.ndarray, iterations: int = 30) -> np.ndarray:
    """Psychrometric wet-bulb temperature (deg C) from temperature, dew point and pressure.

    Solves es(Tw) - A p (T - Tw) = e(Td) with A = 6.53e-4 (1 + 0.000944 Tw) per K (WMO psychrometer
    constant, ventilated) by bisection between Td and T; 30 halvings give < 1e-6 K."""
    t_c, td_c = np.asarray(t_c, float), np.minimum(np.asarray(td_c, float), t_c)
    e = _es_hpa(td_c)
    lo, hi = td_c.copy(), t_c.copy()
    for _ in range(iterations):
        mid = 0.5 * (lo + hi)
        f = _es_hpa(mid) - 6.53e-4 * (1 + 0.000944 * mid) * p_hpa * (t_c - mid) - e
        hi = np.where(f > 0, mid, hi)
        lo = np.where(f > 0, lo, mid)
    return 0.5 * (lo + hi)


def snow_area(tp_mm: np.ndarray, sf_mm: np.ndarray, min_mm: float, min_fraction: float) -> np.ndarray:
    """1 where the window's precipitation is at least min_mm and at least min_fraction of it fell as snow."""
    frac = np.divide(sf_mm, tp_mm, out=np.zeros_like(tp_mm), where=tp_mm > 0)
    return ((tp_mm >= min_mm) & (frac >= min_fraction)).astype(float)

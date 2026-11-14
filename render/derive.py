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
        div = ndimage.gaussian_filter(div, smooth_sigma, mode="nearest")
    if level_hpa is not None and sp_pa is not None:
        under = sp_pa < level_hpa * 100.0
        div = np.where(under, np.nan, div)
        qu = np.where(under, np.nan, qu)
        qv = np.where(under, np.nan, qv)
    return qu, qv, div


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

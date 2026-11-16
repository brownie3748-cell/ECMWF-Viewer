"""Matplotlib/cartopy chart rendering with a fixed pixel layout.

Every chart of a domain has the same figure size and map-axes position, so a
transparent overlay (wpsh500) lines up exactly with its base chart (gz500).
"""

from __future__ import annotations

import datetime as dt

import matplotlib

matplotlib.use("Agg")

import cartopy.crs as ccrs  # noqa: E402
import cartopy.feature as cfeature  # noqa: E402
import matplotlib.colors as mcolors  # noqa: E402
import matplotlib.patheffects as patheffects  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from cartopy.mpl.ticker import LatitudeFormatter, LongitudeFormatter  # noqa: E402
from scipy import ndimage  # noqa: E402

from . import derive  # noqa: E402

PC = ccrs.PlateCarree()
MS_TO_KT = 1.0 / 0.514444
_FEATURES: dict = {}


def _admin1(countries: tuple):
    """Natural Earth 10m admin-1 boundary lines of the given countries (ADM0_A3), cached."""
    key = ("admin1",) + countries
    if key not in _FEATURES:
        import cartopy.io.shapereader as shpreader
        r = shpreader.Reader(shpreader.natural_earth(resolution="10m", category="cultural",
                                                     name="admin_1_states_provinces_lines"))
        geoms = [x.geometry for x in r.records() if x.attributes.get("ADM0_A3") in countries]
        _FEATURES[key] = cfeature.ShapelyFeature(geoms, PC, facecolor="none")
    return _FEATURES[key]


def _features(res):
    if res not in _FEATURES:
        _FEATURES[res] = (
            cfeature.COASTLINE.with_scale(res),
            cfeature.BORDERS.with_scale(res),
        )
    return _FEATURES[res]


_GEOM: dict = {}


def domain_geometry(d: dict) -> dict:
    """Map projection, extent and lat/lon bounding box of a domain.

    A domain with `projection = "lcc"` is a rectangle in Lambert conformal x/y (metres, `extent_m`), e.g. the
    KIM regional grid; its lat/lon bounding box (for cropping data) is computed from the rectangle's edges.
    Other domains are lat/lon rectangles in plate carree.
    """
    key = repr(sorted((k, repr(v)) for k, v in d.items()))
    if key in _GEOM:
        return _GEOM[key]
    if d.get("projection") == "lcc":
        p = d["lcc"]
        crs = ccrs.LambertConformal(central_longitude=p["lon_0"], central_latitude=p["lat_0"],
                                    standard_parallels=(p["lat_1"], p["lat_2"]),
                                    globe=ccrs.Globe(semimajor_axis=p["radius"], semiminor_axis=p["radius"]))
        x0, x1, y0, y1 = d["extent_m"]
        t = np.linspace(0, 1, 200)
        xs = np.concatenate([x0 + (x1 - x0) * t, np.full(200, x1), x1 - (x1 - x0) * t, np.full(200, x0)])
        ys = np.concatenate([np.full(200, y0), y0 + (y1 - y0) * t, np.full(200, y1), y1 - (y1 - y0) * t])
        ll = PC.transform_points(crs, xs, ys)
        bbox = dict(lat_min=float(ll[:, 1].min()), lat_max=float(ll[:, 1].max()),
                    lon_min=float(ll[:, 0].min() % 360), lon_max=float(ll[:, 0].max() % 360))
        g = {"crs": crs, "extent": [x0, x1, y0, y1], "aspect": (y1 - y0) / (x1 - x0), "bbox": bbox, "lcc": True}
    else:
        g = {"crs": PC, "extent": [d["lon_min"], d["lon_max"], d["lat_min"], d["lat_max"]],
             "aspect": (d["lat_max"] - d["lat_min"]) / (d["lon_max"] - d["lon_min"]),
             "bbox": {k: d[k] for k in ("lat_min", "lat_max", "lon_min", "lon_max")}, "lcc": False}
    _GEOM[key] = g
    return g


def kst(t: dt.datetime, cfg) -> dt.datetime:
    return t + dt.timedelta(hours=cfg["style"]["kst_offset_h"])


class Frame:
    """One chart image of a given domain with the standard header/footer."""

    def __init__(self, cfg: dict, domain: str, transparent: bool = False, strong_coast: bool = False):
        self.cfg = cfg
        self.strong_coast = strong_coast
        self.geom = domain_geometry(cfg["domains"][domain])
        # lat/lon keys of self.dom are the bounding box (used to crop data), also for a Lambert domain
        self.dom = dict(cfg["domains"][domain], **self.geom["bbox"])
        self.domain = domain
        lay = cfg["layout"]
        st = cfg["style"]
        self.mw = self.dom["map_width_px"]
        self.mh = int(round(self.mw * self.geom["aspect"]))
        self.W = lay["margin_left_px"] + self.mw + lay["margin_right_px"]
        self.H = lay["margin_bottom_px"] + self.mh + lay["margin_top_px"]
        self.dpi = lay["dpi"]
        plt.rcParams["font.family"] = st["font_family"]
        self.fig = plt.figure(figsize=(self.W / self.dpi, self.H / self.dpi), dpi=self.dpi)
        self.transparent = transparent
        W, H = self.W, self.H
        self.ax = self.fig.add_axes(
            [lay["margin_left_px"] / W, lay["margin_bottom_px"] / H, self.mw / W, self.mh / H],
            projection=self.geom["crs"],
        )
        self.ax.set_extent(self.geom["extent"], crs=self.geom["crs"])
        x0 = (lay["margin_left_px"] + self.mw + lay["colorbar_pad_px"]) / W
        self.cax_rect = [x0, lay["margin_bottom_px"] / H, lay["colorbar_width_px"] / W, self.mh / H]
        if transparent:
            self.fig.patch.set_alpha(0.0)
            self.ax.patch.set_alpha(0.0)
            self.ax.axis("off")
        else:
            self._base()

    # ------------------------------------------------------------------ base
    def _base(self):
        st = self.cfg["style"]
        d = self.dom
        coast, borders = _features(st["coast_resolution"])
        sfx = "_strong" if self.strong_coast else ""
        self.ax.add_feature(coast, edgecolor=st["coast_color" + sfx], linewidth=st["coast_width" + sfx], zorder=3)
        self.ax.add_feature(borders, edgecolor=st["border_color" + sfx], linewidth=st["border_width" + sfx],
                            zorder=3)
        if st.get("admin1_countries"):
            self.ax.add_feature(_admin1(tuple(st["admin1_countries"])), edgecolor=st["admin1_color" + sfx],
                                linewidth=st["admin1_width" + sfx], zorder=3)
        g = d["grid_step_deg"]
        xt = np.arange(np.ceil(d["lon_min"] / g) * g, d["lon_max"] + 1e-6, g)
        yt = np.arange(np.ceil(d["lat_min"] / g) * g, d["lat_max"] + 1e-6, g)
        if self.geom["lcc"]:
            gl = self.ax.gridlines(crs=PC, xlocs=xt, ylocs=yt, color=st["grid_color"], linewidth=st["grid_width"],
                                   linestyle="--", zorder=2, draw_labels=True, x_inline=False, y_inline=False,
                                   rotate_labels=False)
            gl.top_labels = gl.right_labels = False
            gl.xformatter = LongitudeFormatter(degree_symbol="", dateline_direction_label=False)
            gl.yformatter = LatitudeFormatter(degree_symbol="")
            gl.xlabel_style = gl.ylabel_style = {"size": st["label_size"]}
            return
        self.ax.set_xticks(xt, crs=PC)
        self.ax.set_yticks(yt, crs=PC)
        self.ax.xaxis.set_major_formatter(LongitudeFormatter(degree_symbol="", dateline_direction_label=False))
        self.ax.yaxis.set_major_formatter(LatitudeFormatter(degree_symbol=""))
        self.ax.tick_params(labelsize=st["label_size"], length=3)
        self.ax.gridlines(xlocs=xt, ylocs=yt, color=st["grid_color"], linewidth=st["grid_width"],
                          linestyle="--", zorder=2)

    def inside(self, lon: float, lat: float, edge_frac: float = 0.0) -> bool:
        """True if (lon, lat) is on the map, at least edge_frac of the map size away from its edges."""
        x, y = self.geom["crs"].transform_point(lon, lat, PC)
        x0, x1, y0, y1 = self.geom["extent"]
        ex, ey = (x1 - x0) * edge_frac, (y1 - y0) * edge_frac
        return x0 + ex <= x <= x1 - ex and y0 + ey <= y <= y1 - ey

    def header(self, title: str, run: dt.datetime, step: int):
        st = self.cfg["style"]
        f = self.fig
        W, H = self.W, self.H
        L = self.cfg["layout"]["margin_left_px"]
        f.text(L / W, (H - 26) / H, title, fontsize=st["title_size"], fontweight="bold", ha="left", va="baseline")
        f.text((L + self.mw) / W, (H - 26) / H, st["model_label"], fontsize=st["label_size"] + 1,
               ha="right", va="baseline")
        valid = run + dt.timedelta(hours=step)
        t1 = f.text(L / W, 24 / H, f"VALID: {valid:%Y-%m-%d %H}UTC", color=st["utc_color"],
                    fontsize=st["footer_size"], fontweight="bold", ha="left", va="baseline")
        f.text((L + self.mw) / W, 24 / H, f"TIME: {run:%Y-%m-%d %H}UTC +{step:03d}h",
               fontsize=st["footer_size"], ha="right", va="baseline")
        self._kst_after(t1, f"  ({kst(valid, self.cfg):%Y-%m-%d %H}KST)", st["footer_size"])
        f.text(L / W, 7 / H, st["credit"], fontsize=st["credit_size"], color="#444444", ha="left", va="baseline")

    def _kst_after(self, t1, text, size):
        # place a black text immediately to the right of the red UTC text
        self.ax.annotate(text, xy=(1, 0), xycoords=t1, xytext=(0, 0), textcoords="offset points",
                         fontsize=size, ha="left", va="bottom", annotation_clip=False)

    def colorbar(self, mappable, label=None, ticks=None):
        cax = self.fig.add_axes(self.cax_rect)
        cb = self.fig.colorbar(mappable, cax=cax, ticks=ticks, format="%g")
        cb.ax.tick_params(labelsize=self.cfg["style"]["label_size"])
        if label:
            cb.set_label(label, fontsize=self.cfg["style"]["label_size"])
        return cb

    def png_bytes(self) -> bytes:
        import io

        buf = io.BytesIO()
        self.fig.savefig(buf, format="png", dpi=self.dpi, transparent=self.transparent)
        plt.close(self.fig)
        return buf.getvalue()


# ---------------------------------------------------------------------- helpers
def _sub(F, fr):
    d = fr.dom
    m = 1.0
    return F.crop(d["lat_min"] - m, d["lat_max"] + m, d["lon_min"] - m, d["lon_max"] + m)


def _levels(a, interval):
    lo = np.floor(np.nanmin(a) / interval) * interval
    hi = np.ceil(np.nanmax(a) / interval) * interval
    return np.arange(lo, hi + interval / 2, interval)


def _underground(F, level):
    return F[("sp", 0)] < level * 100.0


# ---------------------------------------------------------------------- charts
def _clabel(fr, cs, family="a", levels=None, **kw):
    """Contour labels at stable places: where each line crosses fixed meridians (config style.label_meridians).

    matplotlib's automatic placement moves labels from run to run and step to step; anchoring them on fixed
    meridians keeps them in a column (like the trough-axis labels of hand-drawn charts) and moves them only as
    far as the line itself moves. A line that crosses no anchor meridian (small closed centre) gets one label
    at its northernmost point (heights / isobars only, lines spanning > 2 deg). Labels closer than style.label_min_km to an earlier one are dropped.
    """
    st = fr.cfg["style"]
    lm = st["label_meridians"]
    key = next((k for k, d in fr.cfg["domains"].items() if k in lm and d is fr.cfg["domains"][fr.domain]
                and k != fr.domain), fr.domain)        # zoom maps are passed in as "default"
    lons = lm[key if key in lm else "default"][family]
    min_d = st.get("label_min_km", 90) * 1000.0
    proj = fr.ax.projection
    x0, x1, y0, y1 = fr.ax.get_extent(crs=proj)
    pad = 0.03 * (x1 - x0)
    want = None if levels is None else set(levels)
    pos, lv_used = [], []
    for lv, segs in zip(cs.levels, cs.allsegs):
        if want is not None and lv not in want:
            continue
        for seg in segs:
            if len(seg) < 8:
                continue
            lo, la = seg[:, 0], seg[:, 1]
            cand = []
            for m in lons:
                d = lo - m
                idx = np.nonzero((d[:-1] < 0) != (d[1:] < 0))[0]     # vertices exactly on the meridian count too
                if len(idx):
                    i = idx[np.argmin(la[idx])]             # southernmost crossing: trough bottom / ridge base
                    f = d[i] / (d[i] - d[i + 1]) if d[i] != d[i + 1] else 0.0
                    cand.append((m, la[i] + f * (la[i + 1] - la[i])))
            if not cand and family == "a" and (np.ptp(lo) > 2.0 or np.ptp(la) > 2.0):
                i = int(np.argmax(la))                       # closed centre / short line: one label on top
                cand.append((lo[i], la[i]))
            for clon, clat in cand:
                x, y = proj.transform_point(clon, clat, PC)
                if not (x0 + pad < x < x1 - pad and y0 + pad < y < y1 - pad):
                    continue
                if any((x - a) ** 2 + (y - b) ** 2 < min_d ** 2 for a, b in pos):
                    continue
                pos.append((x, y))
                lv_used.append(lv)
    if not pos:
        return None
    return fr.ax.clabel(cs, levels=sorted(set(lv_used)), manual=pos, **kw)


def _contourf(fr, S, z, **kw):
    """Filled contours computed in map x/y (transform_first). Reprojecting the filled polygons onto the
    Lambert map can invert one of them, which then floods the whole map with one colour (AIFS +192 h tp)."""
    lon2, lat2 = np.meshgrid(S.lon, S.lat)
    return fr.ax.contourf(lon2, lat2, z, transform_first=True, **kw)


def _tp_fill(cfg, fr, S, P):
    """Shade precipitation accumulated between P and S (mm) with the tp colour table + colour bar."""
    c = cfg["charts"]["tp"]
    acc = derive.tp_accum_mm(S[("tp", 0)], P[("tp", 0)])
    cmap = mcolors.ListedColormap(c["colors"][:-1])
    cmap.set_over(c["colors"][-1])
    norm = mcolors.BoundaryNorm(c["levels"], cmap.N)
    cf = _contourf(fr, S, acc, levels=c["levels"], cmap=cmap, norm=norm, extend="max",
                        transform=PC, zorder=1)
    fr.colorbar(cf, label="mm", ticks=c["levels"])
    return acc


def chart_msl(cfg, F, run, step, F_prev=None, window_h=None):
    """MSLP contours + H/L centres; with F_prev also the precipitation of the preceding window."""
    c = cfg["charts"]["msl"]
    fr = Frame(cfg, "default")
    S = _sub(F, fr)
    if F_prev is not None:
        _tp_fill(cfg, fr, S, _sub(F_prev, fr))
    p = S[("msl", 0)] / 100.0
    if c.get("smooth_sigma", 0) > 0:           # smooth the terrain noise of the isobars over mountains only
        p = derive.terrain_smooth(p, S[("msl", 0)], S[("sp", 0)], c["smooth_sigma"])
    lv = _levels(p, c["interval"])
    widths = [1.6 if (v % c["bold_every"] == 0) else 0.8 for v in lv]
    cs = fr.ax.contour(S.lon, S.lat, p, levels=lv, colors="k", linewidths=widths, transform=PC, zorder=4)
    _clabel(fr, cs, "a", levels=[v for v in cs.levels if v % c["label_every"] == 0], fmt="%d", fontsize=8,
                 inline_spacing=2)
    halo = [patheffects.withStroke(linewidth=3, foreground="white")]
    d = fr.dom
    for kind, sym, col in (("max", "H", "#0033cc"), ("min", "L", "#cc0000")):
        ext = derive.local_extrema(p, S.lat, S.lon, c["hl_window_deg"], cfg["data"]["grid_deg"], kind=kind,
                                   edge_exclude_deg=0.0 if fr.geom["lcc"] else c["hl_edge_exclude_deg"], box=d)
        if fr.geom["lcc"]:                       # curved lat/lon edges: test against the map rectangle itself
            ext = [e for e in ext if fr.inside(e[1], e[0], c.get("hl_edge_exclude_frac", 0.05))]
        for la, lo, val in ext:
            fr.ax.text(lo, la, sym, color=col, fontsize=16, fontweight="bold", ha="center", va="center",
                       transform=PC, zorder=6, path_effects=halo)
            fr.ax.text(lo, la - 0.9, f"{val:.0f}", color=col, fontsize=9, fontweight="bold", ha="center",
                       va="center", transform=PC, zorder=6, path_effects=halo)
    title = c["title_with_tp"].format(window=window_h) if F_prev is not None else c["title"]
    fr.header(title, run, step)
    return fr


def chart_tp(cfg, F_now, F_prev, run, step, window_h):
    """Precipitation only (kept for measurements; the published msl chart includes it)."""
    c = cfg["charts"]["tp"]
    fr = Frame(cfg, "default")
    acc = _tp_fill(cfg, fr, _sub(F_now, fr), _sub(F_prev, fr))
    title = c["title_short"] if window_h == cfg["data"]["tp_short_accum_h"] else c["title_long"]
    fr.header(title, run, step)
    return fr, acc


def temp_colormap(cfg):
    """Fixed temperature colour scale (style C): returns (cmap, norm)."""
    a = cfg["charts"]["gz"]["fill_c"]["anchors"]
    lo, hi = a[0][0], a[-1][0]
    cmap = mcolors.LinearSegmentedColormap.from_list(
        "temp_c", [((t - lo) / (hi - lo), col) for t, col in a], N=512)
    return cmap, mcolors.Normalize(vmin=lo, vmax=hi)


def temp_style_for(cfg, level):
    c = cfg["charts"]["gz"]
    return c.get("temp_style_by_level", {}).get(str(level), c["temp_style"])


def chart_gz(cfg, F, level, run, step, temp_style=None, domain="default"):
    c = cfg["charts"]["gz"]
    temp_style = temp_style or temp_style_for(cfg, level)
    lvkey = str(level)
    wind_fill = level in c.get("wind_fill", {}).get("levels", [])
    fr = Frame(cfg, domain, strong_coast=(temp_style == "C" or wind_fill))
    S = _sub(F, fr)
    gh = S[("gh", level)].copy()
    t = S[("t", level)] - 273.15
    u = S[("u", level)] * MS_TO_KT
    v = S[("v", level)] * MS_TO_KT
    if level in c["mask_underground"]:
        m = _underground(S, level)
        for a in (gh, t, u, v):
            a[m] = np.nan
    ti = c["t_interval"][lvkey]
    barb_color = "#1f3d7a"
    if temp_style == "C":
        fc = c["fill_c"]
        lo, hi = c["t_fill_range"][lvkey]
        cmap, norm = temp_colormap(cfg)
        tl = np.arange(lo, hi + fc["step"] / 2, fc["step"])
        cf = _contourf(fr, S, t, levels=tl, cmap=cmap, norm=norm, extend="both", transform=PC,
                            zorder=1)
        fr.colorbar(cf, label="deg C", ticks=[x for x in tl if x % 5 == 0])
        iso = _levels(t, ti)
        cs = fr.ax.contour(S.lon, S.lat, t, levels=iso, colors=fc["isotherm_color"],
                           linewidths=fc["isotherm_width"], transform=PC, zorder=2)
        _clabel(fr, cs, "b", fmt="%d", fontsize=7, colors=fc["isotherm_color"], inline_spacing=2)
        barb_color = fc["barb_color"]
    elif temp_style == "A":
        lo, hi = c["t_fill_range"][lvkey]
        tl = np.arange(lo, hi + ti / 2, ti)
        cf = _contourf(fr, S, t, levels=tl, cmap=c["t_cmap"], extend="both", transform=PC,
                            zorder=1, alpha=0.85)
        fr.colorbar(cf, label="deg C", ticks=tl[::2])
    else:
        iso_col = "#d00000"
        if wind_fill:
            wf = c["wind_fill"]
            spd = np.hypot(u, v)
            a = wf["anchors"]
            cmap = mcolors.LinearSegmentedColormap.from_list(
                "wspd", [(k / wf["max"], col) for k, col in a], N=512)
            cmap.set_over(a[-1][1])
            wl = np.arange(0, wf["max"] + wf["step"] / 2, wf["step"])
            cf = _contourf(fr, S, spd, levels=wl, cmap=cmap, vmin=0, vmax=wf["max"], extend="max",
                                transform=PC, zorder=1)
            fr.colorbar(cf, label="kt", ticks=wl[::5])
            iso_col = wf["isotherm_color"]
            barb_color = wf["barb_color"]
        tl = _levels(t, ti)
        cs = fr.ax.contour(S.lon, S.lat, t, levels=tl, colors=iso_col, linewidths=0.8, linestyles="--",
                           transform=PC, zorder=4)
        _clabel(fr, cs, "b", fmt="%d", fontsize=7, colors=iso_col)
    hi_iso = c.get("highlight_isotherm", {})
    hv = hi_iso.get("levels", {}).get(lvkey)
    if hv is not None and np.nanmin(t) < hv < np.nanmax(t):
        cs = fr.ax.contour(S.lon, S.lat, t, levels=[hv], colors=hi_iso["color"], linewidths=hi_iso["width"], linestyles="solid",
                           transform=PC, zorder=4.5)
        _clabel(fr, cs, "c", fmt="%d", fontsize=8, colors=hi_iso["color"])
    hl = _levels(gh, c["gh_interval"][lvkey])
    cs = fr.ax.contour(S.lon, S.lat, gh, levels=hl, colors="k", linewidths=1.1, transform=PC, zorder=5)
    _clabel(fr, cs, "a", fmt="%d", fontsize=8)
    n = fr.dom["barb_every"]
    sl = (slice(None, None, n), slice(None, None, n))
    fr.ax.barbs(S.lon[::n], S.lat[::n], u[sl], v[sl], length=5, linewidth=0.5, color=barb_color, alpha=0.7,
                transform=PC, zorder=6)
    ttl = (f"{level}hPa Height (gpm), Temperature (C), Wind Speed (kt)" if wind_fill
           else f"{level}hPa Geopotential Height (gpm), Temperature (C), Wind (kt)")
    fr.header(ttl, run, step)
    return fr


def chart_t2m(cfg, F, run, step):
    """2 m temperature: the fixed rainbow shading of the 925/850/700 hPa charts, labelled isotherms, 10 m wind."""
    c = cfg["charts"]["t2m"]
    fc = cfg["charts"]["gz"]["fill_c"]
    fr = Frame(cfg, "default", strong_coast=True)
    S = _sub(F, fr)
    t = S[("2t", 0)] - 273.15
    cmap, norm = temp_colormap(cfg)
    lo, hi = c["fill_range"]
    tl = np.arange(lo, hi + fc["step"] / 2, fc["step"])
    cf = _contourf(fr, S, t, levels=tl, cmap=cmap, norm=norm, extend="both", transform=PC, zorder=1)
    fr.colorbar(cf, label="deg C", ticks=[x for x in tl if x % 5 == 0])
    ts = derive.terrain_smooth(t, S[("msl", 0)], S[("sp", 0)], c.get("smooth_sigma", 0))   # lines only, mountains only
    cs = fr.ax.contour(S.lon, S.lat, ts, levels=_levels(ts, c["interval"]), colors=fc["isotherm_color"],
                       linewidths=fc["isotherm_width"], transform=PC, zorder=2)
    _clabel(fr, cs, "b", fmt="%d", fontsize=7, colors=fc["isotherm_color"], inline_spacing=2)
    hv = c.get("highlight")
    if hv is not None and np.nanmin(t) < hv < np.nanmax(t):
        cs = fr.ax.contour(S.lon, S.lat, t, levels=[hv], colors=cfg["charts"]["gz"]["highlight_isotherm"]["color"],
                           linewidths=1.4, linestyles="solid", transform=PC, zorder=4.5)
        _clabel(fr, cs, "a", fmt="%d", fontsize=8)
    n = fr.dom["barb_every"]
    sl = (slice(None, None, n), slice(None, None, n))
    u = S[("10u", 0)] * MS_TO_KT
    v = S[("10v", 0)] * MS_TO_KT
    fr.ax.barbs(S.lon[::n], S.lat[::n], u[sl], v[sl], length=5, linewidth=0.5, color=fc["barb_color"], alpha=0.7,
                transform=PC, zorder=6)
    fr.header(c["title"], run, step)
    return fr


def chart_rh700(cfg, F, run, step):
    c = cfg["charts"]["rh700"]
    fr = Frame(cfg, "default", strong_coast=True)
    S = _sub(F, fr)
    rh = np.clip(S[("r", 700)], 0, 100)   # IFS r can be <0 or >100 (ice saturation)
    a = c["anchors"]
    cmap = mcolors.LinearSegmentedColormap.from_list("rh", [(v / 100, col) for v, col in a], N=256)
    lv = np.arange(0, 100 + c["step"] / 2, c["step"])
    cf = _contourf(fr, S, rh, levels=lv, cmap=cmap, vmin=0, vmax=100, transform=PC, zorder=1)
    fr.colorbar(cf, label="%", ticks=lv[::2])
    gh = S[("gh", 700)]
    cs = fr.ax.contour(S.lon, S.lat, gh, levels=_levels(gh, cfg["charts"]["gz"]["gh_interval"]["700"]),
                       colors="k", linewidths=1.0, transform=PC, zorder=5)
    _clabel(fr, cs, "a", fmt="%d", fontsize=8)
    fr.header(c["title"], run, step)
    return fr


def chart_mfc(cfg, F, level, run, step):
    c = cfg["charts"]["mfc"]
    fr = Frame(cfg, "default")
    # compute on a slightly larger area so the gradient/smoothing edges fall outside the map
    d = fr.dom
    S = F.crop(d["lat_min"] - 3, d["lat_max"] + 3, d["lon_min"] - 3, d["lon_max"] + 3)
    qu, qv, div = derive.moisture_flux(S[("q", level)], S[("u", level)], S[("v", level)], S.lat, S.lon,
                                       level_hpa=level, sp_pa=S[("sp", 0)], smooth_sigma=c["smooth_sigma"])
    val = div * c["scale"]
    shade = np.where(val < 0, val, np.nan)
    sl = np.arange(c["shade_min"], 0 + 1e-9, c["shade_step"])
    cmap = mcolors.ListedColormap(list(reversed(c["shade_colors"])))
    cmap.set_under(c["shade_colors"][-1])
    norm = mcolors.BoundaryNorm(sl, cmap.N)
    cf = _contourf(fr, S, shade, levels=sl, cmap=cmap, norm=norm, extend="min", transform=PC,
                        zorder=1)
    fr.colorbar(cf, ticks=sl)
    ci = c["contour_interval"]
    vmax = np.nanmax(val) if np.isfinite(val).any() else 0
    pl = np.arange(0, max(ci, vmax) + ci, ci)
    # thin grey divergence contours drawn below coastlines/borders (zorder 3) so they do not hide them
    cs = fr.ax.contour(S.lon, S.lat, val, levels=pl, colors=c["contour_color"], linewidths=c["contour_width"],
                       transform=PC, zorder=2.5)
    _clabel(fr, cs, "b", fmt="%d", fontsize=6.5, colors=c["contour_color"])
    n = fr.dom["vector_every"]
    s = (slice(None, None, n), slice(None, None, n))
    q = fr.ax.quiver(S.lon[::n], S.lat[::n], qu[s], qv[s], color=c["vector_color"], scale=c["vector_scale"],
                     width=0.0018, headwidth=3.5, transform=PC, zorder=6)
    fr.ax.quiverkey(q, 0.92, 1.015, c["vector_ref"], f"{c['vector_ref']} m/s", labelpos="E",
                    coordinates="axes", fontproperties={"size": 8})
    fr.header(c["title"].format(level=level), run, step)
    return fr, val


def overlay_wpsh(cfg, F, run, step, domain="default"):
    """Transparent PNG with only the red H-50 contour and its legend.

    Runs whose hour is not in wpsh.run_hours (06/18Z) get no line; the legend says so and
    the returned result has drawn=False.
    """
    w = cfg["wpsh"]
    fr = Frame(cfg, domain, transparent=True)
    if run.hour not in w.get("run_hours", [0, 6, 12, 18]):
        fr.ax.text(0.99, 0.985, "WPSH  line: 00/12 UTC runs only", transform=fr.ax.transAxes, ha="right",
                   va="top", fontsize=9, color="#666666", zorder=8,
                   bbox=dict(boxstyle="round,pad=0.25", fc="white", ec="#999999", lw=0.8, alpha=0.9))
        return fr, {"candidates": [], "H": None, "centre": None, "line_gpm": None, "drawn": False}
    box = w["search_box"]
    mg = w["local_window_deg"] + 1
    B = F.crop(box["lat_min"] - mg, box["lat_max"] + mg, box["lon_min"] - mg, box["lon_max"] + mg)
    res = derive.wpsh_centre(B[("gh", 500)], B.lat, B.lon, cfg, cfg["data"]["grid_deg"])
    if res["line_gpm"] is not None:
        S = _sub(F, fr)
        cs = fr.ax.contour(S.lon, S.lat, S[("gh", 500)], levels=[res["line_gpm"]], colors=w["line_color"],
                           linewidths=w["line_width"], transform=PC, zorder=7)
        # white halo keeps the red line readable over the warm colour fill of gz500 (style A)
        cs.set(path_effects=[patheffects.Stroke(linewidth=w["line_width"] + 2.5, foreground="white"),
                             patheffects.Normal()])
        txt = f"WPSH  H={res['H']:.0f}  line={res['line_gpm']:.0f} gpm"
    else:
        txt = "WPSH  no centre"
    res["drawn"] = res["line_gpm"] is not None
    fr.ax.text(0.99, 0.985, txt, transform=fr.ax.transAxes, ha="right", va="top", fontsize=9,
               color=w["line_color"], fontweight="bold", zorder=8,
               bbox=dict(boxstyle="round,pad=0.25", fc="white", ec=w["line_color"], lw=0.8, alpha=0.9))
    return fr, res

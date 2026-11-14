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

from . import derive  # noqa: E402

PC = ccrs.PlateCarree()
MS_TO_KT = 1.0 / 0.514444
_FEATURES: dict = {}


def _features(res):
    if res not in _FEATURES:
        _FEATURES[res] = (
            cfeature.COASTLINE.with_scale(res),
            cfeature.BORDERS.with_scale(res),
        )
    return _FEATURES[res]


def kst(t: dt.datetime, cfg) -> dt.datetime:
    return t + dt.timedelta(hours=cfg["style"]["kst_offset_h"])


class Frame:
    """One chart image of a given domain with the standard header/footer."""

    def __init__(self, cfg: dict, domain: str, transparent: bool = False):
        self.cfg = cfg
        self.dom = cfg["domains"][domain]
        self.domain = domain
        lay = cfg["layout"]
        st = cfg["style"]
        d = self.dom
        self.mw = d["map_width_px"]
        self.mh = int(round(self.mw * (d["lat_max"] - d["lat_min"]) / (d["lon_max"] - d["lon_min"])))
        self.W = lay["margin_left_px"] + self.mw + lay["margin_right_px"]
        self.H = lay["margin_bottom_px"] + self.mh + lay["margin_top_px"]
        self.dpi = lay["dpi"]
        plt.rcParams["font.family"] = st["font_family"]
        self.fig = plt.figure(figsize=(self.W / self.dpi, self.H / self.dpi), dpi=self.dpi)
        self.transparent = transparent
        W, H = self.W, self.H
        self.ax = self.fig.add_axes(
            [lay["margin_left_px"] / W, lay["margin_bottom_px"] / H, self.mw / W, self.mh / H],
            projection=PC,
        )
        self.ax.set_extent([d["lon_min"], d["lon_max"], d["lat_min"], d["lat_max"]], crs=PC)
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
        self.ax.add_feature(coast, edgecolor=st["coast_color"], linewidth=st["coast_width"], zorder=3)
        self.ax.add_feature(borders, edgecolor=st["border_color"], linewidth=st["border_width"], zorder=3)
        g = d["grid_step_deg"]
        xt = np.arange(np.ceil(d["lon_min"] / g) * g, d["lon_max"] + 1e-6, g)
        yt = np.arange(np.ceil(d["lat_min"] / g) * g, d["lat_max"] + 1e-6, g)
        self.ax.set_xticks(xt, crs=PC)
        self.ax.set_yticks(yt, crs=PC)
        self.ax.xaxis.set_major_formatter(LongitudeFormatter(degree_symbol="", dateline_direction_label=False))
        self.ax.yaxis.set_major_formatter(LatitudeFormatter(degree_symbol=""))
        self.ax.tick_params(labelsize=st["label_size"], length=3)
        self.ax.gridlines(xlocs=xt, ylocs=yt, color=st["grid_color"], linewidth=st["grid_width"],
                          linestyle="--", zorder=2)

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
        cb = self.fig.colorbar(mappable, cax=cax, ticks=ticks)
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
def chart_msl(cfg, F, run, step):
    c = cfg["charts"]["msl"]
    fr = Frame(cfg, "default")
    S = _sub(F, fr)
    p = S[("msl", 0)] / 100.0
    lv = _levels(p, c["interval"])
    widths = [1.6 if (v % c["bold_every"] == 0) else 0.8 for v in lv]
    cs = fr.ax.contour(S.lon, S.lat, p, levels=lv, colors="k", linewidths=widths, transform=PC, zorder=4)
    fr.ax.clabel(cs, fmt="%d", fontsize=8, inline_spacing=2)
    d = fr.dom
    for kind, sym, col in (("max", "H", "#0033cc"), ("min", "L", "#cc0000")):
        ext = derive.local_extrema(p, S.lat, S.lon, c["hl_window_deg"], cfg["data"]["grid_deg"], kind=kind,
                                   edge_exclude_deg=c["hl_edge_exclude_deg"], box=d)
        for la, lo, val in ext:
            fr.ax.text(lo, la, sym, color=col, fontsize=16, fontweight="bold", ha="center", va="center",
                       transform=PC, zorder=6)
            fr.ax.text(lo, la - 0.9, f"{val:.0f}", color=col, fontsize=9, fontweight="bold", ha="center",
                       va="center", transform=PC, zorder=6)
    fr.header(c["title"], run, step)
    return fr


def chart_tp(cfg, F_now, F_prev, run, step, window_h):
    c = cfg["charts"]["tp"]
    fr = Frame(cfg, "default")
    S, P = _sub(F_now, fr), _sub(F_prev, fr)
    acc = derive.tp_accum_mm(S[("tp", 0)], P[("tp", 0)])
    cmap = mcolors.ListedColormap(c["colors"][:-1])
    cmap.set_over(c["colors"][-1])
    norm = mcolors.BoundaryNorm(c["levels"], cmap.N)
    cf = fr.ax.contourf(S.lon, S.lat, acc, levels=c["levels"], cmap=cmap, norm=norm, extend="max",
                        transform=PC, zorder=1)
    fr.colorbar(cf, label="mm", ticks=c["levels"])
    title = c["title_short"] if window_h == cfg["data"]["tp_short_accum_h"] else c["title_long"]
    fr.header(title, run, step)
    return fr, acc


def temp_style_for(cfg, level):
    c = cfg["charts"]["gz"]
    return c.get("temp_style_by_level", {}).get(str(level), c["temp_style"])


def chart_gz(cfg, F, level, run, step, temp_style=None, domain="default"):
    c = cfg["charts"]["gz"]
    temp_style = temp_style or temp_style_for(cfg, level)
    lvkey = str(level)
    fr = Frame(cfg, domain)
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
    if temp_style == "A":
        lo, hi = c["t_fill_range"][lvkey]
        tl = np.arange(lo, hi + ti / 2, ti)
        cf = fr.ax.contourf(S.lon, S.lat, t, levels=tl, cmap=c["t_cmap"], extend="both", transform=PC,
                            zorder=1, alpha=0.85)
        fr.colorbar(cf, label="deg C", ticks=tl[::2])
    else:
        tl = _levels(t, ti)
        cs = fr.ax.contour(S.lon, S.lat, t, levels=tl, colors="#d00000", linewidths=0.8, linestyles="--",
                           transform=PC, zorder=4)
        fr.ax.clabel(cs, fmt="%d", fontsize=7, colors="#d00000")
    hl = _levels(gh, c["gh_interval"][lvkey])
    cs = fr.ax.contour(S.lon, S.lat, gh, levels=hl, colors="k", linewidths=1.1, transform=PC, zorder=5)
    fr.ax.clabel(cs, fmt="%d", fontsize=8)
    n = fr.dom["barb_every"]
    sl = (slice(None, None, n), slice(None, None, n))
    fr.ax.barbs(S.lon[::n], S.lat[::n], u[sl], v[sl], length=5, linewidth=0.5, color="#1f3d7a",
                transform=PC, zorder=6)
    ttl = f"{level}hPa Geopotential Height (gpm), Temperature (C), Wind (kt)"
    fr.header(ttl, run, step)
    return fr


def chart_rh700(cfg, F, run, step):
    c = cfg["charts"]["rh700"]
    fr = Frame(cfg, "default")
    S = _sub(F, fr)
    rh = np.clip(S[("r", 700)], 0, 100)   # IFS r can be <0 or >100 (ice saturation)
    cf = fr.ax.contourf(S.lon, S.lat, rh, levels=c["levels"], colors=c["colors"], extend="neither",
                        transform=PC, zorder=1)
    fr.colorbar(cf, label="%", ticks=c["levels"])
    gh = S[("gh", 700)]
    cs = fr.ax.contour(S.lon, S.lat, gh, levels=_levels(gh, cfg["charts"]["gz"]["gh_interval"]["700"]),
                       colors="k", linewidths=1.0, transform=PC, zorder=5)
    fr.ax.clabel(cs, fmt="%d", fontsize=8)
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
    cf = fr.ax.contourf(S.lon, S.lat, shade, levels=sl, cmap=cmap, norm=norm, extend="min", transform=PC,
                        zorder=1)
    fr.colorbar(cf, ticks=sl)
    ci = c["contour_interval"]
    vmax = np.nanmax(val) if np.isfinite(val).any() else 0
    pl = np.arange(0, max(ci, vmax) + ci, ci)
    # thin grey divergence contours drawn below coastlines/borders (zorder 3) so they do not hide them
    cs = fr.ax.contour(S.lon, S.lat, val, levels=pl, colors=c["contour_color"], linewidths=c["contour_width"],
                       transform=PC, zorder=2.5)
    fr.ax.clabel(cs, fmt="%d", fontsize=6.5, colors=c["contour_color"])
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

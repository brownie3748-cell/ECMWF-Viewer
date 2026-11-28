"""강수 비교 (owner 2026-11-28): observed radar accumulation next to the models' and the KMA forecast's rainfall.

- Radar: the Radar Center's own accumulation image (radar.kma.go.kr 누적, 1 h / 3 h), requested for exactly the KIM
  Korea map (config [compare]; geometry measured in M10) and put on that map with our coastlines, the radar's colour
  bar and its credit. One frame per window and hour (KST, on the hour), the last `keep_hours` hours.
- Models: drawn by the render pipeline (pipeline.compare_charts, cmp<window>_<step> in each run); not here.
- KMA forecast (단기 PCP / 초단기 RN1 grids from the API Hub): drawn on the same map in the same colours, per
  issuance and valid hour (see forecast_frames).

Everything goes under compare/ with compare/index.json; `python -m render compare` (forecast.yml) keeps it current.
Measured numbers are in docs/STAGE3.md.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import logging
import time

import numpy as np
import requests

from . import encode, plot

log = logging.getLogger(__name__)
KST = dt.timezone(dt.timedelta(hours=9))
# The radar image's map: LCC 30/60N, 126E, lat_0 38N on WGS84 (radar.kma.go.kr EPSG:980201; measured M10)
RADAR_PROJ = "+proj=lcc +lat_1=30 +lat_2=60 +lat_0=38 +lon_0=126 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"


def kst_now() -> dt.datetime:
    return dt.datetime.now(KST).replace(tzinfo=None, second=0, microsecond=0)


def radar_box(cfg: dict) -> dict:
    """The KIM Korea map's rectangle in the radar image's projection: extent (x0, x1, y0, y1 in m), the upper-left
    corner's lon / lat and the image size in pixels (the map area of a chart)."""
    import cartopy.crs as ccrs
    from pyproj import CRS, Transformer
    c = cfg["compare"]
    geom = plot.domain_geometry(cfg["domains"][c["domain"]])
    x0, x1, y0, y1 = geom["extent"]
    ours = CRS.from_user_input(geom["crs"].proj4_init)
    rp = CRS.from_proj4(RADAR_PROJ)
    t = Transformer.from_crs(ours, rp, always_xy=True)
    xs, ys = t.transform([x0, x0, x1, x1], [y0, y1, y0, y1])
    ex = [float(np.mean([xs[0], xs[1]])), float(np.mean([xs[2], xs[3]])),
          float(np.mean([ys[0], ys[2]])), float(np.mean([ys[1], ys[3]]))]
    to_ll = Transformer.from_crs(rp, "EPSG:4326", always_xy=True)
    lu, rl = to_ll.transform(ex[0], ex[3]), to_ll.transform(ex[1], ex[2])
    w = int(cfg["domains"][c["domain"]]["map_width_px"])
    h = int(round(w * (y1 - y0) / (x1 - x0)))
    crs = ccrs.LambertConformal(central_longitude=126.0, central_latitude=38.0, standard_parallels=(30.0, 60.0),
                                globe=ccrs.Globe(datum="WGS84", ellipse="WGS84"))
    return {"extent": ex, "lu": lu, "rl": rl, "w": w, "h": h, "crs": crs}


def radar_url(cfg: dict, valid_kst: dt.datetime, window_h: int) -> str:
    """The Radar Center's accumulation image (`window_h` hours ending at valid_kst) for the KIM map's rectangle."""
    b = radar_box(cfg)
    x0, x1, y0, y1 = b["extent"]
    return cfg["compare"]["radar_url"].format(
        acc=window_h * 60, tm=f"{valid_kst:%Y%m%d%H%M}", lu_lon=f"{b['lu'][0]:.5f}", lu_lat=f"{b['lu'][1]:.5f}",
        rl_lon=f"{b['rl'][0]:.5f}", rl_lat=f"{b['rl'][1]:.5f}",
        x_dist=int(round(x1 - x0)), y_dist=int(round(y1 - y0)), w=b["w"], h=b["h"])


def _header(cfg, fr, title, right, valid_kst, credit):
    st = cfg["style"]
    f, W, H = fr.fig, fr.W, fr.H
    L = cfg["layout"]["margin_left_px"]
    f.text(L / W, (H - 26) / H, title, fontsize=st["title_size"], fontweight="bold", ha="left", va="baseline")
    f.text((L + fr.mw) / W, (H - 26) / H, right, fontsize=st["label_size"] + 1, ha="right", va="baseline")
    utc = valid_kst - dt.timedelta(hours=9)
    f.text(L / W, 24 / H, f"VALID: {utc:%Y-%m-%d %H}UTC ({valid_kst:%Y-%m-%d %H}KST)", color=st["utc_color"],
           fontsize=st["footer_size"], fontweight="bold", ha="left", va="baseline")
    f.text(L / W, 7 / H, credit, fontsize=st["credit_size"], color="#444444", ha="left", va="baseline")


def radar_frame(cfg: dict, png: bytes, valid_kst: dt.datetime, window_h: int) -> bytes:
    """The radar image on the KIM Korea map (our coastlines, the radar's colour bar), encoded like the charts."""
    from PIL import Image
    c = cfg["compare"]
    b = radar_box(cfg)
    im = np.asarray(Image.open(io.BytesIO(png)).convert("RGBA"))
    fr = plot.Frame(cfg, c["domain"], strong_coast=True)
    x0, x1, y0, y1 = b["extent"]
    fr.ax.imshow(im, extent=(x0, x1, y0, y1), transform=b["crs"], origin="upper", interpolation="nearest",
                 zorder=1, regrid_shape=(b["h"], b["w"]))
    cmap, norm, lv = plot.compare_cmap(cfg)
    import matplotlib.cm as mcm
    fr.colorbar(mcm.ScalarMappable(norm=norm, cmap=cmap), label="mm", ticks=lv)
    _header(cfg, fr, c["radar_title"].format(window=window_h), c["radar_label"], valid_kst, c["radar_credit"])
    fmt = cfg["output"]["format"]
    return encode.encode(fr.png_bytes(), fmt, cfg["output"]["png8_colors"], cfg["output"]["webp_quality"])


def fetch_radar(cfg: dict, valid_kst: dt.datetime, window_h: int, session=None) -> bytes | None:
    s = session or requests
    u = radar_url(cfg, valid_kst, window_h)
    for attempt in range(3):
        try:
            r = s.get(u, timeout=cfg["compare"]["timeout_s"])
            if r.status_code == 200 and r.content[:4] == b"\x89PNG":
                return r.content
            log.warning("radar %s %dh: HTTP %s %r", f"{valid_kst:%m%d%H}", window_h, r.status_code, r.content[:80])
        except Exception as e:  # noqa: BLE001
            log.warning("radar %s %dh: %r", f"{valid_kst:%m%d%H}", window_h, e)
        time.sleep(3 * (attempt + 1))
    return None


def radar_key(cfg: dict, window_h: int, valid_kst: dt.datetime) -> str:
    ext = encode.EXT[cfg["output"]["format"]]
    return f"{cfg['compare']['prefix']}radar/{window_h}h/{valid_kst:%Y%m%d%H}.{ext}"


def wanted_hours(cfg: dict, now_kst: dt.datetime) -> list[dt.datetime]:
    """Hours (KST, on the hour) to keep: the newest one the radar has had time to finish, back keep_hours."""
    c = cfg["compare"]
    last = (now_kst - dt.timedelta(minutes=c["radar_delay_min"])).replace(minute=0)
    return [last - dt.timedelta(hours=i) for i in range(c["keep_hours"])][::-1]


def run(cfg: dict, st, now: dt.datetime | None = None) -> list[str]:
    """Bring compare/ up to date: missing radar frames of the last keep_hours, prune older ones, write the index."""
    from . import forecast_grid
    c = cfg["compare"]
    now = now or kst_now()
    man_key = c["prefix"] + "index.json"
    man = st.get_json(man_key) or {}
    radar = {w: dict(man.get("radar", {}).get(str(w), {})) for w in c["windows"]}
    hours = wanted_hours(cfg, now)
    keep = {f"{h:%Y%m%d%H}" for h in hours}
    sizes = st.list_sizes()
    used = sum(sizes.values())
    session = requests.Session()
    lines, new, t0 = [], 0, time.monotonic()
    for w in (c["windows"] if c.get("radar_enabled", False) else []):
        for h in hours:
            k = f"{h:%Y%m%d%H}"
            if k in radar[w]:
                continue
            png = fetch_radar(cfg, h, w, session)
            if png is None:
                continue
            data = radar_frame(cfg, png, h, w)
            if used + len(data) > cfg["storage"]["max_bytes"]:
                lines.append("- radar: storage cap reached, stopped")
                break
            key = radar_key(cfg, w, h)
            st.check_budget(1)
            st.put_bytes(key, data, cfg["storage"]["image_cache_control"])
            used += len(data)
            radar[w][k] = key.split("/", 1)[1]
            new += 1
    gone = []
    for w in c["windows"]:
        for k in [k for k in radar[w] if k not in keep]:
            gone.append(c["prefix"] + radar[w].pop(k))
    fc_lines, fcst = forecast_grid.run(cfg, st, man.get("forecast", {}), hours, sizes)
    lines += fc_lines
    if gone:
        st.delete_keys(gone, sizes)
    m = {"schema": 1, "generated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "tz": "KST", "windows": c["windows"], "hours": [f"{h:%Y%m%d%H}" for h in hours],
         "radar": {str(w): dict(sorted(radar[w].items())) for w in c["windows"]},
         "radar_credit": c["radar_credit"], "forecast": fcst, "forecast_credit": c["forecast"]["credit"],
         "ext": encode.EXT[cfg["output"]["format"]],
         "models": {m_: ws for m_, ws in c["models"].items()}}
    st.put_bytes(man_key, json.dumps(m, ensure_ascii=False, separators=(",", ":")).encode(), "no-cache, max-age=0")
    lines.insert(0, f"- radar: {new} new frames, {len(gone)} pruned, {time.monotonic() - t0:.0f} s (measured); "
                    f"hours {hours[0]:%m-%d %H} .. {hours[-1]:%m-%d %H} KST")
    return lines

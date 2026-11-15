"""Pipeline: render every published step of one run of one model into a local staging directory.

Downloads run in the main process (one step ahead of rendering); rendering and encoding run in
a process pool. GRIB files are deleted as soon as a step is rendered, so disk use stays small.
"""

from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import json
import logging
import pathlib
import shutil
import time

from . import config as config_mod
from . import derive, encode, fetch, grib, plot

log = logging.getLogger(__name__)

_CFG: dict | None = None


def _init_worker(cfg_path: str | None, model_id: str):
    global _CFG
    _CFG = config_mod.for_model(config_mod.load(cfg_path), model_id)


def chart_jobs(cfg, F, P, run, step):
    """{chart_id: callable returning a plot.Frame} for one step (msl includes precipitation when P is given)."""
    window = fetch.tp_window(step, cfg)
    jobs = {
        "msl": lambda: plot.chart_msl(cfg, F, run, step, P, window if P is not None else None),
        "rh700": lambda: plot.chart_rh700(cfg, F, run, step),
        "mfc925": lambda: plot.chart_mfc(cfg, F, 925, run, step)[0],
        "mfc850": lambda: plot.chart_mfc(cfg, F, 850, run, step)[0],
        "gz500_wide": lambda: plot.chart_gz(cfg, F, 500, run, step, domain="wide"),
    }
    for lv in (925, 850, 700, 500, 300, 200):
        jobs[f"gz{lv}"] = (lambda lv=lv: plot.chart_gz(cfg, F, lv, run, step))
    return jobs


def render_step(run_s: str, step: int, grib_path: str, tp_prev_path: str | None, outdir: str) -> dict:
    """Worker: render all charts of one step, write images, delete the GRIB inputs."""
    cfg = _CFG
    run = dt.datetime.strptime(run_s, "%Y%m%d%H")
    out = pathlib.Path(outdir)
    t0 = time.monotonic()
    fmt = cfg["output"]["format"]
    F = grib.read(grib_path, cfg["data"]["read_region"])
    P = grib.read(tp_prev_path, cfg["data"]["read_region"]) if tp_prev_path else None
    derive.ensure_rh(F, 700)
    sizes = {}
    want = [c["id"] for c in cfg["site"]["charts"]]

    def save(chart_id, frame):
        data = encode.encode(frame.png_bytes(), fmt, cfg["output"]["png8_colors"], cfg["output"]["webp_quality"])
        path = out / cfg["site"]["path"].format(model=cfg["model_id"], run=run_s, chart=chart_id,
                                                step=step).split("/", 2)[-1]
        path.write_bytes(data)
        sizes[chart_id] = len(data)

    jobs = chart_jobs(cfg, F, P, run, step)
    for cid in want:
        if cid in jobs:
            save(cid, jobs[cid]())
    fr, res = plot.overlay_wpsh(cfg, F, run, step)     # also gives H / H-50 for the manifest
    if "wpsh500" in want:
        save("wpsh500", fr)
    if "wpsh500_wide" in want:
        save("wpsh500_wide", plot.overlay_wpsh(cfg, F, run, step, "wide")[0])
    for p in (grib_path, tp_prev_path):
        if p:
            pathlib.Path(p).unlink(missing_ok=True)
    wpsh = [round(res["H"]), round(res["line_gpm"])] if res.get("drawn") else None
    return {"step": step, "seconds": time.monotonic() - t0, "bytes": sum(sizes.values()),
            "images": len(sizes), "wpsh": wpsh}


def render_run(cfg: dict, sources, run: dt.datetime, site: pathlib.Path, work: pathlib.Path,
               steps: list[int] | None = None, cfg_path: str | None = None) -> dict:
    """Render one run of cfg's model into site/<model>/<run>/ and write its meta.json (written last)."""
    run_s = f"{run:%Y%m%d%H}"
    model = cfg["model_id"]
    rdir = site / model / run_s
    if rdir.exists():
        shutil.rmtree(rdir)          # partial leftovers from an interrupted job
    rdir.mkdir(parents=True)
    gdir = work / "grib" / model / run_s
    gdir.mkdir(parents=True, exist_ok=True)
    steps = steps if steps is not None else fetch.published_steps(run.hour, cfg)
    t0 = time.monotonic()
    results, dl_seconds, srcs = [], 0.0, set()
    with cf.ProcessPoolExecutor(cfg["site"]["workers"], initializer=_init_worker,
                                initargs=(cfg_path, model)) as pool:
        futures = []
        for step in steps:
            r = sources.fetch_step(run, step, gdir)
            dl_seconds += r["seconds"]
            srcs.add(r["source"])
            tp_prev = None
            if step > 0:
                rp = sources.fetch_step(run, step - fetch.tp_window(step, cfg), gdir, tp_only=True)
                dl_seconds += rp["seconds"]
                tp_prev = rp["path"]
            futures.append(pool.submit(render_step, run_s, step, r["path"], tp_prev, str(rdir)))
            # keep at most 2 x workers steps of GRIB on disk
            while sum(not f.done() for f in futures) > 2 * cfg["site"]["workers"]:
                time.sleep(0.2)
        for f in futures:
            results.append(f.result())
    shutil.rmtree(gdir, ignore_errors=True)
    results.sort(key=lambda r: r["step"])
    wpsh = None
    if run.hour in cfg["wpsh"]["run_hours"]:
        wpsh = {str(r["step"]): r["wpsh"] for r in results}
    meta = {
        "model": model,
        "run": run_s,
        "steps": [r["step"] for r in results],
        "wpsh": wpsh,
        "render_version": cfg["site"]["render_version"],
        "rendered_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": sorted(srcs),
        "images": sum(r["images"] for r in results),
        "bytes": sum(r["bytes"] for r in results),
        "download_seconds": round(dl_seconds, 1),
        "wall_seconds": round(time.monotonic() - t0, 1),
        "render_seconds_sum": round(sum(r["seconds"] for r in results), 1),
    }
    if getattr(sources, "stats", None):
        meta["field_ranges"] = dict(sources.stats)      # KIM: min/max per field, a units sanity check
    (rdir / "meta.json").write_text(json.dumps(meta, indent=1))
    log.info("%s run %s: %d images, %.0f MB, wall %.0f s", model, run_s, meta["images"], meta["bytes"] / 1e6,
             meta["wall_seconds"])
    return meta

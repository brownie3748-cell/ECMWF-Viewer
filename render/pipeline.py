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


class Deferred(RuntimeError):
    """The job's time limit came before the run was finished; the rendered steps are kept for the next poll."""

_CFG: dict | None = None


def _init_worker(cfg_path: str | None, model_id: str):
    global _CFG
    _CFG = config_mod.for_model(config_mod.load(cfg_path), model_id)


def chart_jobs(cfg, F, P, run, step, clim=None):
    """{chart_id: callable returning a plot.Frame} for one step (msl includes precipitation when P is given;
    the 편차 maps a_* only with the climatology of the valid time, see render.clim.grid_at)."""
    window = fetch.tp_window(step, cfg)
    jobs = {
        "msl": lambda: plot.chart_msl(cfg, F, run, step, P, window if P is not None else None),
        "rh700": lambda: plot.chart_rh700(cfg, F, run, step),
        "t2m": lambda: plot.chart_t2m(cfg, F, run, step),
        "mfc925": lambda: plot.chart_mfc(cfg, F, 925, run, step)[0],
        "mfc850": lambda: plot.chart_mfc(cfg, F, 850, run, step)[0],
        "ept925": lambda: plot.chart_ept(cfg, F, 925, run, step),
        "ept850": lambda: plot.chart_ept(cfg, F, 850, run, step),
        "ept700": lambda: plot.chart_ept(cfg, F, 700, run, step),
        "gz500_wide": lambda: plot.chart_gz(cfg, F, 500, run, step, domain="wide"),
    }
    if P is not None and ("2d", 0) in F.data:
        jobs["snow"] = lambda: plot.chart_snow(cfg, F, P, run, step, window)
    for lv in (925, 850, 700, 500, 300, 200):
        jobs[f"gz{lv}"] = (lambda lv=lv: plot.chart_gz(cfg, F, lv, run, step))
    if clim is not None:
        jobs["a_t2m"] = lambda: plot.chart_anom(cfg, F, "t2m", None, run, step, clim)
        jobs["a_gz500"] = lambda: plot.chart_anom(cfg, F, "gz500", None, run, step, clim)
        for lv in (925, 850, 700):
            jobs[f"a_t{lv}"] = (lambda lv=lv: plot.chart_anom(cfg, F, "t", lv, run, step, clim))
            jobs[f"a_ept{lv}"] = (lambda lv=lv: plot.chart_anom(cfg, F, "ept", lv, run, step, clim))
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
    hourly_only = fetch.is_hourly_only(step, cfg)
    if hourly_only:                                    # surface-only step: surface charts, no WPSH
        want = [c for c in want if c in cfg["data"]["hourly"]["charts"]]

    def save(chart_id, frame):
        data = encode.encode(frame.png_bytes(), fmt, cfg["output"]["png8_colors"], cfg["output"]["webp_quality"])
        path = out / cfg["site"]["path"].format(model=cfg["model_id"], run=run_s, chart=chart_id,
                                                step=step).split("/", 2)[-1]
        path.write_bytes(data)
        sizes[chart_id] = len(data)

    clim = None
    if any(c.startswith("a_") for c in want):
        from . import clim as clim_mod
        clim = clim_mod.grid_at(cfg, run + dt.timedelta(hours=step))     # None: no climatology, no 편차 maps
    jobs = chart_jobs(cfg, F, P, run, step, clim)
    for cid in want:
        if cid in jobs:
            if not cid.startswith("a_"):
                save(cid, jobs[cid]())
                continue
            try:                         # a 편차 map never fails the run (KIM: a lost run costs ~1.9 GB of quota)
                save(cid, jobs[cid]())
            except Exception as e:  # noqa: BLE001
                log.warning("%s %s +%d %s: skipped (%r)", cfg["model_id"], run_s, step, cid, e)
    z = None if hourly_only else cfg["site"].get("zoom")
    if z:                                               # Korea zoom: same charts on the zoom map, prefixed ids
        zcfg = dict(cfg, domains=dict(cfg["domains"], default=cfg["domains"][z["domain"]]))
        zjobs = chart_jobs(zcfg, F, P, run, step)
        for cid in z["charts"]:
            save(z["prefix"] + cid, zjobs[cid]())
    res = {}
    if not hourly_only:
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
            "images": len(sizes), "wpsh": wpsh, "charts": sorted(sizes)}


def render_run(cfg: dict, sources, run: dt.datetime, site: pathlib.Path, work: pathlib.Path,
               steps: list[int] | None = None, cfg_path: str | None = None, resume: dict | None = None) -> dict:
    """Render one run of cfg's model into site/<model>/<run>/ and write its meta.json (written last).

    resume (KIM, whose downloads cost daily API quota): {"done": {step: result} already rendered and stored,
    "on_step": callable(result, files) called as each step finishes (stores its images, then they may be
    deleted locally), "deadline": time.monotonic() value after which no new step starts (raises Deferred)}.
    A job cancelled or stopped mid-run then loses at most the steps in flight (2026-11-22: a whole KIMR run,
    about 1.9 GB of quota, was lost to the 180 min job timeout and downloaded again)."""
    from . import ens
    if ens.is_ens(cfg):                       # AIFS-ENS: one plume graph per run, no map steps
        return ens.render_run(cfg, sources, run, site, work)
    resume = resume or {}
    done = {int(k): v for k, v in (resume.get("done") or {}).items()}
    on_step, deadline = resume.get("on_step"), resume.get("deadline")
    run_s = f"{run:%Y%m%d%H}"
    model = cfg["model_id"]
    rdir = site / model / run_s
    if rdir.exists():
        shutil.rmtree(rdir)          # partial leftovers from an interrupted job
    rdir.mkdir(parents=True)
    gdir = work / "grib" / model / run_s
    gdir.mkdir(parents=True, exist_ok=True)
    steps = steps if steps is not None else sorted(fetch.published_steps(run.hour, cfg)
                                                   + fetch.hourly_steps(run.hour, cfg))
    t0 = time.monotonic()
    results, dl_seconds, srcs = [dict(r) for _, r in sorted(done.items())], 0.0, set()
    if done:
        srcs.add("resumed")
        log.info("%s %s: resuming, %d steps already rendered", model, run_s, len(done))
    deferred = False
    with cf.ProcessPoolExecutor(cfg["site"]["workers"], initializer=_init_worker,
                                initargs=(cfg_path, model)) as pool:
        futures, handled = [], set()

        def flush(block: bool = False) -> None:
            for f in futures:
                if id(f) in handled or not (block or f.done()):
                    continue
                r = f.result()
                handled.add(id(f))
                results.append(r)
                if on_step:
                    on_step(r, sorted(rdir.glob(f"*_{r['step']:03d}.*")))

        # A step whose download keeps failing (KMA: 'HTTP 200 # ERROR: CGI ...' for one field, 2026-11-18) is put
        # aside and tried once more at the end; if it still fails, the run is published without it as long as
        # no more than site.max_missing_steps are missing (0 = any failure fails the run, as before).
        allow = cfg["site"].get("max_missing_steps", 0)
        try:
            pending, missing = [s for s in steps if s not in done], []
            for attempt in (1, 2):
                later = []
                for step in pending:
                    if deadline is not None and time.monotonic() > deadline:
                        deferred = True
                        break
                    try:
                        r = sources.fetch_step(run, step, gdir)
                    except RuntimeError as e:
                        if allow == 0 or type(e).__name__ == "QuotaError":    # daily limit: stop, do not skip
                            raise
                        log.warning("%s %s +%d: download failed (%s), %s", model, run_s, step, str(e)[:160],
                                    "retried at the end" if attempt == 1 else "left out")
                        later.append(step)
                        continue
                    dl_seconds += r["seconds"]
                    srcs.add(r["source"])
                    tp_prev = None
                    if step > 0:
                        try:
                            rp = sources.fetch_step(run, step - fetch.tp_window(step, cfg), gdir, tp_only=True)
                            dl_seconds += rp["seconds"]
                            tp_prev = rp["path"]
                        except RuntimeError as e:
                            if allow == 0 or type(e).__name__ == "QuotaError":
                                raise
                            log.warning("%s %s +%d: no precipitation window (%s)", model, run_s, step, str(e)[:160])
                    futures.append(pool.submit(render_step, run_s, step, r["path"], tp_prev, str(rdir)))
                    # keep at most 2 x workers steps of GRIB on disk
                    while sum(not f.done() for f in futures) > 2 * cfg["site"]["workers"]:
                        time.sleep(0.2)
                    flush()
                if deferred:
                    break
                pending = later
                if not pending:
                    break
        except Exception:
            try:
                flush(block=True)        # e.g. the hub's daily limit: keep the steps already rendered
            except Exception as e:  # noqa: BLE001
                log.warning("%s %s: could not store finished steps (%s)", model, run_s, e)
            raise
        flush(block=True)                # steps in flight are stored even when the run stops here
        if deferred:
            raise Deferred(f"job time limit: {len(results)} of {len(steps)} steps rendered and kept, "
                           "the rest follows next poll")
        missing = pending
        if len(missing) > allow:
            raise RuntimeError(f"{len(missing)} steps failed to download: {missing}")
    shutil.rmtree(gdir, ignore_errors=True)
    results.sort(key=lambda r: r["step"])
    wpsh = None
    hourly = [r["step"] for r in results if fetch.is_hourly_only(r["step"], cfg)]
    results = [r for r in results if r["step"] not in hourly] + [r for r in results if r["step"] in hourly]
    main = results[:len(results) - len(hourly)]
    if run.hour in cfg["wpsh"]["run_hours"]:
        wpsh = {str(r["step"]): r["wpsh"] for r in main}
    meta = {
        "model": model,
        "run": run_s,
        "steps": [r["step"] for r in main],
        "hourly_steps": hourly,             # surface-only steps (data.hourly): only the hourly charts exist
        "missing_steps": missing,
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
    # 편차 maps: listed only when every main step has them (the app then never asks for a missing image)
    want_a = [c["id"] for c in cfg["site"]["charts"] if c["id"].startswith("a_")]
    if want_a and main:
        have = set.intersection(*[set(r.get("charts", [])) for r in main])
        meta["charts"] = [c["id"] for c in cfg["site"]["charts"] if not c["id"].startswith("a_") or c["id"] in have]
    if getattr(sources, "stats", None):
        meta["field_ranges"] = dict(sources.stats)      # KIM: min/max per field, a units sanity check
    if hasattr(sources, "request_summary"):
        meta["kma_requests"] = sources.request_summary()
    (rdir / "meta.json").write_text(json.dumps(meta, indent=1))
    log.info("%s run %s: %d images, %.0f MB, wall %.0f s", model, run_s, meta["images"], meta["bytes"] / 1e6,
             meta["wall_seconds"])
    return meta

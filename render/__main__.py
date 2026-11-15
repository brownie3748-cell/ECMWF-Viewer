"""Command line: python -m render {poll,render-run,latest}."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import pathlib
import sys
import time

from . import config, fetch, kim, pipeline, site
from . import sources as sources_mod
from . import store as store_mod

log = logging.getLogger("render")


def recent_cycles(n: int, now: dt.datetime | None = None) -> list[dt.datetime]:
    now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    base = now.replace(minute=0, second=0, microsecond=0, hour=(now.hour // 6) * 6)
    return [base - dt.timedelta(hours=6 * i) for i in range(n)]


def _gh_output(**kv):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as fh:
            for k, v in kv.items():
                fh.write(f"{k}={v}\n")


def _summary(text: str):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as fh:
            fh.write(text + "\n")
    print(text)


def process_order(cfg) -> list[str]:
    """Models in processing order within a cycle (lightest first), from site.process_order."""
    order = [m for m in cfg["site"].get("process_order", []) if m in config.model_ids(cfg)]
    return order + [m for m in config.model_ids(cfg) if m not in order]


def _poll_plan(cfg, store_runs, complete_by_model, rv):
    """Work for one poll: (todo [(cycle, model)], remove {model: [run]}).

    Newest cycle first; within a cycle the models in process_order (lightest first).
    rv: render_version, an int for all models or {model: int}."""
    rv_of = (lambda m: rv[m]) if isinstance(rv, dict) else (lambda m: rv)
    todo, remove = [], {}
    cycles = sorted({c for cs in complete_by_model.values() for c in cs}, reverse=True)
    for c in cycles:
        rs = f"{c:%Y%m%d%H}"
        for mid in process_order(cfg):
            if c in complete_by_model.get(mid, []):
                have = store_runs.get(mid, {}).get(rs)
                if not have or have.get("rv") != rv_of(mid):
                    todo.append((c, mid))
    for mid in config.model_ids(cfg):
        target = {f"{c:%Y%m%d%H}" for c in complete_by_model.get(mid, [])}
        gone = sorted(set(store_runs.get(mid, {})) - target)
        if gone:
            remove[mid] = gone
    for mid in set(store_runs) - set(config.model_ids(cfg)):     # model dropped from the config
        remove[mid] = sorted(store_runs[mid])
    return todo, remove


def _newer_complete(cfg, sources, complete, queue):
    """[(cycle, model)] for cycles newer than each model's newest known complete run that are now complete."""
    now_cycles = recent_cycles(2)
    found = []
    for c in sorted(now_cycles, reverse=True):
        for mid in process_order(cfg):
            if mid not in sources:
                continue
            known = complete.get(mid, [])
            if (known and c <= known[0]) or (c, mid) in queue:
                continue
            if sources[mid][1].run_complete(c):
                found.append((c, mid))
    return found


def cmd_poll(args, cfg):
    work = pathlib.Path(args.work)
    stage = work / "stage"
    t_start = time.monotonic()
    st = store_mod.open_store(cfg, args.store)
    rv = {mid: config.for_model(cfg, mid)["site"]["render_version"] for mid in config.model_ids(cfg)}
    old = st.get_json("index.json")
    runs = site.runs_from_manifest(old)
    keep = cfg["site"]["retention_runs"]
    cycles = recent_cycles(cfg["site"]["lookback_runs"])
    complete, sources = {}, {}
    kma_usage = kim.load_usage(st)             # shared by the KIM models: one daily API Hub budget
    kma_calls0 = kma_usage["calls"]
    probe_notes = []
    for mid in config.model_ids(cfg):
        mcfg = config.for_model(cfg, mid)
        src = sources_mod.make_sources(mcfg)
        if kim.is_kma(mcfg):
            src.usage = kma_usage
        sources[mid] = (mcfg, src)
        complete[mid] = []
        for c in cycles:                       # newest first
            have = runs.get(mid, {}).get(f"{c:%Y%m%d%H}")
            if have and have.get("rv") == rv[mid]:
                complete[mid].append(c)        # already published: complete, no need to ask the source again
            else:
                try:
                    if src.run_complete(c):
                        complete[mid].append(c)
                except kim.QuotaError as e:
                    probe_notes.append(f"{mid}: {e}")
                    break
            if len(complete[mid]) >= keep:
                break
    todo, remove = _poll_plan(cfg, runs, complete, rv)
    manifest_stale = old is None or site.manifest(cfg, runs)["models"] != old.get("models") \
        or old.get("path") != cfg["site"]["path"]
    lines = ["## Poll result", "", f"Store: {st.name}", ""]
    for mid in config.model_ids(cfg):
        lines.append(f"- {mid}: complete runs in window {[f'{c:%Y%m%d%H}' for c in complete[mid]]}")
    if not todo and not remove and not manifest_stale:
        if kma_usage["calls"] != kma_calls0:
            kim.save_usage(st, kma_usage)       # completeness probes count against the daily budget
        lines.append("\nNothing to do.")
        _gh_output(changed="false")
        _summary("\n".join(lines))
        return

    sizes = st.list_sizes()
    error = None
    # 1) prune: drop runs from the manifest first, then delete their objects
    removed = []
    for mid, rlist in remove.items():
        for r in rlist:
            runs.get(mid, {}).pop(r, None)
            removed.append(f"{mid}/{r}")
    if removed:
        site.write_manifest(cfg, st, runs)
    for key in removed:
        st.delete_prefix(key + "/", sizes)
    # orphans: run prefixes in the bucket that the manifest does not list (interrupted uploads)
    listed = {f"{m}/{r}/" for m, rs in runs.items() for r in rs}
    todo_prefixes = {f"{m}/{c:%Y%m%d%H}/" for c, m in todo}
    orphans = sorted({"/".join(k.split("/")[:2]) + "/" for k in sizes if k.count("/") >= 2
                      and not k.startswith("_ops/")} - listed - todo_prefixes)
    for pfx in orphans:
        st.delete_prefix(pfx, sizes)
    if "NOTICE.txt" not in sizes:
        site.write_notice(cfg, st)
    # 2) render + upload, newest cycle first, within the time budget
    rendered, skipped, late, failed = [], [], [], []
    queue = list(todo)
    while queue:
        c, mid = queue.pop(0)
        rs = f"{c:%Y%m%d%H}"
        if error or (time.monotonic() - t_start) / 60 > args.time_budget_min:
            skipped.append(f"{mid}/{rs}")
            continue
        mcfg, src = sources[mid]
        store_mod.clear_dir(stage)
        try:
            if kim.is_kma(mcfg):               # do not start a KIM run the daily API budget cannot finish
                src.check_budget(*kim.run_cost(mcfg, len(fetch.published_steps(c.hour, mcfg))))
            meta = pipeline.render_run(mcfg, src, c, stage, work, cfg_path=args.config)
        except (kim.QuotaError, RuntimeError) as e:
            failed.append(f"{mid}/{rs}: {str(e)[:200]}")
            store_mod.clear_dir(stage)
            continue
        try:
            meta["uploaded_bytes"] = st.upload_dir(stage / mid / rs, f"{mid}/{rs}/", sizes, last="meta.json")
        except store_mod.StorageCapError as e:
            error = str(e)
            skipped.append(f"{mid}/{rs}")
            continue
        finally:
            store_mod.clear_dir(stage)
        runs.setdefault(mid, {})[rs] = site.run_entry(meta)
        site.write_manifest(cfg, st, runs)       # each finished run appears in the app right away
        rendered.append(meta)
        # a run published while this job works (long backfills) goes to the front of the queue
        new = _newer_complete(cfg, sources, complete, queue)
        for item in reversed(new):
            queue.insert(0, item)
            complete[item[1]].insert(0, item[0])
            late.append(f"{item[1]}/{item[0]:%Y%m%d%H}")
    if not rendered and not removed and manifest_stale:
        site.write_manifest(cfg, st, runs)
    if any(kim.is_kma(m) for m, _ in sources.values()):
        kim.save_usage(st, kma_usage)
    st.save_ops()
    _gh_output(changed="true")
    used = sum(sizes.values())
    ops = st.month_ops()
    lines += ["", f"Removed: {removed or 'none'}; orphans deleted: {orphans or 'none'}",
              f"Skipped (time budget or cap, next poll): {skipped or 'none'}",
              f"Published while this job ran (moved to the front): {late or 'none'}",
              f"Failed (retried next poll): {failed or 'none'}",
              f"KMA API Hub today (KST): {kma_usage['calls']} calls, {kma_usage['bytes'] / 1e9:.2f} GB "
              f"(guard {cfg['kma_quota']['max_calls_per_day']} calls / {cfg['kma_quota']['max_bytes_per_day'] / 1e9:.1f} GB)"
              + (f"; {probe_notes}" if probe_notes else ""), ""]
    if rendered:
        lines += ["| model | run | images | MB | download s | wall s | render s (sum) | sources |",
                  "|---|---|---|---|---|---|---|---|"]
        for r in rendered:
            lines.append(f"| {r['model']} | {r['run']} | {r['images']} | {r['bytes'] / 1e6:.1f} | "
                         f"{r['download_seconds']} | {r['wall_seconds']} | {r['render_seconds_sum']} | "
                         f"{', '.join(r['sources'])} |")
    per_model = {}
    for k, v in sizes.items():
        per_model[k.split("/")[0]] = per_model.get(k.split("/")[0], 0) + v
    lines += ["", f"**Storage: {used / 1e9:.2f} GB of cap {cfg['storage']['max_bytes'] / 1e9:.1f} GB** "
                  f"({len(sizes)} objects; " + ", ".join(f"{k} {v / 1e6:.0f} MB" for k, v in sorted(per_model.items()))
              + ")",
              f"Requests this month (upper-bound count): Class A {ops['class_a']} of "
              f"{cfg['storage']['max_class_a_per_month']}, Class B {ops['class_b']}"]
    if error:
        lines += ["", f"**STOPPED: {error}**"]
    _summary("\n".join(lines))
    if error:
        sys.exit(f"storage guard: {error}")


def cmd_render_run(args, cfg):
    """Render one run of one model into a local directory (smoke tests, debugging)."""
    run = dt.datetime.strptime(args.run, "%Y%m%d%H")
    steps = [int(s) for s in args.steps.split(",")] if args.steps else None
    mcfg = config.for_model(cfg, args.model)
    meta = pipeline.render_run(mcfg, sources_mod.make_sources(mcfg), run, pathlib.Path(args.site), pathlib.Path(args.work),
                               steps=steps, cfg_path=args.config)
    print(json.dumps(meta, indent=1))


def cmd_latest(args, cfg):
    src = sources_mod.make_sources(config.for_model(cfg, args.model))
    for c in recent_cycles(cfg["site"]["lookback_runs"]):
        if (args.main_only and c.hour not in (0, 12)):
            continue
        if src.run_complete(c):
            print(f"{c:%Y%m%d%H}")
            _gh_output(run=f"{c:%Y%m%d%H}")
            return
    sys.exit("no complete run found")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m render")
    ap.add_argument("--config", default=None, help="path to config.toml (default: render/config.toml)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("poll", help="render missing complete runs of every model, upload, prune, manifest")
    p.add_argument("--store", default="r2", help="r2 (credentials from env) or local:<dir>")
    p.add_argument("--work", default="work")
    p.add_argument("--time-budget-min", type=float, default=150)
    p = sub.add_parser("render-run", help="render one run of one model locally (optionally selected steps)")
    p.add_argument("--run", required=True)
    p.add_argument("--model", default="ifs")
    p.add_argument("--steps", default=None, help="comma-separated, e.g. 0,6,150")
    p.add_argument("--site", default="site")
    p.add_argument("--work", default="work")
    p = sub.add_parser("latest", help="print the newest complete run")
    p.add_argument("--model", default="ifs")
    p.add_argument("--main-only", action="store_true", help="00/12Z runs only")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config.load(args.config)
    {"poll": cmd_poll, "render-run": cmd_render_run, "latest": cmd_latest}[args.cmd](args, cfg)


if __name__ == "__main__":
    main()

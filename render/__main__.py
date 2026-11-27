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


GROUPS = ("all", "ecmwf", "kim")
# Each group's request counter in _ops/ (store.month): the jobs never write the same counter file.
COUNTERS = {"all": "", "ecmwf": "", "kim": "kim-", "dfs": "dfs-", "clim": "clim-"}


def group_models(cfg, group: str) -> list[str]:
    """Models one poll job handles: all, ecmwf (everything but the KMA API Hub models) or kim (publish.yml and
    kim.yml run as separate jobs, so a two-hour KIM run never holds back the ECMWF runs)."""
    ids = config.model_ids(cfg)
    if group == "all":
        return ids
    is_k = {m: kim.is_kma(config.for_model(cfg, m)) for m in ids}
    return [m for m in ids if is_k[m] == (group == "kim")]


def owns_dropped(group: str) -> bool:
    """Runs of a model dropped from the config are removed by the all / ecmwf job."""
    return group != "kim"


def use_own_counter(st, name: str) -> int:
    """Count this job's requests in _ops/<prefix><month>.json and lower its Class A limit by what the other jobs
    counted this month (their files are read once, at the start). Returns the other jobs' Class A count."""
    month = st.month
    others = sum((st.get_json(f"_ops/{pre}{month}.json") or {}).get("class_a", 0)
                 for pre in set(COUNTERS.values()) - {COUNTERS[name]})
    st.month, st._month_ops = COUNTERS[name] + month, None
    st.scfg = dict(st.scfg, max_class_a_per_month=st.scfg["max_class_a_per_month"] - others)
    return others


def write_manifest(cfg, st, runs, mine) -> None:
    """index.json with this job's models from `runs` and every other model as the store has it now: the other
    poll job (publish.yml / kim.yml) may have published or pruned a run since this job started."""
    if set(mine) >= set(config.model_ids(cfg)):
        site.write_manifest(cfg, st, runs)
        return
    fresh = site.runs_from_manifest(st.get_json("index.json"))
    merged = {m: (runs.get(m, {}) if m in mine else fresh.get(m, runs.get(m, {})))
              for m in set(runs) | set(fresh)}
    site.write_manifest(cfg, st, merged)


def _poll_plan(cfg, store_runs, complete_by_model, rv, models=None, dropped=True):
    """Work for one poll: (todo [(cycle, model)], remove {model: [run]}).

    Newest cycle first; within a cycle the models in process_order (lightest first).
    rv: render_version, an int for all models or {model: int}. models: the models this job handles (default all);
    dropped: also remove the runs of models no longer in the config."""
    rv_of = (lambda m: rv[m]) if isinstance(rv, dict) else (lambda m: rv)
    models = config.model_ids(cfg) if models is None else models
    todo, remove = [], {}
    cycles = sorted({c for cs in complete_by_model.values() for c in cs}, reverse=True)
    for c in cycles:
        rs = f"{c:%Y%m%d%H}"
        for mid in process_order(cfg):
            if mid in models and c in complete_by_model.get(mid, []):
                have = store_runs.get(mid, {}).get(rs)
                if not have or (have.get("rv") != rv_of(mid) and not _quota_model(cfg, mid)):
                    todo.append((c, mid))
    for mid in models:
        target = {f"{c:%Y%m%d%H}" for c in complete_by_model.get(mid, [])}
        gone = sorted(set(store_runs.get(mid, {})) - target)
        if gone:
            remove[mid] = gone
    for mid in set(store_runs) - set(config.model_ids(cfg)) if dropped else ():     # model dropped from the config
        remove[mid] = sorted(store_runs[mid])
    return todo, remove


def _quota_model(cfg, mid) -> bool:
    """KIM (KMA API Hub): every download costs daily quota, so a stored run is never fetched again just to
    re-render it with a newer render_version (one KIMR run is about 1.9 GB of the 5 GB a day); new runs get it."""
    return kim.is_kma(config.for_model(cfg, mid))


PARTIAL = "partial.json"                  # <model>/<run>/partial.json: steps of an unfinished KIM run in the store


def _resumable(mcfg, st, mid, c) -> bool:
    """A KIM run an earlier job partly rendered (and that is still young enough to finish)."""
    age_h = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - c).total_seconds() / 3600
    if age_h > mcfg["kma"].get("max_fetch_age_h", 1e9) + mcfg["kma"].get("resume_extra_h", 12):
        return False
    return bool(st.get_json(f"{mid}/{c:%Y%m%d%H}/{PARTIAL}"))


def _resume_hook(st, mid, rs, rv, sizes, usage, deadline):
    """render_run's resume dict: each finished step's images go to the store at once, with partial.json."""
    prefix, key = f"{mid}/{rs}/", f"{mid}/{rs}/{PARTIAL}"
    part = st.get_json(key) or {}
    if part.get("rv") != rv:
        part = {"rv": rv, "done": {}}
    cap = st.scfg["max_bytes"]
    cc = st.scfg["image_cache_control"]

    def on_step(result, files):
        new = sum(p.stat().st_size for p in files)
        if sum(sizes.values()) + new > cap:
            raise store_mod.StorageCapError(f"{prefix}: step +{result['step']} would pass the storage cap")
        for p in files:
            data = p.read_bytes()
            st.put_bytes(prefix + p.name, data, cc)
            sizes[prefix + p.name] = len(data)
            p.unlink()
        part["done"][str(result["step"])] = result
        raw = json.dumps(part).encode()
        st.put_bytes(key, raw, "no-cache")
        sizes[key] = len(raw)
        kim.save_usage(st, usage)          # the quota count survives a cancelled job too

    return {"done": part["done"], "on_step": on_step, "deadline": deadline}


def _complete_runs(mcfg, st, mid, src, cycles, stored: dict, keep: int, notes: list) -> list[dt.datetime]:
    """Complete runs of one model among cycles (newest first), at most keep of them."""
    out, quota_stop = [], False
    for c in cycles:
        rs = f"{c:%Y%m%d%H}"
        if stored.get(rs):
            out.append(c)                  # already published: complete, no need to ask the source again
        elif kim.is_kma(mcfg) and _resumable(mcfg, st, mid, c):
            out.append(c)                  # partly rendered by an earlier job: complete, no probe
        elif not quota_stop:
            try:
                if src.run_complete(c):
                    out.append(c)
            except kim.QuotaError as e:
                # stop probing, but keep walking the stored runs: a 'break' here left the stored KIM runs out
                # of the window and the prune step deleted all of them (2026-11-22)
                notes.append(f"{mid}: {e}")
                quota_stop = True
        if len(out) >= keep:
            break
    return out


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
    mine = group_models(cfg, args.models)
    other_ops = use_own_counter(st, args.models) if args.models != "all" else 0
    rv = {mid: config.for_model(cfg, mid)["site"]["render_version"] for mid in mine}
    old = st.get_json("index.json")
    runs = site.runs_from_manifest(old)
    keep = cfg["site"]["retention_runs"]
    cycles = recent_cycles(cfg["site"]["lookback_runs"])
    complete, sources = {}, {}
    kma_usage = kim.load_usage(st)             # shared by the KIM models: one daily API Hub budget
    other = st.get_json(f"_ops/kmadfs-{kma_usage['day']}.json") or {}      # the 예보 job's own count (forecast.yml)
    if other.get("day") == kma_usage["day"]:
        kma_usage["other_calls"], kma_usage["other_bytes"] = other.get("calls", 0), other.get("bytes", 0)
    kma_calls0 = kma_usage["calls"]
    probe_notes = []
    clim = None
    for mid in mine:
        mcfg = config.for_model(cfg, mid)
        if mcfg["data"].get("source") == "ens":            # climate lines for the 기온 graphs (clim.yml builds them)
            if clim is None:
                clim = st.get_json(cfg["clim"]["key"]) or {}
            mcfg["ens"]["clim"] = clim
        src = sources_mod.make_sources(mcfg)
        if kim.is_kma(mcfg):
            src.usage = kma_usage
        sources[mid] = (mcfg, src)
        complete[mid] = _complete_runs(mcfg, st, mid, src, cycles, runs.get(mid, {}),
                                       mcfg["site"].get("retention_runs", keep), probe_notes)
    todo, remove = _poll_plan(cfg, runs, complete, rv, mine, owns_dropped(args.models))
    # a run whose meta.json is in the store but not in the manifest (the other poll job wrote index.json in
    # between, or a job stopped after the upload): adopt it instead of fetching it again (KIM: 1.9 GB of quota)
    adopted = []
    for c, mid in list(todo):
        meta = st.get_json(f"{mid}/{c:%Y%m%d%H}/meta.json")
        if meta and (meta.get("render_version", 1) == rv[mid] or _quota_model(cfg, mid)):
            runs.setdefault(mid, {})[f"{c:%Y%m%d%H}"] = site.run_entry(meta)
            todo.remove((c, mid))
            adopted.append(f"{mid}/{c:%Y%m%d%H}")
    manifest_stale = old is None or site.manifest(cfg, runs)["models"] != old.get("models") \
        or old.get("path") != cfg["site"]["path"]
    lines = ["## Poll result", "", f"Store: {st.name}; models: {args.models} ({', '.join(mine)})", ""]
    for mid in mine:
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
        write_manifest(cfg, st, runs, mine)
    for key in removed:
        st.delete_prefix(key + "/", sizes)
    # orphans: run prefixes in the bucket that the manifest does not list (interrupted uploads). Only model
    # prefixes: dfs/ (forecast.yml) and _ops/ belong to other jobs (2026-11-26: every poll deleted dfs/vsrt/ and
    # dfs/shrt/ as "orphans", so the 예보 images were gone minutes after forecast.yml uploaded them)
    listed = {f"{m}/{r}/" for m, rs in runs.items() for r in rs}
    todo_prefixes = {f"{m}/{c:%Y%m%d%H}/" for c, m in todo}
    # a KIM run with partial.json is kept for a later poll (it is no longer in todo once the hub's daily limit
    # stops the probes); _resumable's age limit decides when it is given up
    resumable = {k[: -len(PARTIAL)] for k in sizes if k.endswith("/" + PARTIAL)
                 and _resumable(config.for_model(cfg, k.split("/")[0]), st, k.split("/")[0],
                                dt.datetime.strptime(k.split("/")[1], "%Y%m%d%H"))} \
        if any(kim.is_kma(config.for_model(cfg, m)) for m in config.model_ids(cfg)) else set()
    model_ids = set(mine) | ((set(runs) - set(config.model_ids(cfg))) if owns_dropped(args.models) else set())
    orphans = sorted({"/".join(k.split("/")[:2]) + "/" for k in sizes if k.count("/") >= 2
                      and k.split("/")[0] in model_ids} - listed - todo_prefixes - resumable)
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
                src.check_budget(*kim.run_cost(mcfg, len(fetch.published_steps(c.hour, mcfg)),
                                                len(fetch.hourly_steps(c.hour, mcfg))))
            resume = None
            if kim.is_kma(mcfg):
                resume = _resume_hook(st, mid, rs, rv[mid], sizes, kma_usage,
                                      t_start + args.hard_stop_min * 60)
            if any(x["id"].startswith("a_") for x in mcfg["site"]["charts"]):
                # 편차 maps: the climatology days this run needs, read by the render workers (ANOM_CLIM_DIR)
                from . import clim as clim_mod
                cdir = work / "clim_grid"
                steps_all = fetch.published_steps(c.hour, mcfg) + fetch.hourly_steps(c.hour, mcfg)
                if clim_mod.fetch_grid(st, cfg, c, max(steps_all), cdir):
                    log.warning("%s %s: map climatology missing in the store (run clim.yml): no 편차 maps", mid, rs)
                os.environ["ANOM_CLIM_DIR"] = str(cdir)
            meta = pipeline.render_run(mcfg, src, c, stage, work, cfg_path=args.config, resume=resume)
        except (kim.QuotaError, RuntimeError) as e:
            failed.append(f"{mid}/{rs}: {str(e)[:200]}")
            if isinstance(e, kim.QuotaError):  # hub or own daily limit: no other KIM run can start this poll
                queue = [(qc, qm) for qc, qm in queue if not kim.is_kma(sources[qm][0])]
            store_mod.clear_dir(stage)
            continue
        finally:
            if kim.is_kma(mcfg):
                kim.save_usage(st, kma_usage)   # after every KIM run: a cancelled job keeps its count
        try:
            meta["uploaded_bytes"] = st.upload_dir(stage / mid / rs, f"{mid}/{rs}/", sizes, last="meta.json",
                                                   keep_others=resume is not None)
            if resume is not None:
                st.delete_prefix(f"{mid}/{rs}/{PARTIAL}", sizes)
        except store_mod.StorageCapError as e:
            error = str(e)
            skipped.append(f"{mid}/{rs}")
            continue
        finally:
            store_mod.clear_dir(stage)
        runs.setdefault(mid, {})[rs] = site.run_entry(meta)
        write_manifest(cfg, st, runs, mine)      # each finished run appears in the app right away
        rendered.append(meta)
        # a run published while this job works (long backfills) goes to the front of the queue
        new = _newer_complete(cfg, sources, complete, queue)
        for item in reversed(new):
            queue.insert(0, item)
            complete[item[1]].insert(0, item[0])
            late.append(f"{item[1]}/{item[0]:%Y%m%d%H}")
    if not rendered and not removed and manifest_stale:
        write_manifest(cfg, st, runs, mine)
    if any(kim.is_kma(m) for m, _ in sources.values()):
        kim.save_usage(st, kma_usage)
    st.save_ops()
    _gh_output(changed="true")
    used = sum(sizes.values())
    ops = st.month_ops()
    lines += ["", f"Removed: {removed or 'none'}; orphans deleted: {orphans or 'none'}; "
                  f"adopted from the store: {adopted or 'none'}",
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
              f"Requests this month (upper-bound count): Class A {ops['class_a']} (this job's counter) + "
              f"{other_ops} (other jobs) of {cfg['storage']['max_class_a_per_month']}, Class B {ops['class_b']}"]
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


def cmd_dfs(args, cfg):
    """KMA forecast images (예보 tab): fetch new 초단기 / 단기 issuances, upload, prune, write dfs/index.json."""
    from . import dfs
    st = store_mod.open_store(cfg, args.store)
    use_own_counter(st, "dfs")                       # own counter file; the limit counts the other jobs too
    lines = ["## KMA forecast images (예보)", ""]
    try:
        lines += dfs.run(cfg, st, force=args.force)
    finally:
        st.save_ops()
        _summary("\n".join(lines))


def cmd_clim(args, cfg):
    """Build clim/points.json (ERA5 climatology + KMA normals at the ENS points) and upload it to the store."""
    from . import clim as clim_mod
    st = store_mod.open_store(cfg, args.store)
    use_own_counter(st, "clim")
    t0 = time.monotonic()
    data, files = clim_mod.build(cfg)
    raw = json.dumps(data, separators=(",", ":")).encode()
    sizes = st.list_sizes()
    new = len(raw) + sum(len(b) for b in files.values())
    after = sum(v for k, v in sizes.items() if not k.startswith("clim/")) + new
    if after > st.scfg["max_bytes"]:
        sys.exit(f"storage guard: clim files ({new / 1e6:.0f} MB) would bring the bucket to {after / 1e9:.2f} GB")
    st.check_budget(len(files) + 2)
    with __import__("concurrent.futures").futures.ThreadPoolExecutor(cfg["storage"]["upload_threads"]) as ex:
        list(ex.map(lambda kv: st.put_bytes(kv[0], kv[1], "public, max-age=86400"), files.items()))
    st.put_bytes(cfg["clim"]["key"], raw, "no-cache, max-age=0")
    st.save_ops()
    k = data["kma"]["points"]
    _summary("\n".join(["## Climate (clim/points.json, clim/grid/)", "",
                        f"- ERA5: {len(data['era5']['points'])} points, {data['era5']['chunks']} chunks read",
                        f"- Map climatology: {len(files)} day files, {sum(len(b) for b in files.values()) / 1e6:.0f} MB, "
                        f"box {data['grid']['box']}",
                        "- KMA normals: " + ", ".join(f"{p} {v['stn']} ({v['tmst'] - 30}-{v['tmst'] - 1})"
                                                       for p, v in k.items()),
                        f"- {new / 1e6:.0f} MB uploaded, {time.monotonic() - t0:.0f} s (measured); bucket after: "
                        f"{after / 1e9:.2f} GB"]))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m render")
    ap.add_argument("--config", default=None, help="path to config.toml (default: render/config.toml)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("poll", help="render missing complete runs of every model, upload, prune, manifest")
    p.add_argument("--store", default="r2", help="r2 (credentials from env) or local:<dir>")
    p.add_argument("--work", default="work")
    p.add_argument("--models", choices=GROUPS, default="all",
                   help="ecmwf: publish.yml; kim: kim.yml (KMA API Hub models); all: both in one job")
    p.add_argument("--time-budget-min", type=float, default=150)
    p.add_argument("--hard-stop-min", type=float, default=165,
                   help="a KIM run starts no new step after this (the job times out at 180 min)")
    p = sub.add_parser("render-run", help="render one run of one model locally (optionally selected steps)")
    p.add_argument("--run", required=True)
    p.add_argument("--model", default="ifs")
    p.add_argument("--steps", default=None, help="comma-separated, e.g. 0,6,150")
    p.add_argument("--site", default="site")
    p.add_argument("--work", default="work")
    p = sub.add_parser("latest", help="print the newest complete run")
    p.add_argument("--model", default="ifs")
    p.add_argument("--main-only", action="store_true", help="00/12Z runs only")
    p = sub.add_parser("dfs", help="KMA forecast images (초단기 / 단기) for the 예보 tab")
    p.add_argument("--store", default="r2", help="r2 (credentials from env) or local:<dir>")
    p.add_argument("--force", action="store_true", help="fetch the newest issuances even if published")
    p = sub.add_parser("clim", help="climate lines for the ENS graphs (ERA5 climatology + KMA normals), once")
    p.add_argument("--store", default="r2", help="r2 (credentials from env) or local:<dir>")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config.load(args.config)
    {"poll": cmd_poll, "render-run": cmd_render_run, "latest": cmd_latest, "dfs": cmd_dfs, "clim": cmd_clim}[args.cmd](args, cfg)


if __name__ == "__main__":
    main()

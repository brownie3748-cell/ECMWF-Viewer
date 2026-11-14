"""Command line: python -m render {poll,render-run,manifest}."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import pathlib
import sys
import time

from . import config, pipeline, site
from .sources import Sources

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


def cmd_poll(args, cfg):
    sitedir, work = pathlib.Path(args.site), pathlib.Path(args.work)
    src = Sources(cfg)
    t_start = time.monotonic()
    cycles = recent_cycles(cfg["site"]["lookback_runs"])
    complete = []
    for c in cycles:                       # newest first
        if src.run_complete(c):
            complete.append(c)
        if len(complete) >= cfg["site"]["retention_runs"]:
            break
    target = {f"{c:%Y%m%d%H}" for c in complete}
    have = site.complete_runs(sitedir)
    # 1) prune first (runs that left the window and partial leftovers)
    removed = site.prune(sitedir, target & set(have))
    # 2) render missing runs, newest first, within the time budget
    rendered, skipped = [], []
    for c in complete:
        rs = f"{c:%Y%m%d%H}"
        if rs in have:
            continue
        if (time.monotonic() - t_start) / 60 > args.time_budget_min:
            skipped.append(rs)
            continue
        meta = pipeline.render_run(cfg, src, c, sitedir, work, cfg_path=args.config)
        rendered.append(meta)
    m = site.write_manifest(cfg, sitedir)
    changed = bool(rendered or removed)
    _gh_output(changed=str(changed).lower())
    lines = ["## Poll result", "",
             f"Complete runs in window: {sorted(target)}", f"Removed: {removed or 'none'}",
             f"Skipped (time budget, next poll): {skipped or 'none'}",
             f"Runs on site: {[r['run'] for r in m['runs']]}", ""]
    if rendered:
        lines += ["| run | images | MB | download s | wall s | render s (sum over workers) | sources |",
                  "|---|---|---|---|---|---|---|"]
        for r in rendered:
            lines.append(f"| {r['run']} | {r['images']} | {r['bytes'] / 1e6:.1f} | {r['download_seconds']} | "
                         f"{r['wall_seconds']} | {r['render_seconds_sum']} | {', '.join(r['sources'])} |")
    total = sum(p.stat().st_size for p in sitedir.rglob("*") if p.is_file())
    lines.append(f"\nSite size: {total / 1e6:.0f} MB, files: {sum(1 for p in sitedir.rglob('*') if p.is_file())}")
    _summary("\n".join(lines))


def cmd_render_run(args, cfg):
    run = dt.datetime.strptime(args.run, "%Y%m%d%H")
    steps = [int(s) for s in args.steps.split(",")] if args.steps else None
    meta = pipeline.render_run(cfg, Sources(cfg), run, pathlib.Path(args.site), pathlib.Path(args.work),
                               steps=steps, cfg_path=args.config)
    site.write_manifest(cfg, pathlib.Path(args.site))
    print(json.dumps(meta, indent=1))


def cmd_latest(args, cfg):
    src = Sources(cfg)
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
    p = sub.add_parser("poll", help="prune, render missing complete runs, write the manifest")
    p.add_argument("--site", default="site")
    p.add_argument("--work", default="work")
    p.add_argument("--time-budget-min", type=float, default=150)
    p = sub.add_parser("render-run", help="render one run (optionally selected steps)")
    p.add_argument("--run", required=True)
    p.add_argument("--steps", default=None, help="comma-separated, e.g. 0,6,150")
    p.add_argument("--site", default="site")
    p.add_argument("--work", default="work")
    p = sub.add_parser("latest", help="print the newest complete run")
    p.add_argument("--main-only", action="store_true", help="00/12Z runs only")
    p = sub.add_parser("manifest", help="rewrite index.json from the site directory")
    p.add_argument("--site", default="site")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config.load(args.config)
    {"poll": cmd_poll, "render-run": cmd_render_run, "latest": cmd_latest,
     "manifest": lambda a, c: site.write_manifest(c, pathlib.Path(a.site))}[args.cmd](args, cfg)


if __name__ == "__main__":
    main()

"""Published site: run directories, pruning and the index.json manifest."""

from __future__ import annotations

import datetime as dt
import json
import logging
import pathlib
import shutil

log = logging.getLogger(__name__)
ROOT = pathlib.Path(__file__).resolve().parents[1]


def complete_runs(site: pathlib.Path) -> dict[str, dict]:
    """Runs whose meta.json exists (meta.json is written last, so these are complete)."""
    out = {}
    for meta in sorted((site / "runs").glob("*/meta.json")):
        try:
            out[meta.parent.name] = json.loads(meta.read_text())
        except json.JSONDecodeError:
            log.warning("unreadable %s", meta)
    return out


def prune(site: pathlib.Path, keep: set[str]) -> list[str]:
    """Delete every run directory (complete or partial) not in `keep`."""
    removed = []
    runs_dir = site / "runs"
    if not runs_dir.exists():
        return removed
    for d in sorted(runs_dir.iterdir()):
        if d.is_dir() and d.name not in keep:
            shutil.rmtree(d)
            removed.append(d.name)
    return removed


def sync_app(src: pathlib.Path, site: pathlib.Path) -> bool:
    """Mirror the web app (app/www) into site/app/. Returns True if anything changed."""
    dst = site / "app"
    want = {p.relative_to(src): p.read_bytes() for p in src.rglob("*") if p.is_file()}
    have = {p.relative_to(dst): p.read_bytes() for p in dst.rglob("*") if p.is_file()} if dst.exists() else {}
    if want == have:
        return False
    if dst.exists():
        shutil.rmtree(dst)
    for rel, data in want.items():
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        (dst / rel).write_bytes(data)
    return True


def manifest(cfg: dict) -> dict:
    return {
        "schema_version": cfg["site"]["schema_version"],
        "model": cfg["style"]["model_label"],
        "attribution": cfg["style"]["credit"],
        "kst_offset_h": cfg["style"]["kst_offset_h"],
        "path": cfg["site"]["path"],
        "charts": [dict(c) for c in cfg["site"]["charts"]],
        "runs": [],
    }


def write_manifest(cfg: dict, site: pathlib.Path) -> dict:
    """index.json: charts + complete runs (oldest first) with steps and WPSH H / H-50 per step."""
    m = manifest(cfg)
    m["generated_utc"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for run, meta in sorted(complete_runs(site).items()):
        m["runs"].append({"run": run, "steps": meta["steps"], "wpsh": meta["wpsh"]})
    m["latest"] = m["runs"][-1]["run"] if m["runs"] else None
    site.mkdir(parents=True, exist_ok=True)
    (site / "index.json").write_text(json.dumps(m, separators=(",", ":")))
    (site / "NOTICE.txt").write_text((ROOT / "NOTICE").read_text())
    (site / "index.html").write_text(
        "<!doctype html><meta charset=utf-8><title>ECMWF chart viewer data</title>"
        "<p>Static chart data for the ECMWF chart viewer app. Open the viewer: <a href=app/>app/</a>. "
        "Manifest: <a href=index.json>index.json</a>. "
        f"Latest run: {m['latest']}.</p><p>{cfg['style']['credit']} See <a href=NOTICE.txt>NOTICE</a>.</p>")
    return m

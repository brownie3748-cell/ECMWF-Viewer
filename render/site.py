"""Published site layout and the index.json manifest (schema 2: several models).

Layout in the store (R2 bucket or local directory):
    index.json                       manifest, written after every change (last)
    NOTICE.txt                       licence / attribution notice
    <model>/<run>/<chart>_<step>.png images
    <model>/<run>/meta.json          written after the run's images (= run complete)
    _ops/<YYYY-MM>.json              request counter for the monthly budget
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import pathlib

from . import config as config_mod

log = logging.getLogger(__name__)
ROOT = pathlib.Path(__file__).resolve().parents[1]


def run_entry(meta: dict) -> dict:
    """The part of a run's meta.json that goes into the manifest."""
    e = {"run": meta["run"], "steps": meta["steps"], "wpsh": meta["wpsh"],
         "rv": meta.get("render_version", 1), "bytes": meta.get("bytes", 0)}
    if meta.get("hourly_steps"):
        e["hourly_steps"] = meta["hourly_steps"]     # surface-only steps; old apps ignore them
    return e


def runs_from_manifest(m: dict | None) -> dict[str, dict[str, dict]]:
    """{model: {run: entry}} from a schema-2 manifest (empty for none or an older schema)."""
    out: dict[str, dict[str, dict]] = {}
    if not m or m.get("schema_version") != 2:
        return out
    for mod in m.get("models", []):
        if mod.get("data_model"):              # virtual model (KIM zoom): its runs belong to the data model
            continue
        out[mod["id"]] = {r["run"]: r for r in mod.get("runs", [])}
    return out


def manifest(cfg: dict, runs: dict[str, dict[str, dict]]) -> dict:
    """index.json: per model its charts and complete runs (oldest first) with steps and WPSH per step."""
    models = []
    for mid in config_mod.model_ids(cfg):
        mc = config_mod.for_model(cfg, mid)
        rs = [dict(r) for _, r in sorted(runs.get(mid, {}).items())]
        for r in rs:
            r.pop("bytes", None)
            if mc["data"].get("source") == "ens":        # plume: the app shows "<last valid time>까지"
                r["end_step"] = config_mod.ens_last_step(mc, int(r["run"][8:10]))
        models.append({
            "id": mid,
            "label": mc["model_label_short"],
            "title": mc["style"]["model_label"],
            "attribution": mc["style"]["credit"],
            "charts": [dict(c, hourly=True) if c["id"] in (mc["data"].get("hourly") or {}).get("charts", [])
                       else dict(c) for c in mc["site"]["charts"]],
            "runs": rs,
            "latest": rs[-1]["run"] if rs else None,
        })
        z = mc["site"].get("zoom")
        if z:                                           # virtual model: the same runs drawn on the zoom map
            by_id = {c["id"]: c for c in mc["site"]["charts"]}
            models.append(dict(models[-1], id=z["model_id"], label=z["label"], data_model=mid,
                               chart_prefix=z["prefix"], charts=[dict(by_id[c]) for c in z["charts"]]))
    return {
        "schema_version": cfg["site"]["schema_version"],
        "generated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "attribution": cfg["style"]["credit"],
        "kst_offset_h": cfg["style"]["kst_offset_h"],
        "path": cfg["site"]["path"],
        "default_model": config_mod.model_ids(cfg)[0],
        "models": models,
    }


def write_manifest(cfg: dict, store, runs: dict[str, dict[str, dict]]) -> dict:
    m = manifest(cfg, runs)
    store.put_bytes("index.json", json.dumps(m, separators=(",", ":")).encode(),
                    cfg["storage"]["manifest_cache_control"])
    return m


def write_notice(cfg: dict, store) -> None:
    store.put_bytes("NOTICE.txt", (ROOT / "NOTICE").read_bytes(), "public, max-age=3600")

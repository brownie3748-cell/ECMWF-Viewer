"""Shared helpers for the Stage 0 measurement scripts."""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys
import traceback

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from render import config  # noqa: E402

WORK = pathlib.Path(os.environ.get("STAGE0_WORK", ROOT / "work"))
DATA = WORK / "data"
RESULTS = WORK / "results"
IMAGES = WORK / "images"
for p in (DATA, RESULTS, IMAGES):
    p.mkdir(parents=True, exist_ok=True)

CFG = config.load()


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def parse_run(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y%m%d%H")


def fmt_run(r: dt.datetime) -> str:
    return r.strftime("%Y%m%d%H")


def save(mid: str, data: dict, md: str) -> None:
    (RESULTS / f"{mid}.json").write_text(json.dumps(data, indent=2, default=str))
    (RESULTS / f"{mid}.md").write_text(md)
    print(md)


def load(mid: str) -> dict | None:
    p = RESULTS / f"{mid}.json"
    return json.loads(p.read_text()) if p.exists() else None


def guarded(mid: str, title: str):
    """Decorator: on an unexpected exception write a FAIL/UNKNOWN report instead of crashing."""

    def deco(fn):
        def wrapper(*a, **k):
            try:
                return fn(*a, **k)
            except Exception as e:  # noqa: BLE001
                tb = traceback.format_exc()
                save(mid, {"status": "UNKNOWN", "error": repr(e), "traceback": tb},
                     f"## {title}\n\n**UNKNOWN** — script error: `{e!r}`\n\n```\n{tb[-3000:]}\n```\n")
                return None
        return wrapper
    return deco


def md_table(headers: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for r in rows:
        out.append("| " + " | ".join("" if v is None else str(v) for v in r) + " |")
    return "\n".join(out) + "\n"


def best_source(default: str | None = None) -> list[str]:
    """Sources ordered by M1 download speed (fastest first), falling back to config order."""
    m1 = load("m1")
    order = []
    if m1 and m1.get("ranking"):
        order = list(m1["ranking"])
    for s in [default, CFG["data"]["preferred_source"], *CFG["data"]["sources"]]:
        if s and s not in order:
            order.append(s)
    return order


def latest_main_run() -> dt.datetime | None:
    m1 = load("m1")
    if m1 and m1.get("common_latest_main_run"):
        return parse_run(m1["common_latest_main_run"])
    return None

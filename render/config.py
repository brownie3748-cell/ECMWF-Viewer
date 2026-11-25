"""Load the single TOML configuration file and derive per-model configurations."""

from __future__ import annotations

import copy
import pathlib
import tomllib

CONFIG_PATH = pathlib.Path(__file__).with_name("config.toml")


def load(path: str | pathlib.Path | None = None) -> dict:
    with open(path or CONFIG_PATH, "rb") as fh:
        return tomllib.load(fh)


def _merge(base: dict, over: dict) -> dict:
    """Deep-merge `over` into a copy of `base`; lists and scalars in `over` replace."""
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def model_ids(cfg: dict) -> list[str]:
    return [m["id"] for m in cfg["models"] if m.get("enabled", True)]


def for_model(cfg: dict, model_id: str) -> dict:
    """Config for one model: base + its override, site.charts reduced to the model's chart list."""
    m = next((m for m in cfg["models"] if m["id"] == model_id), None)
    if m is None:
        raise KeyError(f"unknown model {model_id!r}")
    out = _merge(cfg, m.get("override", {}))
    if m["charts"] == "all":                   # map models: graph charts (AIFS-ENS plume) belong to their own model
        out["site"]["charts"] = [c for c in out["site"]["charts"] if c.get("domain") != "graph"]
    else:
        want = list(m["charts"])
        by_id = {c["id"]: c for c in out["site"]["charts"]}
        missing = [c for c in want if c not in by_id]
        if missing:
            raise KeyError(f"model {model_id}: unknown charts {missing}")
        out["site"]["charts"] = [by_id[c] for c in want]
    out["model_id"] = model_id
    out["model_label_short"] = m["label"]
    return out

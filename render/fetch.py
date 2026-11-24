"""Download GRIB2 fields from ECMWF open data (portal or cloud mirrors)."""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import time

from ecmwf.opendata import Client


def make_client(source: str, cfg: dict, retries: int = 3, retry_after: int = 10) -> Client:
    d = cfg["data"]
    return Client(
        source=source,
        model=d["model"],
        resol=d["resolution"],
        maximum_retries=retries,
        retry_after=retry_after,
    )


def run_steps(run_hour: int, cfg: dict) -> list[int]:
    """Native steps of a run up to the configured maximum."""
    d = cfg["data"]
    max_step = d["max_step_main"] if run_hour in (0, 12) else d["max_step_short"]
    steps = list(range(0, min(144, max_step) + 1, 3))
    steps += list(range(150, max_step + 1, 6))
    return steps


def published_steps(run_hour: int, cfg: dict) -> list[int]:
    d = cfg["data"]
    max_step = d["max_step_main"] if run_hour in (0, 12) else d["max_step_short"]
    switch = min(d["tp_switch_h"], max_step)
    out = list(range(0, switch + 1, d["publish_every_h"]))
    long_every = d.get("publish_every_h_long", d["publish_every_h"])
    return out + [s for s in range(switch + long_every, max_step + 1, long_every) if s % long_every == 0]


def hourly_steps(run_hour: int, cfg: dict) -> list[int]:
    """Extra surface-only steps between the published steps (data.hourly, KIM regional); [] for other models."""
    h = cfg["data"].get("hourly")
    if not h:
        return []
    main = set(published_steps(run_hour, cfg))
    d = cfg["data"]
    max_step = d["max_step_main"] if run_hour in (0, 12) else d["max_step_short"]
    return [s for s in range(1, min(h["until_h"], max_step) + 1) if s not in main]


def is_hourly_only(step: int, cfg: dict) -> bool:
    """A surface-only step (data.hourly): only data.hourly.params are fetched, only its charts drawn."""
    h = cfg["data"].get("hourly")
    return bool(h) and 0 < step <= h["until_h"] and step % cfg["data"]["publish_every_h"] != 0


def tp_window(step: int, cfg: dict) -> int:
    """Precipitation window ending at step (h); shorter at the first hourly steps (+1 h, +2 h)."""
    d = cfg["data"]
    w = d["tp_short_accum_h"] if step <= d["tp_switch_h"] else d["tp_long_accum_h"]
    return min(w, step) if step > 0 else w


def fetch_step(
    client: Client,
    run: dt.datetime,
    step: int,
    outdir: str | os.PathLike,
    cfg: dict,
    tp_only: bool = False,
    groups: list[dict] | None = None,
    tag: str = "",
) -> dict:
    """Fetch all configured field groups for one step into a single GRIB file.

    Returns {"path", "bytes", "seconds"}.
    """
    outdir = pathlib.Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    tag = tag or ("_tp" if tp_only else "")
    target = outdir / f"{run:%Y%m%d%H}_{step:03d}{tag}.grib2"
    parts = []
    t0 = time.monotonic()
    if groups is None:
        groups = [{"levtype": "sfc", "param": list(cfg["data"].get("accum_params", ["tp"]))}] if tp_only \
            else cfg["data"]["groups"]
    for i, g in enumerate(groups):
        part = target.with_suffix(f".part{i}")
        req = dict(
            date=run.strftime("%Y-%m-%d"),
            time=run.hour,
            type="fc",
            stream="oper",
            step=step,
            param=list(g["param"]),
            target=str(part),
        )
        if g["levtype"] == "pl":
            req["levtype"] = "pl"
            req["levelist"] = list(g["levelist"])
        client.retrieve(**req)
        parts.append(part)
    with open(target, "wb") as out:
        for p in parts:
            out.write(p.read_bytes())
            p.unlink()
    return {
        "path": str(target),
        "bytes": target.stat().st_size,
        "seconds": time.monotonic() - t0,
    }

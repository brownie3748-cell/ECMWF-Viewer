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
    return list(range(0, max_step + 1, d["publish_every_h"]))


def tp_window(step: int, cfg: dict) -> int:
    d = cfg["data"]
    return d["tp_short_accum_h"] if step <= d["tp_switch_h"] else d["tp_long_accum_h"]


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
        groups = [{"levtype": "sfc", "param": ["tp"]}] if tp_only else cfg["data"]["groups"]
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

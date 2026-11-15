"""Source handling for Stage 1: run completeness checks and downloads with fallback."""

from __future__ import annotations

import datetime as dt
import logging

import requests
from ecmwf.opendata.client import SPLIT_RESOLUTIONS
from ecmwf.opendata.urls import URLS

from . import fetch

log = logging.getLogger(__name__)
SAS_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/token/ai4edataeuwest/ecmwf"
TIMEOUT = 20


class Sources:
    """Ordered list of data sources; every call falls back to the next source on failure."""

    def __init__(self, cfg: dict, order: list[str] | None = None):
        self.cfg = cfg
        self.order = order or cfg["data"]["source_order"]
        self._clients: dict = {}
        self._sas: dict = {}

    # ---------------------------------------------------------------- urls
    def _sas_token(self, source: str) -> str:
        if source != "azure":
            return ""
        if "t" not in self._sas:
            self._sas["t"] = requests.get(SAS_URL, timeout=TIMEOUT).json()["token"]
        return self._sas["t"]

    def index_url(self, source: str, run: dt.datetime, step: int) -> str:
        stream = "oper"
        if run.date() < dt.date(2026, 5, 12) and run.hour in (6, 18):
            stream = "scda"   # pre-50r1 layout (see ecmwf-opendata client.patch_stream)
        resol = self.cfg["data"]["resolution"]
        split = "-sfc" if resol in SPLIT_RESOLUTIONS else ""   # 0p1: separate -sfc / -pl files
        u = (f"{URLS[source]}/{run:%Y%m%d}/{run:%H}z/{self.cfg['data']['model']}/"
             f"{resol}/{stream}/{run:%Y%m%d%H}0000-{step}h-{stream}-fc{split}.index")
        t = self._sas_token(source)
        return f"{u}?{t}" if t else u

    # ---------------------------------------------------------- completeness
    def final_step(self, run: dt.datetime) -> int:
        d = self.cfg["data"]
        return d["max_step_main"] if run.hour in (0, 12) else d["max_step_short"]

    def run_complete(self, run: dt.datetime) -> str | None:
        """Return the first source on which the run's final step is published, else None."""
        step = self.final_step(run)
        for src in self.order:
            try:
                r = requests.head(self.index_url(src, run, step), timeout=TIMEOUT, allow_redirects=True)
                if r.status_code == 200:
                    return src
            except Exception as e:  # noqa: BLE001
                log.warning("HEAD %s %s+%d: %r", src, f"{run:%Y%m%d%H}", step, e)
        return None

    # ------------------------------------------------------------- download
    def _client(self, source: str):
        if source not in self._clients:
            self._clients[source] = fetch.make_client(source, self.cfg, retries=2, retry_after=5)
        return self._clients[source]

    def fetch_step(self, run: dt.datetime, step: int, outdir, tp_only: bool = False) -> dict:
        last = None
        for src in self.order:
            try:
                r = fetch.fetch_step(self._client(src), run, step, outdir, self.cfg, tp_only=tp_only)
                r["source"] = src
                return r
            except Exception as e:  # noqa: BLE001
                last = e
                log.warning("fetch %s+%d%s via %s failed: %r", f"{run:%Y%m%d%H}", step,
                            " (tp)" if tp_only else "", src, e)
        raise RuntimeError(f"all sources failed for {run:%Y%m%d%H}+{step}: {last!r}")


def make_sources(cfg: dict):
    """Sources for an ECMWF model, kim.KimSource for a KMA model (data.source = "kma")."""
    from . import kim

    return kim.KimSource(cfg) if kim.is_kma(cfg) else Sources(cfg)

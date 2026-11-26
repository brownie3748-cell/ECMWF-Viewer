"""KMA forecast images for the "예보" tab: 초단기예보 (ultra-short) and 단기예보 (short-range) grid images from the KMA
API Hub, copied as they are (no rendering) to the store under dfs/ with a small manifest dfs/index.json.

    dfs/index.json
    dfs/<product>/<issuance KST YYYYMMDDHHMM>/<element>_<valid KST YYYYMMDDHH>.png

Run by `python -m render dfs` from forecast.yml: its own workflow and concurrency group, so a two-hour KIM run in
publish.yml never holds it back; it also keeps its own KMA usage file (_ops/kmadfs-<KST day>.json) and its own
Class A counter (_ops/dfs-<month>.json), so the two jobs never write the same object.

Measured 2026-11-26 (measure M9): an unavailable valid time returns a grey "no data" map (the same bytes for every
element of a product), an unknown element a 172-byte blank; the hub snaps the ultra-short tm_fc to its 10-minute
issues; the short-range tm_fc must be an issuance hour.
"""

from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import hashlib
import json
import logging
import os
import time

import requests

from . import kim

log = logging.getLogger(__name__)
KST = dt.timezone(dt.timedelta(hours=9))
PRODUCTS = ("vsrt", "shrt")
BLANK_MAX_BYTES = 400            # unknown element: 172-byte one-colour PNG (measured)


def kst_now() -> dt.datetime:
    return dt.datetime.now(KST).replace(second=0, microsecond=0, tzinfo=None)


def latest_issue(product: str, cfg: dict, now: dt.datetime) -> dt.datetime:
    """Newest issuance (KST, naive) old enough to fetch."""
    p = cfg["dfs"][product]
    t = now - dt.timedelta(minutes=p["min_age_min"])
    t = t.replace(minute=0)
    hours = p.get("issue_hours_kst")
    while hours and t.hour not in hours:
        t -= dt.timedelta(hours=1)
    return t


def element_vars(cfg: dict, product: str) -> list[str]:
    return [e[product] for e in cfg["dfs"]["elements"] if e.get(product)]


def valid_hours(product: str, cfg: dict, fc: dt.datetime, now: dt.datetime) -> list[dt.datetime]:
    """Candidate valid hours: from the current hour (or the issuance hour, if later) to max_lead_h."""
    start = max(fc, now.replace(minute=0))
    end = fc + dt.timedelta(hours=cfg["dfs"][product]["max_lead_h"])
    out, t = [], start
    while t <= end:
        out.append(t)
        t += dt.timedelta(hours=1)
    return out


def key_for(cfg: dict, product: str, fc: dt.datetime, var: str, ef: dt.datetime) -> str:
    return f"{cfg['dfs']['prefix']}{product}/{fc:%Y%m%d%H%M}/{var}_{ef:%Y%m%d%H}.png"


class Hub:
    """KMA API Hub image requests with a daily call count of its own."""

    def __init__(self, cfg: dict, usage: dict):
        self.cfg = cfg
        self.d = cfg["dfs"]
        self.key = os.environ.get("KMA_APIHUB_KEY", "")
        self.usage = usage
        self.session = requests.Session()

    def url(self, product: str, var: str, fc: dt.datetime, ef: dt.datetime) -> str:
        p = self.d[product]
        q = self.d["query"].format(var=var, fc=f"{fc:%Y%m%d%H%M}", ef=f"{ef:%Y%m%d%H%M}")
        return f"{self.d['base']}{p['cgi']}?{q}{p.get('extra', '')}"

    def get(self, product: str, var: str, fc: dt.datetime, ef: dt.datetime) -> bytes | None:
        if not self.key:
            raise RuntimeError("KMA_APIHUB_KEY is not set")
        u = self.url(product, var, fc, ef) + "&authKey=" + self.key
        for attempt in range(4):                     # the hub resets connections now and then (measured)
            try:
                r = self.session.get(u, timeout=self.d["timeout_s"])
                self.usage["calls"] += 1
                self.usage["bytes"] += len(r.content)
                if r.status_code == 403 and "제한".encode() in r.content:
                    raise kim.QuotaError("KMA API Hub daily limit reached (HTTP 403)")
                if r.status_code == 200 and r.content[:4] == b"\x89PNG":
                    return r.content
                log.warning("%s %s %s: HTTP %s %r", product, var, f"{ef:%m%d%H}", r.status_code, r.content[:120])
            except kim.QuotaError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("%s %s %s: %r", product, var, f"{ef:%m%d%H}", e)
            time.sleep(3 * (attempt + 1))
        return None


def _sha(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()


def fetch_issue(hub: Hub, product: str, fc: dt.datetime, now: dt.datetime) -> tuple[dict, dict[str, bytes]]:
    """Images of one issuance: ({var: [valid KST YYYYMMDDHH, ...]}, {store key: png}).

    The "no data" map of this product is fetched first (a valid time far beyond the range); the first element
    then finds the last valid hour (stops at the first "no data"), and the other elements are fetched up to it."""
    cfg = hub.cfg
    vars_ = element_vars(cfg, product)
    hours = valid_hours(product, cfg, fc, now)
    nodata = hub.get(product, vars_[0], fc, fc + dt.timedelta(days=10))
    if nodata is None:
        # Without the reference the range end cannot be told from data (2026-11-26: connection resets lost it and
        # the grey +6 h map was published as a frame). Skip; the next run (30 min) tries again.
        log.warning("%s %s: no 'no data' reference image, skipped", product, f"{fc:%m%d%H%M}")
        return {}, {}
    nodata_sha = _sha(nodata)
    frames: dict[str, list[str]] = {}
    files: dict[str, bytes] = {}

    def ok(b: bytes | None) -> bool:
        return b is not None and len(b) > BLANK_MAX_BYTES and _sha(b) != nodata_sha

    first = vars_[0]
    last = None
    for ef in hours:                                   # range probe with the first element
        b = hub.get(product, first, fc, ef)
        if not ok(b):
            if b is not None:
                break                                  # "no data": the range ends here
            continue                                   # a failed request: skip this hour
        files[key_for(cfg, product, fc, first, ef)] = b
        frames.setdefault(first, []).append(f"{ef:%Y%m%d%H}")
        last = ef
    if last is None:
        return {}, {}
    todo = [(v, ef) for v in vars_[1:] for ef in hours if ef <= last]
    with cf.ThreadPoolExecutor(cfg["dfs"]["parallel"]) as ex:
        for (v, ef), b in zip(todo, ex.map(lambda t: hub.get(product, t[0], fc, t[1]), todo)):
            if ok(b):
                files[key_for(cfg, product, fc, v, ef)] = b
                frames.setdefault(v, []).append(f"{ef:%Y%m%d%H}")
    for v in frames:
        frames[v].sort()
    return frames, files


def usage_key(day: str) -> str:
    return f"_ops/kmadfs-{day}.json"


def run(cfg: dict, st, now: dt.datetime | None = None, force: bool = False) -> list[str]:
    """Fetch every product whose newest issuance is not published yet; upload; prune; write dfs/index.json.
    Returns summary lines."""
    now = now or kst_now()
    d = cfg["dfs"]
    day = f"{now:%Y-%m-%d}"
    usage = st.get_json(usage_key(day)) or {}
    if usage.get("day") != day:
        usage = {"day": day, "calls": 0, "bytes": 0}
    ku = st.get_json(f"_ops/kma-{day}.json") or {}                    # the KIM job's count today (publish.yml)
    kim_bytes = ku.get("bytes", 0) if ku.get("day") == day else 0
    man_key = d["prefix"] + "index.json"
    man = st.get_json(man_key) or {}
    products = man.get("products", {})
    hub = Hub(cfg, usage)
    lines, changed = [], False
    sizes = None
    for product in PRODUCTS:
        fc = latest_issue(product, cfg, now)
        have = products.get(product, [])
        if not force and have and have[-1]["fc"] == f"{fc:%Y%m%d%H%M}":
            lines.append(f"- {product}: {fc:%m-%d %H:%M} KST already published")
            continue
        est = len(element_vars(cfg, product)) * len(valid_hours(product, cfg, fc, now)) + 1
        est_bytes = est * d["est_bytes_per_image"]
        if usage["calls"] + est > d["max_calls_per_day"]:
            lines.append(f"- {product}: skipped, daily cap ({usage['calls']} used + ~{est} > {d['max_calls_per_day']})")
            continue
        if kim_bytes + usage["bytes"] + est_bytes > cfg["kma_quota"]["max_bytes_per_day"]:
            lines.append(f"- {product}: skipped, KMA bytes guard (KIM {kim_bytes / 1e9:.2f} GB + this job "
                         f"{usage['bytes'] / 1e9:.2f} GB + ~{est_bytes / 1e9:.2f} GB > "
                         f"{cfg['kma_quota']['max_bytes_per_day'] / 1e9:.1f} GB)")
            continue
        t0 = time.monotonic()
        calls0, bytes0 = usage["calls"], usage["bytes"]
        frames, files = fetch_issue(hub, product, fc, now)
        if not files:
            lines.append(f"- {product}: {fc:%m-%d %H:%M} KST not available yet ({usage['calls'] - calls0} calls)")
            continue
        st.check_budget(len(files) + 2)
        with cf.ThreadPoolExecutor(cfg["storage"]["upload_threads"]) as ex:
            list(ex.map(lambda kv: st.put_bytes(kv[0], kv[1], cfg["storage"]["image_cache_control"]), files.items()))
        have = [h for h in have if h["fc"] != f"{fc:%Y%m%d%H%M}"] + [{"fc": f"{fc:%Y%m%d%H%M}", "frames": frames}]
        drop, have = have[:-d["keep_issuances"]], have[-d["keep_issuances"]:]
        if drop:
            sizes = sizes if sizes is not None else st.list_sizes()
            for h in drop:
                st.delete_prefix(f"{d['prefix']}{product}/{h['fc']}/", sizes)
        products[product] = have
        changed = True
        n = sum(len(v) for v in frames.values())
        lines.append(f"- {product}: {fc:%m-%d %H:%M} KST, {n} images, {sum(map(len, files.values())) / 1e6:.1f} MB, "
                     f"{usage['calls'] - calls0} calls, {(usage['bytes'] - bytes0) / 1e6:.1f} MB from the hub, "
                     f"{time.monotonic() - t0:.0f} s (measured); valid {min(min(v) for v in frames.values())}.."
                     f"{max(max(v) for v in frames.values())} KST")
    st.put_bytes(usage_key(day), json.dumps(usage).encode(), "no-cache")
    if changed:
        man = {"schema": 1, "generated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "tz": "KST", "elements": d["elements"], "attribution": d["credit"], "products": products}
        st.put_bytes(man_key, json.dumps(man, ensure_ascii=False).encode(), "no-cache, max-age=0")
    lines.append(f"- KMA API Hub today (this job's file): {usage['calls']} calls, {usage['bytes'] / 1e6:.1f} MB")
    return lines

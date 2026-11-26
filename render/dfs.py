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


def probe_range(hub: Hub, product: str, fc: dt.datetime, now: dt.datetime, files: dict) -> tuple[str | None, list]:
    """Find the issuance's last valid hour with its first element: (last YYYYMMDDHH or None, frames of that element).

    The "no data" map of this product is fetched first (a valid time far beyond the range). Leading "no data"
    hours are skipped (단기 starts at issuance + 1 h, measured 2026-11-26: 11 KST issuance, 11시 = no data); after
    the first image the first "no data" hour ends the range."""
    cfg = hub.cfg
    first = element_vars(cfg, product)[0]
    nodata = hub.get(product, first, fc, fc + dt.timedelta(days=10))
    if nodata is None:
        # Without the reference the range end cannot be told from data (2026-11-26: connection resets lost it and
        # the grey +6 h map was published as a frame). Skip; the next run (30 min) tries again.
        log.warning("%s %s: no 'no data' reference image, skipped", product, f"{fc:%m%d%H%M}")
        return None, []
    nodata_sha = _sha(nodata)
    frames, last, lead_skips = [], None, 0
    for ef in valid_hours(product, cfg, fc, now):
        b = hub.get(product, first, fc, ef)
        if b is None:
            continue                                   # a failed request: skip this hour
        if len(b) <= BLANK_MAX_BYTES or _sha(b) == nodata_sha:
            if last is None and lead_skips < 3:
                lead_skips += 1                        # before the first image: not the end yet
                continue
            break                                      # "no data": the range ends here
        files[key_for(cfg, product, fc, first, ef)] = b
        frames.append(f"{ef:%Y%m%d%H}")
        last = ef
    return (f"{last:%Y%m%d%H}" if last else None), frames


def fetch_element(hub: Hub, product: str, var: str, fc: dt.datetime, now: dt.datetime, first: str, last: str,
                  files: dict) -> list[str]:
    """One element's images between the issuance's first and last valid hour found by the probe (and from the
    current hour on)."""
    cfg = hub.cfg
    lo, hi = (dt.datetime.strptime(x, "%Y%m%d%H") for x in (first, last))
    hours = [ef for ef in valid_hours(product, cfg, fc, now) if lo <= ef <= hi]
    frames = []
    with cf.ThreadPoolExecutor(cfg["dfs"]["parallel"]) as ex:
        for ef, b in zip(hours, ex.map(lambda e: hub.get(product, var, fc, e), hours)):
            if b is not None and len(b) > BLANK_MAX_BYTES:
                files[key_for(cfg, product, fc, var, ef)] = b
                frames.append(f"{ef:%Y%m%d%H}")
    return frames


def usage_key(day: str) -> str:
    return f"_ops/kmadfs-{day}.json"


def run(cfg: dict, st, now: dt.datetime | None = None, force: bool = False) -> list[str]:
    """Fetch the newest issuance of every product, element by element: each element's images are uploaded and
    dfs/index.json and the usage count are saved as soon as the element is done, so a cancelled job keeps its work
    and the next run resumes the missing elements (2026-11-26: the first run hit the 25-minute job limit with
    ~700 calls fetched and nothing uploaded). Returns summary lines."""
    now = now or kst_now()
    d = cfg["dfs"]
    t_start = time.monotonic()
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
    lines = []

    def save():
        st.put_bytes(usage_key(day), json.dumps(usage).encode(), "no-cache")
        m = {"schema": 1, "generated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "tz": "KST", "elements": d["elements"], "attribution": d["credit"], "products": products}
        st.put_bytes(man_key, json.dumps(m, ensure_ascii=False).encode(), "no-cache, max-age=0")

    def upload(files: dict):
        st.check_budget(len(files) + 2)
        with cf.ThreadPoolExecutor(cfg["storage"]["upload_threads"]) as ex:
            list(ex.map(lambda kv: st.put_bytes(kv[0], kv[1], cfg["storage"]["image_cache_control"]), files.items()))

    for product in PRODUCTS:
        fc = latest_issue(product, cfg, now)
        fcs = f"{fc:%Y%m%d%H%M}"
        have = products.setdefault(product, [])
        entry = have[-1] if have and have[-1]["fc"] == fcs else None
        if entry and entry.get("complete", True) and not force:
            lines.append(f"- {product}: {fc:%m-%d %H:%M} KST already published")
            continue
        if entry is None or force:
            have[:] = [h for h in have if h["fc"] != fcs]
            entry = {"fc": fcs, "frames": {}, "done": [], "complete": False}
        vars_ = element_vars(cfg, product)
        est = (len(vars_) - len(entry["done"])) * len(valid_hours(product, cfg, fc, now)) + 1
        est_bytes = est * d["est_bytes_per_image"]
        if usage["calls"] + est > d["max_calls_per_day"]:
            lines.append(f"- {product}: skipped, daily cap ({usage['calls']} used + ~{est} > {d['max_calls_per_day']})")
            continue
        if kim_bytes + usage["bytes"] + est_bytes > cfg["kma_quota"]["max_bytes_per_day"]:
            lines.append(f"- {product}: skipped, KMA bytes guard (KIM {kim_bytes / 1e9:.2f} GB + this job "
                         f"{usage['bytes'] / 1e9:.2f} GB + ~{est_bytes / 1e9:.2f} GB > "
                         f"{cfg['kma_quota']['max_bytes_per_day'] / 1e9:.1f} GB)")
            continue
        t0, calls0, bytes0, n0 = time.monotonic(), usage["calls"], usage["bytes"], \
            sum(len(v) for v in entry["frames"].values())
        if not entry.get("last"):
            files: dict = {}
            last, frames = probe_range(hub, product, fc, now, files)
            if last is None:
                st.put_bytes(usage_key(day), json.dumps(usage).encode(), "no-cache")
                lines.append(f"- {product}: {fc:%m-%d %H:%M} KST not available yet ({usage['calls'] - calls0} calls)")
                continue
            upload(files)
            entry.update(first=frames[0], last=last, frames={vars_[0]: frames}, done=[vars_[0]])
            have.append(entry)
            save()
        stopped = False
        for v in vars_:
            if v in entry["done"]:
                continue
            if time.monotonic() - t_start > d["time_budget_min"] * 60:
                stopped = True                          # the next run resumes from here
                break
            files = {}
            entry["frames"][v] = fetch_element(hub, product, v, fc, now, entry["first"], entry["last"], files)
            upload(files)
            entry["done"].append(v)
            save()
        if not stopped:
            entry["complete"] = True
            drop = have[:-d["keep_issuances"]]
            del have[:-d["keep_issuances"]]
            if drop:
                sizes = st.list_sizes()
                for h in drop:
                    st.delete_prefix(f"{d['prefix']}{product}/{h['fc']}/", sizes)
            save()
        n = sum(len(v) for v in entry["frames"].values()) - n0
        lines.append(f"- {product}: {fc:%m-%d %H:%M} KST, {n} images, {usage['calls'] - calls0} calls, "
                     f"{(usage['bytes'] - bytes0) / 1e6:.1f} MB from the hub, {time.monotonic() - t0:.0f} s (measured); "
                     f"valid to {entry['last']} KST; "
                     + ("complete" if not stopped else f"stopped at the {d['time_budget_min']}-minute budget, "
                                                       f"{len(entry['done'])}/{len(vars_)} elements (resumes next run)"))
    lines.append(f"- KMA API Hub today (this job's file): {usage['calls']} calls, {usage['bytes'] / 1e6:.1f} MB")
    return lines

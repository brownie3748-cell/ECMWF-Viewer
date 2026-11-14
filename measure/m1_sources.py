"""M1: data sources (ECMWF portal and AWS/Azure/Google mirrors) as seen from this machine.

Measures, per source:
  * latest 00/12Z run whose final step (240 h) exists
  * download time and size of one step's 30 fields (steps 120 and 240 of that run)
  * publication delay of the final step (Last-Modified minus nominal run time)
  * retention: which past runs still exist (sparse probe back to ~2 years) + oldest listed date
The step 120/240 GRIB files of the first successful source are kept in work/data for M2/M3/M5.
"""

from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import email.utils
import re
import shutil
import statistics

import requests
from ecmwf.opendata.urls import URLS

from measure.common import CFG, DATA, fmt_run, guarded, md_table, now_utc, save
from render import fetch

SOURCES = CFG["data"]["sources"]
SAS_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/token/ai4edataeuwest/ecmwf"
TIMEOUT = 20
_SAS: dict = {}


def sas(source):
    if source != "azure":
        return ""
    if "t" not in _SAS:
        _SAS["t"] = requests.get(SAS_URL, timeout=TIMEOUT).json()["token"]
    return _SAS["t"]


def final_step(run: dt.datetime) -> int:
    return CFG["data"]["max_step_main"] if run.hour in (0, 12) else CFG["data"]["max_step_short"]


def file_url(source, run: dt.datetime, step: int) -> str:
    stream = "oper"
    if run.date() < dt.date(2026, 5, 12) and run.hour in (6, 18):
        stream = "scda"   # pre-50r1 layout (see ecmwf-opendata client.patch_stream)
    u = (f"{URLS[source]}/{run:%Y%m%d}/{run:%H}z/ifs/{CFG['data']['resolution']}/{stream}/"
         f"{run:%Y%m%d%H}0000-{step}h-{stream}-fc.grib2")
    t = sas(source)
    return f"{u}?{t}" if t else u


def head(source, run, step):
    try:
        r = requests.head(file_url(source, run, step), timeout=TIMEOUT, allow_redirects=True)
        lm = r.headers.get("Last-Modified")
        lm_dt = email.utils.parsedate_to_datetime(lm).replace(tzinfo=None) if lm else None
        return {"status": r.status_code, "last_modified": lm_dt,
                "size": int(r.headers.get("Content-Length", 0) or 0)}
    except Exception as e:  # noqa: BLE001
        return {"status": None, "error": repr(e)}


def oldest_listed(source) -> str | None:
    """Oldest YYYYMMDD directory in the bucket/portal listing (first page is enough: sorted)."""
    root = URLS[source]
    try:
        if source == "aws":
            txt = requests.get(f"{root}/?list-type=2&delimiter=/", timeout=TIMEOUT).text
        elif source == "google":
            txt = requests.get(f"{root}?delimiter=/", timeout=TIMEOUT).text
        elif source == "azure":
            txt = requests.get(f"{root}?restype=container&comp=list&delimiter=/&{sas(source)}",
                               timeout=TIMEOUT).text
        else:
            txt = requests.get(f"{root}/", timeout=TIMEOUT).text
        dates = sorted(set(re.findall(r"(20\d{6})/", txt)))
        return dates[0] if dates else None
    except Exception as e:  # noqa: BLE001
        return f"error: {e!r}"


def candidate_runs(now: dt.datetime) -> list[dt.datetime]:
    base = now.replace(minute=0, second=0, microsecond=0, hour=(now.hour // 6) * 6)
    recent = [base - dt.timedelta(hours=6 * i) for i in range(0, 17)]   # last 4 days, all cycles
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    sparse = [day0 - dt.timedelta(days=d) for d in
              (5, 6, 7, 8, 10, 14, 21, 30, 45, 60, 90, 120, 180, 270, 365, 540, 730)]
    return recent + sparse


@guarded("m1", "M1 sources")
def main():
    now = now_utc()
    runs = candidate_runs(now)
    res = {"measured_at_utc": now.isoformat(timespec="seconds"), "sources": {}}

    # ---- availability, publication delay, retention (HEAD probes) ----
    for src in SOURCES:
        s = {"reachable": False}
        try:
            sas(src)
            with cf.ThreadPoolExecutor(8) as ex:
                probes = list(ex.map(lambda r: (r, head(src, r, final_step(r))), runs))
        except Exception as e:  # noqa: BLE001
            s["error"] = repr(e)
            res["sources"][src] = s
            continue
        s["reachable"] = any(p["status"] is not None for _, p in probes)
        s["errors"] = sorted({p.get("error", "")[:200] for _, p in probes if p.get("error")})
        avail = [(r, p) for r, p in probes if p["status"] == 200]
        s["probes"] = [{"run": fmt_run(r), "final_step": final_step(r), "status": p["status"],
                        "last_modified": p.get("last_modified"), "size_mb": round(p.get("size", 0) / 1e6, 1)}
                       for r, p in probes]
        mains = [r for r, _ in avail if r.hour in (0, 12)]
        s["latest_main_run"] = fmt_run(max(mains)) if mains else None
        recent_set = set(runs[:17])
        s["recent_runs_available"] = sum(1 for r, _ in avail if r in recent_set)
        s["oldest_probe_available"] = fmt_run(min(r for r, _ in avail)) if avail else None
        s["oldest_listed_date"] = oldest_listed(src)
        delays = {}
        for r, p in avail:
            if r in recent_set and p.get("last_modified"):
                delays[fmt_run(r)] = round((p["last_modified"] - r).total_seconds() / 3600, 2)
        s["final_step_delay_h"] = delays
        res["sources"][src] = s

    mains = [res["sources"][s].get("latest_main_run") for s in SOURCES if res["sources"][s].get("latest_main_run")]
    common = min(mains) if mains else None
    res["common_latest_main_run"] = common

    # ---- download timing: 30 fields of steps 120 and 240 ----
    kept = {}
    if common:
        run = dt.datetime.strptime(common, "%Y%m%d%H")
        for src in SOURCES:
            s = res["sources"][src]
            s["downloads"] = []
            if not s.get("reachable"):
                continue
            try:
                client = fetch.make_client(src, CFG, retries=2, retry_after=5)
            except Exception as e:  # noqa: BLE001
                s["downloads"].append({"error": repr(e)})
                continue
            for step in (120, 240):
                tmp = DATA / f"m1_{src}"
                try:
                    r = fetch.fetch_step(client, run, step, tmp, CFG)
                    r = {"step": step, "mb": round(r["bytes"] / 1e6, 2), "seconds": round(r["seconds"], 1),
                         "mb_per_s": round(r["bytes"] / 1e6 / max(r["seconds"], 1e-3), 2), "path": r["path"]}
                    if step not in kept:
                        dst = DATA / f"{common}_{step:03d}.grib2"
                        shutil.copy(r["path"], dst)
                        kept[step] = str(dst)
                except Exception as e:  # noqa: BLE001
                    r = {"step": step, "error": repr(e)[:300]}
                s["downloads"].append(r)
            shutil.rmtree(DATA / f"m1_{src}", ignore_errors=True)
    res["kept_files"] = kept

    # ---- ranking ----
    def speed(src):
        d = [x for x in res["sources"][src].get("downloads", []) if "seconds" in x]
        return statistics.mean(x["seconds"] for x in d) if len(d) == 2 else float("inf")

    ranking = sorted([s for s in SOURCES if speed(s) < float("inf")], key=speed)
    res["ranking"] = ranking
    res["status"] = "PASS" if ranking else "FAIL"
    save("m1", res, render_md(res))


def render_md(res) -> str:
    rows = []
    for src, s in res["sources"].items():
        d = s.get("downloads", [])
        dl = "; ".join(f"+{x['step']}h {x['mb']} MB / {x['seconds']} s" if "seconds" in x
                       else f"+{x.get('step')}h ERR {x.get('error', '')[:60]}" for x in d) or "-"
        delays = list(s.get("final_step_delay_h", {}).values())
        dly = (f"median {statistics.median(delays):.1f} h (n={len(delays)}, "
               f"{min(delays):.1f}-{max(delays):.1f})" if delays else "n/a")
        status = "PASS" if d and all("seconds" in x for x in d) else ("PARTIAL" if any("seconds" in x for x in d) else "FAIL")
        oldest = s.get("oldest_listed_date")
        oldest = "error" if oldest and str(oldest).startswith("error") else oldest
        rows.append([src, status, s.get("latest_main_run"), dl, dly, s.get("recent_runs_available"),
                     s.get("oldest_probe_available"), oldest])
    md = ["## M1 sources (measured from this runner)\n",
          f"Measured at {res['measured_at_utc']} UTC. Common latest complete 00/12Z run: "
          f"`{res.get('common_latest_main_run')}`. Ranking (fastest first): {res.get('ranking')}.\n",
          md_table(["source", "status", "latest 00/12Z (240h present)", "download 30 fields (measured)",
                    "final-step delay after run time (measured, Last-Modified)",
                    "recent runs present (of 17 in last 4 days)", "oldest probed run present", "oldest listed dir"],
                   rows)]
    for src, s in res["sources"].items():
        errs = [s["error"]] if s.get("error") else s.get("errors", [])
        if errs:
            md.append(f"- {src}: {len(errs)} distinct error(s), first: `{errs[0][:160]}`\n")
    md.append("\nRetention probe: every 6 h for the last 4 days, then 00Z at 5,6,7,8,10,14,21,30,45,60,90,"
              "120,180,270,365,540,730 days back. Full probe table in m1.json.\n")
    return "\n".join(md)


if __name__ == "__main__":
    main()

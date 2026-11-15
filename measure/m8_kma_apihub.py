"""M8: KMA API Hub (KIM regional model) reachability and file format, measured from a GitHub runner.

Measurement only: nothing is rendered or published. The API key comes from the KMA_APIHUB_KEY secret and is
never printed (it is masked in the log and replaced by *** in every reported URL).

Inputs (environment):
  KMA_APIHUB_KEY   API Hub auth key (GitHub secret)
  M8_URLS          one or more API Hub request URLs, separated by spaces or newlines, copied from the
                   API Hub "API 활용" page WITHOUT the key; the script appends authKey=<key>.
"""

from __future__ import annotations

import os
import pathlib
import re
import time

import requests

from measure.common import DATA, guarded, md_table, save

TIMEOUT = 60
MAX_BYTES = 400_000_000
ATTEMPTS = 5            # connections from abroad are often reset; every attempt is counted in the report
HUB = "https://apihub.kma.go.kr/"
# Default requests when M8_URLS is empty: KIM regional (KIMR, r030), yesterday 00 UTC, +24 h, whole domain,
# GRIB2, from the "KIM 경량화 다운로드" API (apihub.kma.go.kr/apiList.do?seqApi=9, section 1.1).
# Variable names from the KIM regional/local variable list (pres: tmpr ghgt ugrd vgrd spfh rhwt; unis: pslp psfc apcp).
_DL = "https://apihub.kma.go.kr/api/typ06/url/nwp_nc_vars_down.php?group=KIMR&tmfc={tmfc}&hf=24&dataType=GB2"
DEFAULTS = [
    _DL + "&data=P&vars=ghgt&lvl_lst=500",                       # one field: size, grid, projection
    _DL + "&data=P&vars=ghgt,tmpr,ugrd,vgrd&lvl_lst=850",        # several variables in one request?
    _DL + "&data=P&vars=tmpr&lvl_lst=925,850,700,500",           # several levels in one request?
    _DL + "&data=U&vars=pslp",
    _DL + "&data=U&vars=apcp",
]


def _mask(s: str, key: str) -> str:
    return s.replace(key, "***") if key else s


def _with_key(url: str, key: str) -> str:
    url = re.sub(r"([?&])authKey=[^&]*", r"\1", url).rstrip("&?")
    return url + ("&" if "?" in url else "?") + "authKey=" + key


def _kind(head: bytes) -> str:
    if head.startswith(b"GRIB"):
        return "GRIB"
    if head.startswith(b"\x89HDF"):
        return "NetCDF4/HDF5"
    if head.startswith(b"CDF"):
        return "NetCDF3"
    if head.startswith(b"\x1f\x8b"):
        return "gzip"
    if head.startswith(b"PK"):
        return "zip"
    if head.lstrip()[:1] in (b"{", b"["):
        return "JSON"
    if head.lstrip()[:1] == b"<":
        return "XML/HTML"
    return "other"


def _grib_summary(path: pathlib.Path) -> tuple[list[list], dict]:
    import eccodes

    rows, grid = [], {}
    keys = ["shortName", "name", "typeOfLevel", "level", "stepRange", "units", "dataDate", "dataTime"]
    gkeys = ["gridType", "Nx", "Ny", "Ni", "Nj", "DxInMetres", "DyInMetres", "latitudeOfFirstGridPointInDegrees",
             "longitudeOfFirstGridPointInDegrees", "LoVInDegrees", "Latin1InDegrees", "Latin2InDegrees",
             "iDirectionIncrementInDegrees", "jDirectionIncrementInDegrees", "packingType", "bitsPerValue"]
    with open(path, "rb") as fh:
        while True:
            h = eccodes.codes_grib_new_from_file(fh)
            if h is None:
                break
            try:
                row = []
                for k in keys:
                    try:
                        row.append(eccodes.codes_get(h, k))
                    except Exception:  # noqa: BLE001
                        row.append(None)
                rows.append(row)
                if not grid:
                    for k in gkeys:
                        try:
                            grid[k] = eccodes.codes_get(h, k)
                        except Exception:  # noqa: BLE001
                            pass
            finally:
                eccodes.codes_release(h)
    return rows, grid


def _get_retry(url: str, rec: dict):
    last = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return requests.get(url, timeout=TIMEOUT, stream=True)
        except Exception as e:  # noqa: BLE001
            last = e
            rec["failed_attempts"].append(f"get {attempt}: {type(e).__name__}")
            time.sleep(5)
    raise last


@guarded("m8", "M8 KMA API Hub (KIM regional)")
def main():
    key = os.environ.get("KMA_APIHUB_KEY", "")
    if key:
        print(f"::add-mask::{key}")
    urls = [u for u in re.split(r"\s+", os.environ.get("M8_URLS", "").strip()) if u]
    if not urls:
        import datetime as dt
        tmfc = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).strftime("%Y%m%d00")
        urls = [u.format(tmfc=tmfc) for u in DEFAULTS]
    md = ["## M8 KMA API Hub (KIM regional) - measurement only", ""]
    data = {"key_present": bool(key), "requests": []}

    # 1) reachability of the hub itself from the runner (GitHub-hosted, outside Korea): 10 tries
    tries = []
    for _ in range(10):
        t0 = time.monotonic()
        try:
            r = requests.get(HUB, timeout=TIMEOUT)
            tries.append(f"{r.status_code} {time.monotonic() - t0:.1f}s")
        except Exception as e:  # noqa: BLE001
            tries.append(f"{type(e).__name__} {time.monotonic() - t0:.1f}s")
        time.sleep(2)
    hub = {"ok": sum(t.startswith("200") for t in tries), "of": len(tries), "tries": tries}
    data["hub"] = hub
    md += [f"Hub {HUB} from the runner: `{hub}`", f"API key secret present: {bool(key)}", ""]
    md.append(f"Requests: {'input m8_urls' if os.environ.get('M8_URLS', '').strip() else 'built-in KIM regional defaults'}")
    rows = []
    for i, u in enumerate(urls):
        full = _with_key(u, key) if key else u
        rec = {"url": _mask(full, key), "failed_attempts": []}
        for attempt in range(1, ATTEMPTS + 1):
            t0 = time.monotonic()
            try:
                requests.head(HUB, timeout=TIMEOUT)          # cheap probe; the real request follows
                break
            except Exception as e:  # noqa: BLE001
                rec["failed_attempts"].append(f"probe {attempt}: {type(e).__name__}")
                time.sleep(5)
        t0 = time.monotonic()
        try:
            with _get_retry(full, rec) as r:
                rec["status"] = r.status_code
                rec["content_type"] = r.headers.get("Content-Type")
                rec["content_disposition"] = r.headers.get("Content-Disposition")
                out = DATA / f"m8_{i}.bin"
                n = 0
                with open(out, "wb") as fh:
                    for chunk in r.iter_content(1 << 20):
                        fh.write(chunk)
                        n += len(chunk)
                        if n > MAX_BYTES:
                            rec["truncated"] = True
                            break
            rec["seconds"] = round(time.monotonic() - t0, 2)
            rec["bytes"] = n
            rec["mb_per_s"] = round(n / 1e6 / max(rec["seconds"], 1e-3), 2)
            head = out.read_bytes()[:2048]
            rec["kind"] = _kind(head)
            if rec["kind"] in ("JSON", "XML/HTML", "other"):
                rec["head_text"] = _mask(head[:600].decode("utf-8", "replace"), key)
            if rec["kind"] == "GRIB":
                msgs, grid = _grib_summary(out)
                rec["messages"] = len(msgs)
                rec["grid"] = grid
                rec["fields"] = msgs[:60]
            out.unlink(missing_ok=True)
        except Exception as e:  # noqa: BLE001
            rec["error"] = repr(e)
            rec["seconds"] = round(time.monotonic() - t0, 2)
        data["requests"].append(rec)
        rows.append([i, rec.get("status"), rec.get("kind"), rec.get("bytes"), rec.get("seconds"), rec.get("mb_per_s"),
                     rec.get("messages"), len(rec["failed_attempts"]), rec["url"][:140]])
    if rows:
        md += [md_table(["#", "HTTP", "kind", "bytes", "s", "MB/s", "GRIB msgs", "failed tries", "url (key masked)"], rows)]
    for i, rec in enumerate(data["requests"]):
        if rec.get("grid"):
            md += [f"### Request {i}: grid", "```", "\n".join(f"{k} = {v}" for k, v in rec["grid"].items()), "```"]
            md += [md_table(["shortName", "name", "typeOfLevel", "level", "step", "units", "date", "time"],
                            rec["fields"][:40])]
        if rec.get("head_text"):
            md += [f"### Request {i}: first bytes", "```", rec["head_text"], "```"]
        if rec.get("content_disposition"):
            md.append(f"Request {i} Content-Disposition: `{rec['content_disposition']}`")
    save("m8", data, "\n".join(md) + "\n")


if __name__ == "__main__":
    main()

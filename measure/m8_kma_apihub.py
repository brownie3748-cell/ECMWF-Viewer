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
HUB = "https://apihub.kma.go.kr/"


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


@guarded("m8", "M8 KMA API Hub (KIM regional)")
def main():
    key = os.environ.get("KMA_APIHUB_KEY", "")
    if key:
        print(f"::add-mask::{key}")
    urls = [u for u in re.split(r"\s+", os.environ.get("M8_URLS", "").strip()) if u]
    md = ["## M8 KMA API Hub (KIM regional) - measurement only", ""]
    data = {"key_present": bool(key), "requests": []}

    # 1) reachability of the hub itself from the runner (GitHub-hosted, outside Korea)
    t0 = time.monotonic()
    try:
        r = requests.get(HUB, timeout=TIMEOUT)
        hub = {"status": r.status_code, "seconds": round(time.monotonic() - t0, 2), "bytes": len(r.content)}
    except Exception as e:  # noqa: BLE001
        hub = {"error": repr(e), "seconds": round(time.monotonic() - t0, 2)}
    data["hub"] = hub
    md += [f"Hub {HUB} from the runner: `{hub}`", f"API key secret present: {bool(key)}", ""]
    if not urls:
        md.append("No request URLs given (input `m8_urls`): only reachability measured.")
    rows = []
    for i, u in enumerate(urls):
        full = _with_key(u, key) if key else u
        rec = {"url": _mask(full, key)}
        t0 = time.monotonic()
        try:
            with requests.get(full, timeout=TIMEOUT, stream=True) as r:
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
                     rec.get("messages"), rec["url"][:140]])
    if rows:
        md += [md_table(["#", "HTTP", "kind", "bytes", "s", "MB/s", "GRIB msgs", "url (key masked)"], rows)]
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

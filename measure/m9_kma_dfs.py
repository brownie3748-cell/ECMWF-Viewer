"""M9: KMA API Hub forecast images (초단기예보 / 단기예보 grid images, `nph-dfs_vsrt_ana2`, `nph-dfs_shrt_ana_5d`),
measured from a GitHub runner. Measurement only: nothing is published. The key comes from KMA_APIHUB_KEY and is
masked everywhere.

What it finds out (owner 2026-11-26, a new "예보" tab):
- which elements (data1) each product serves, the image format, size and dimensions, the time per request;
- which tm_fc values are accepted (exact issuance only, or any time snapped to the last issuance);
- how far ahead each product goes (tm_ef);
- what an unavailable time returns.
Images go to work/images/m9/ (artifact stage0-images).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import os
import time

import requests

from measure.common import DATA, IMAGES, guarded, md_table, save

TIMEOUT = 60
KST = dt.timezone(dt.timedelta(hours=9))
BASE = "https://apihub.kma.go.kr/api/typ03/cgi/dfs/"
# Copied from the owner's example URLs (2026-11-26) without the key; data1 / tm_fc / tm_ef are filled in.
COMMON = ("data0=GEMD&data1={var}&tm_fc={fc}&tm_ef={ef}&dtm=H0&map=G1&mask=M&color=E&size=600&effect={effect}"
          "&overlay=S&zoom_rate=2&zoom_level=0&zoom_x=0000000&zoom_y=0000000&auto_man=m&mode=I")
SHRT = BASE + "nph-dfs_shrt_ana_5d?" + COMMON + "&interval=1"
VSRT = BASE + "nph-dfs_vsrt_ana2?" + COMMON
SHRT_ISSUE_H = (2, 5, 8, 11, 14, 17, 20, 23)          # KST issuance hours of the short-range forecast


def _t(d: dt.datetime) -> str:
    return d.strftime("%Y%m%d%H%M")


def _png_info(b: bytes) -> dict:
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(b))
        info = {"format": im.format, "w": im.size[0], "h": im.size[1], "mode": im.mode}
        if im.mode in ("P", "L", "RGB", "RGBA"):
            c = im.convert("RGB").getcolors(1 << 16)
            info["colours"] = len(c) if c else ">65536"
        return info
    except Exception as e:  # noqa: BLE001
        return {"format": None, "err": type(e).__name__, "head": b[:200].decode("utf-8", "replace")}


@guarded("m9", "M9 KMA forecast images")
def main():
    key = os.environ.get("KMA_APIHUB_KEY", "")
    if key:
        print(f"::add-mask::{key}")
    out_dir = IMAGES / "m9"
    out_dir.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(KST).replace(second=0, microsecond=0)
    # latest short-range issuance at least 40 min old, and the one before
    d = now.replace(minute=0)
    while d.hour not in SHRT_ISSUE_H or now - d < dt.timedelta(minutes=40):
        d -= dt.timedelta(hours=1)
    fc = d
    fc_prev = fc - dt.timedelta(hours=3)
    vh = now.replace(minute=0) - dt.timedelta(hours=1)          # an ultra-short hour surely issued
    reqs: list[tuple[str, str, str]] = []                        # (group, label, url)

    def add(group, label, tmpl, var, f, e, effect="NTL"):
        reqs.append((group, label, tmpl.format(var=var, fc=_t(f), ef=_t(e), effect=effect)))

    # A: short-range elements
    for v in ("TMP", "POP", "PTY", "PCP", "SNO", "SKY", "REH", "WSD", "TMN", "TMX", "VEC", "WAV"):
        add("A shrt var", v, SHRT, v, fc, fc + dt.timedelta(hours=4))
    # B: short-range horizon (PTY), on the hour
    for h in (1, 24, 48, 66, 72, 73, 75, 96, 120, 121, 123, 141, 144, 150):
        add("B shrt +h", f"+{h}h", SHRT, "PTY", fc, fc + dt.timedelta(hours=h))
    # C: tm_fc tolerance and an unavailable time
    add("C shrt fc", "fc+1h (not an issuance)", SHRT, "PTY", fc + dt.timedelta(hours=1), fc + dt.timedelta(hours=6))
    add("C shrt fc", "fc+10min", SHRT, "PTY", fc + dt.timedelta(minutes=10), fc + dt.timedelta(hours=6))
    add("C shrt fc", "previous issuance", SHRT, "PTY", fc_prev, fc + dt.timedelta(hours=6))
    add("C shrt fc", "next issuance (future)", SHRT, "PTY", fc + dt.timedelta(hours=3), fc + dt.timedelta(hours=6))
    add("C shrt fc", "effect=GTL", SHRT, "PTY", fc, fc + dt.timedelta(hours=6), "GTL")
    # D: ultra-short tm_fc minutes
    for m in (0, 10, 20, 30, 40, 50):
        f = vh.replace(minute=m)
        add("D vsrt fc", f"fc HH:{m:02d}", VSRT, "T1H", f, vh + dt.timedelta(hours=2))
    # E: ultra-short elements (fc HH:00, +2 h)
    for v in ("T1H", "RN1", "SKY", "PTY", "REH", "WSD", "LGT", "VEC", "TMP", "PCP"):
        add("E vsrt var", v, VSRT, v, vh, vh + dt.timedelta(hours=2))
    # F: ultra-short horizon
    for h in range(0, 8):
        add("F vsrt +h", f"+{h}h", VSRT, "PTY", vh, vh + dt.timedelta(hours=h))
    add("F vsrt +h", "effect=GTL +2h", VSRT, "SKY", vh, vh + dt.timedelta(hours=2), "GTL")

    rows, recs = [], []
    hashes: dict[str, str] = {}
    for i, (group, label, url) in enumerate(reqs):
        rec = {"group": group, "label": label, "url": url}
        t0 = time.monotonic()
        try:
            r = None
            for attempt in range(3):
                try:
                    r = requests.get(url + f"&rand={i}&authKey={key}", timeout=TIMEOUT)
                    break
                except Exception as e:  # noqa: BLE001
                    rec.setdefault("retries", []).append(type(e).__name__)
                    time.sleep(3)
            if r is None:
                raise RuntimeError("no response")
            b = r.content
            rec.update(status=r.status_code, ctype=r.headers.get("Content-Type"), bytes=len(b),
                       seconds=round(time.monotonic() - t0, 2), **_png_info(b))
            h = hashlib.sha1(b).hexdigest()[:10]
            rec["sha"] = h
            rec["same_as"] = hashes.get(h)
            hashes.setdefault(h, f"#{i}")
            if rec.get("format"):
                (out_dir / f"{i:02d}_{group.split()[0]}_{label.replace(' ', '_').replace('/', '')}.{rec['format'].lower()}"
                 ).write_bytes(b)
        except Exception as e:  # noqa: BLE001
            rec.update(error=repr(e).replace(key, "***") if key else repr(e), seconds=round(time.monotonic() - t0, 2))
        recs.append(rec)
        rows.append([i, group, label, rec.get("status"), rec.get("format") or rec.get("ctype"), rec.get("bytes"),
                     f"{rec.get('w')}x{rec.get('h')}" if rec.get("w") else "", rec.get("colours"), rec.get("seconds"),
                     rec.get("same_as") or "", (rec.get("head") or rec.get("error") or "")[:90].replace("|", "/")])
        time.sleep(0.5)
    md = ["## M9 KMA forecast images (초단기 / 단기) - measurement only", "",
          f"Now {now:%Y-%m-%d %H:%M} KST; short-range issuance used {fc:%m-%d %H:%M} KST; ultra-short hour {vh:%m-%d %H:%M} KST.",
          f"Requests: {len(reqs)} (each one API Hub call).", "",
          md_table(["#", "group", "case", "HTTP", "format", "bytes", "size", "colours", "s", "same image as", "text / error"],
                   rows)]
    save("m9", {"now_kst": now.isoformat(), "shrt_fc": fc.isoformat(), "vsrt_hour": vh.isoformat(), "requests": recs},
         "\n".join(md))


if __name__ == "__main__":
    main()

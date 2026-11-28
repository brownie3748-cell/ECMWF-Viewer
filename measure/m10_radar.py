"""M10: sources for a '강수 비교' tab (owner 2026-11-28): observed radar accumulated precipitation (1 h / 3 h) next to
the models' and the KMA forecast's rainfall, all on the KIM Korea map. Measurement only: nothing is published. The
API Hub key comes from KMA_APIHUB_KEY and is masked everywhere.

What it finds out (from a GitHub runner; the sandbox cannot reach apihub.kma.go.kr):
- API Hub radar composite: which of the candidate image / file endpoints answer, with an accumulation option
  (acc = 60 / 180 min), the format, size and time per request;
- API Hub forecast grids (단기 PCP, 초단기 RN1) as numbers, so they can be drawn on our own map (the 예보 tab's
  ready-made images are on KMA's map, which cannot be made to match the KIM map);
- the Radar Center's public image CGI (radar.kma.go.kr, no key): reachable from a runner, and the image it returns
  for the Korea map's lat/lon box (it takes any box: LU_LON / LU_LAT / RL_LON / RL_LAT).
Candidate URLs are best guesses; M10_URLS (space separated, without the key) adds the owner's own.
Images / files go to work/images/m10/ (artifact stage0-images).
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import io
import os
import time

import requests

from measure.common import IMAGES, guarded, md_table, save

TIMEOUT = 120
KST = dt.timezone(dt.timedelta(hours=9))
HUB = "https://apihub.kma.go.kr/api/"
RADAR_CGI = ("https://radar.kma.go.kr/cgi-bin/com_gis/radar_comp_gis_realtime_hsp?D_VER=HSP&D_TYPE=RN&COMP_MIX=2"
             "&IS_SMOOTH=1&FLICKER=0&HT=0&ACC={acc}&DATE={tm}&LU_LON=118.9&LU_LAT=42.0&RL_LON=133.4&RL_LAT=31.0"
             "&IMG_XDIM=900&IMG_YDIM=1000&X_DIST=1127000&Y_DIST=1281000&UNIT_BAR=0&ECHO_OPACITY=1")
SHRT_ISSUE_H = (2, 5, 8, 11, 14, 17, 20, 23)


def _t(d: dt.datetime) -> str:
    return d.strftime("%Y%m%d%H%M")


def _inspect(b: bytes) -> dict:
    """Image (format / size / colours), gzip (unpacked size, first bytes) or text (first lines, line count)."""
    info: dict = {}
    if b[:2] == b"\x1f\x8b":
        try:
            u = gzip.decompress(b)
            info.update(kind="gzip", unpacked=len(u), head=u[:64].hex())
        except Exception as e:  # noqa: BLE001
            info.update(kind="gzip?", err=repr(e))
        return info
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(b))
        info.update(kind=im.format, w=im.size[0], h=im.size[1], mode=im.mode)
        return info
    except Exception:  # noqa: BLE001
        pass
    txt = b[:4000].decode("utf-8", "replace") if b else ""
    lines = b.decode("utf-8", "replace").splitlines() if len(b) < 5_000_000 else []
    info.update(kind="text", lines=len(lines), head=txt[:400].replace("\n", " / "))
    if lines:
        nums = [ln.replace(",", " ").split() for ln in lines if ln and ln[0] not in "#"]
        widths = sorted({len(n) for n in nums})
        info["row_widths"] = widths[:6]
    return info


def _round2(now: dt.datetime) -> list[tuple[str, str, str, bool]]:
    """Round 2 (owner's grid URLs, 2026-11-28): 단기 PCP / 초단기 RN1 grids as numbers, an older issuance (archive),
    and the grid-number <-> lon/lat helper at the grid corners and at the documented origin."""
    d = now.replace(minute=0)
    while d.hour not in SHRT_ISSUE_H or now - d < dt.timedelta(minutes=40):
        d -= dt.timedelta(hours=1)
    g = HUB + "typ01/cgi-bin/url/"
    out = []
    for f, tag in ((d, "latest"), (d - dt.timedelta(hours=24), "24 h older")):
        for h in (1, 3):
            out.append(("F shrt grid", f"PCP {tag} +{h}h",
                        g + f"nph-dfs_shrt_grd?tmfc={f:%Y%m%d%H}&tmef={f + dt.timedelta(hours=h):%Y%m%d%H}&vars=PCP", True))
    v = now.replace(minute=0) - dt.timedelta(hours=1)
    for m in (0, 10, 30, 40):
        out.append(("G vsrt grid", f"RN1 tmfc HH{m:02d} +1h",
                    g + f"nph-dfs_vsrt_grd?tmfc={v:%Y%m%d%H}{m:02d}&tmef={v + dt.timedelta(hours=1):%Y%m%d%H}&vars=RN1",
                    True))
    out.append(("G vsrt grid", "RN1 24 h older", g + f"nph-dfs_vsrt_grd?tmfc={v - dt.timedelta(hours=24):%Y%m%d%H}10"
                f"&tmef={v - dt.timedelta(hours=23):%Y%m%d%H}&vars=RN1", True))
    for x, y in ((1, 1), (149, 253), (43, 136)):
        out.append(("H grid lonlat", f"x={x} y={y}", g + f"nph-dfs_xy_lonlat?x={x}&y={y}&help=1", True))
    return out


def _requests(now: dt.datetime) -> list[tuple[str, str, str, bool]]:
    """(group, label, url, needs_key)."""
    if os.environ.get("M10_ROUND") == "2":
        return _round2(now)
    tm = now.replace(minute=(now.minute // 10) * 10) - dt.timedelta(minutes=10)
    tm_h = now.replace(minute=0) - dt.timedelta(hours=1)
    d = now.replace(minute=0)
    while d.hour not in SHRT_ISSUE_H or now - d < dt.timedelta(minutes=40):
        d -= dt.timedelta(hours=1)
    fc, ef = d, d + dt.timedelta(hours=4)
    vfc = now.replace(minute=0) - dt.timedelta(hours=1)
    out = []
    cmp1 = (HUB + "typ03/cgi/rdr/nph-rdr_cmp1_api?tm={tm}&cmp=HSP&qcd=MSK&obs=ECHO&color=C4&aws=01&acc={acc}"
            "&map=HR&grid=2&legend=1&size=1000&itv=5&zoom_level=0&zoom_x=0000000&zoom_y=0000000&gov=")
    for acc in (0, 60, 180):
        out.append(("A hub radar image", f"nph-rdr_cmp1_api acc={acc}", cmp1.format(tm=_t(tm), acc=acc), True))
    out.append(("A hub radar image", "acc=60 on the hour", cmp1.format(tm=_t(tm_h), acc=60), True))
    for typ in ("typ04", "typ01"):
        for cmp in ("hsp", "hsr"):
            out.append(("B hub radar file", f"{typ} rdr_cmp_file {cmp}",
                        HUB + f"{typ}/url/rdr_cmp_file.php?tm={_t(tm)}&data=bin&cmp={cmp}", True))
    out.append(("C hub grid", "단기 PCP (nph-dfs_shrt_grd)",
                HUB + f"typ01/cgi-bin/url/nph-dfs_shrt_grd?tmfc={fc:%Y%m%d%H}&tmef={ef:%Y%m%d%H}&vars=PCP", True))
    for m in ("00", "30"):
        out.append(("C hub grid", f"초단기 RN1 tmfc HH{m} (nph-dfs_vsrt_grd)",
                    HUB + f"typ01/cgi-bin/url/nph-dfs_vsrt_grd?tmfc={vfc:%Y%m%d%H}{m}"
                          f"&tmef={vfc + dt.timedelta(hours=2):%Y%m%d%H}00&vars=RN1", True))
    for acc in (60, 180):
        out.append(("D radar center CGI", f"Korea box ACC={acc}", RADAR_CGI.format(acc=acc, tm=_t(tm)), False))
    for i, u in enumerate((os.environ.get("M10_URLS") or "").split()):
        out.append(("E owner URL", f"#{i + 1}", u, "apihub.kma.go.kr" in u))
    return out


@guarded("m10", "M10 radar / forecast-grid sources")
def main():
    key = os.environ.get("KMA_APIHUB_KEY", "")
    if key:
        print(f"::add-mask::{key}")
    out_dir = IMAGES / "m10"
    out_dir.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(KST).replace(second=0, microsecond=0)
    reqs = _requests(now)
    rows, recs = [], []
    for i, (group, label, url, needs_key) in enumerate(reqs):
        rec = {"group": group, "label": label, "url": url}
        t0 = time.monotonic()
        try:
            full = url + (("&" if "?" in url else "?") + f"authKey={key}" if needs_key else "")
            r = requests.get(full, timeout=TIMEOUT)
            b = r.content
            rec.update(status=r.status_code, ctype=r.headers.get("Content-Type"), bytes=len(b),
                       seconds=round(time.monotonic() - t0, 2), sha=hashlib.sha1(b).hexdigest()[:10], **_inspect(b))
            if rec.get("kind") == "text" and b and r.status_code == 200:      # grids: kept whole for a look
                (out_dir / f"{i:02d}_{''.join(c if c.isalnum() else '_' for c in label)}.txt").write_bytes(b)
            if rec.get("kind") not in (None, "text") and b:
                ext = {"PNG": "png", "GIF": "gif", "JPEG": "jpg"}.get(rec["kind"], "bin")
                (out_dir / f"{i:02d}_{''.join(c if c.isalnum() else '_' for c in label)}.{ext}").write_bytes(b)
        except Exception as e:  # noqa: BLE001
            rec.update(error=repr(e).replace(key, "***") if key else repr(e), seconds=round(time.monotonic() - t0, 2))
        recs.append(rec)
        size = f"{rec.get('w')}x{rec.get('h')}" if rec.get("w") else (f"{rec.get('unpacked')} unpacked"
                                                                       if rec.get("unpacked") else "")
        rows.append([i, group, label, rec.get("status"), rec.get("kind") or rec.get("ctype"), rec.get("bytes"), size,
                     rec.get("seconds"), str(rec.get("row_widths") or ""),
                     (rec.get("head") or rec.get("error") or "")[:100].replace("|", "/")])
        time.sleep(0.5)
    md = ["## M10 radar / forecast-grid sources ('강수 비교') - measurement only", "",
          f"Now {now:%Y-%m-%d %H:%M} KST. API Hub calls: {sum(1 for r in reqs if r[3])}.", "",
          md_table(["#", "group", "case", "HTTP", "kind", "bytes", "size", "s", "row widths", "text / head / error"],
                   rows)]
    save("m10", {"now_kst": now.isoformat(), "requests": recs}, "\n".join(md))


if __name__ == "__main__":
    main()

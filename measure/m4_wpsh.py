"""M4: west-Pacific subtropical high centre rule on consecutive runs (FH 0 and +120)."""

from __future__ import annotations

import datetime as dt
import math

import matplotlib.patches as mpatches

from measure.common import CFG, DATA, IMAGES, best_source, guarded, md_table, now_utc, save
from render import derive, fetch, grib, plot

N_RUNS = 16          # 16 consecutive runs -> 8 consecutive 00/12Z runs
FHS = [0, 120]
GH500 = [{"levtype": "pl", "levelist": [500], "param": ["gh"]}]


def get_gh(run, step, clients):
    for src, client in clients:
        try:
            r = fetch.fetch_step(client, run, step, DATA / "m4", CFG, groups=GH500, tag="_gh500")
            return r["path"], src
        except Exception as e:  # noqa: BLE001
            print(f"{run:%Y%m%d%H}+{step} via {src}: {e!r}"[:300])
    return None, None


def diag_image(F, res, run, step, path):
    w = CFG["wpsh"]
    box = w["search_box"]
    fr = plot.Frame(CFG, "wide")
    d = fr.dom
    S = F.crop(d["lat_min"] - 1, d["lat_max"] + 1, d["lon_min"] - 1, d["lon_max"] + 1)
    gh = S[("gh", 500)]
    cs = fr.ax.contour(S.lon, S.lat, gh, levels=plot._levels(gh, 60), colors="k", linewidths=0.8,
                       transform=plot.PC, zorder=4)
    fr.ax.clabel(cs, fmt="%d", fontsize=7)
    fr.ax.add_patch(mpatches.Rectangle((box["lon_min"], box["lat_min"]), box["lon_max"] - box["lon_min"],
                                       box["lat_max"] - box["lat_min"], fill=False, ls="--", ec="#0066cc",
                                       lw=1.0, transform=plot.PC, zorder=5))
    e = w["edge_exclude_deg"]
    fr.ax.add_patch(mpatches.Rectangle((box["lon_min"] + e, box["lat_min"] + e),
                                       box["lon_max"] - box["lon_min"] - 2 * e, box["lat_max"] - box["lat_min"] - 2 * e,
                                       fill=False, ls=":", ec="#0066cc", lw=0.8, transform=plot.PC, zorder=5))
    for la, lo, v in res["candidates"]:
        fr.ax.plot(lo, la, "+", color="#0066cc", ms=9, mew=1.5, transform=plot.PC, zorder=6)
        fr.ax.text(lo + 0.4, la + 0.4, f"{v:.0f}", color="#0066cc", fontsize=8, transform=plot.PC, zorder=6)
    if res["line_gpm"] is not None:
        fr.ax.contour(S.lon, S.lat, gh, levels=[res["line_gpm"]], colors=w["line_color"], linewidths=w["line_width"],
                      transform=plot.PC, zorder=7)
        la, lo = res["centre"]
        fr.ax.plot(lo, la, "*", color=w["line_color"], ms=14, mec="k", transform=plot.PC, zorder=8)
        txt = f"H={res['H']:.0f} at {la:.2f}N {lo:.2f}E, line={res['line_gpm']:.0f} gpm"
    else:
        txt = "no centre"
    fr.ax.text(0.01, 0.985, f"WPSH diagnostic: {txt}  (+ candidates, dashed = search box, dotted = edge exclusion)",
               transform=fr.ax.transAxes, fontsize=8, va="top",
               bbox=dict(fc="white", ec="none", alpha=0.85), zorder=9)
    fr.header("500hPa Height (gpm) - WPSH centre rule (diagnostic)", run, step)
    path.write_bytes(fr.png_bytes())


def overlay_composite(F, run, step, path):
    """Production-style overlay (default domain) composited on a gh-only base, to check alignment."""
    import io

    from PIL import Image
    base = plot.Frame(CFG, "default")
    d = base.dom
    S = F.crop(d["lat_min"] - 1, d["lat_max"] + 1, d["lon_min"] - 1, d["lon_max"] + 1)
    gh = S[("gh", 500)]
    cs = base.ax.contour(S.lon, S.lat, gh, levels=plot._levels(gh, 60), colors="k", linewidths=1.0,
                         transform=plot.PC, zorder=4)
    base.ax.clabel(cs, fmt="%d", fontsize=8)
    base.header("500hPa Height (gpm) + WPSH overlay", run, step)
    a = Image.open(io.BytesIO(base.png_bytes())).convert("RGBA")
    ov, _ = plot.overlay_wpsh(CFG, F, run, step)
    ov_png = ov.png_bytes()
    (path.parent / path.name.replace("composite", "overlay")).write_bytes(ov_png)
    b = Image.open(io.BytesIO(ov_png)).convert("RGBA")
    Image.alpha_composite(a, b).save(path)


@guarded("m4", "M4 WPSH")
def main():
    clients = []
    for src in best_source():
        try:
            clients.append((src, fetch.make_client(src, CFG, retries=1, retry_after=5)))
        except Exception as e:  # noqa: BLE001
            print(f"client {src}: {e!r}")
    out = IMAGES / "m4"
    out.mkdir(parents=True, exist_ok=True)
    now = now_utc()
    run = now.replace(minute=0, second=0, microsecond=0, hour=(now.hour // 6) * 6)
    # newest run whose +120 h exists
    for _ in range(4):
        p, _src = get_gh(run, 120, clients)
        if p:
            break
        run -= dt.timedelta(hours=6)
    runs = [run - dt.timedelta(hours=6 * i) for i in range(N_RUNS)][::-1]
    records = []
    w = CFG["wpsh"]
    box = w["search_box"]
    mg = w["local_window_deg"] + 1
    for r in runs:
        for fh in FHS:
            p, src = get_gh(r, fh, clients)
            rec = {"run": f"{r:%Y%m%d%H}", "fh": fh, "valid": f"{r + dt.timedelta(hours=fh):%Y%m%d%H}", "source": src}
            if not p:
                rec["error"] = "download failed"
                records.append(rec)
                continue
            F = grib.read(p, CFG["data"]["read_region"])
            B = F.crop(box["lat_min"] - mg, box["lat_max"] + mg, box["lon_min"] - mg, box["lon_max"] + mg)
            res = derive.wpsh_centre(B[("gh", 500)], B.lat, B.lon, CFG, CFG["data"]["grid_deg"])
            rec.update({"H": res["H"], "centre": res["centre"], "line_gpm": res["line_gpm"],
                        "candidates": [[round(a, 2), round(b, 2), round(c, 1)] for a, b, c in res["candidates"]]})
            diag_image(F, res, r, fh, out / f"wpsh_diag_{r:%Y%m%d%H}_{fh:03d}.png")
            overlay_composite(F, r, fh, out / f"wpsh_composite_{r:%Y%m%d%H}_{fh:03d}.png")
            records.append(rec)

    # H changes between consecutive *drawn* (00/12Z) runs at the same forecast hour.
    # A large centre move with a similar H is accepted (the line level barely changes): reported, not flagged.
    rows = []
    n_jump = 0
    drawn_hours = w.get("run_hours", [0, 6, 12, 18])
    for fh in FHS:
        prev = None
        for rec in [x for x in records if x["fh"] == fh]:
            flag = ""
            drawn = int(rec["run"][-2:]) in drawn_hours
            rec["drawn"] = drawn
            if not drawn:
                flag = "no line (06/18Z)"
            elif prev and rec.get("centre") and prev.get("centre"):
                (a1, o1), (a2, o2) = prev["centre"], rec["centre"]
                dist = math.hypot(a2 - a1, (o2 - o1) * math.cos(math.radians((a1 + a2) / 2)))
                dh = rec["H"] - prev["H"]
                rec["move_deg"], rec["dH"] = round(dist, 1), round(dh, 1)
                if abs(dh) > w["jump_flag_gpm"]:
                    flag = "H JUMP"
                    n_jump += 1
                elif dist > w["jump_flag_deg"]:
                    flag = "moved, H similar (ok)"
            elif prev and (bool(rec.get("centre")) != bool(prev.get("centre"))):
                flag = "APPEAR/VANISH"
                n_jump += 1
            if drawn:
                prev = rec
            rec["flag"] = flag
            c = rec.get("centre")
            rows.append([rec["run"], f"+{fh}", rec["valid"],
                         f"{rec['H']:.0f}" if rec.get("H") else "no centre",
                         f"{c[0]:.2f}N {c[1]:.2f}E" if c else "-",
                         f"{rec['line_gpm']:.0f}" if rec.get("line_gpm") else "-",
                         len(rec.get("candidates", [])),
                         "; ".join(f"{v:.0f}@{a:.1f}N{o:.1f}E" for a, o, v in rec.get("candidates", [])[:5]),
                         rec.get("move_deg", ""), rec.get("dH", ""), flag or rec.get("error", "")])
    ok = sum(1 for r in records if "error" not in r)
    status = "PASS" if ok >= 16 and n_jump == 0 else "FLAGGED" if ok >= 16 else ("UNKNOWN" if ok == 0 else "PARTIAL")
    res = {"status": status, "runs": [f"{r:%Y%m%d%H}" for r in runs], "records": records, "jumps": n_jump}
    md = [f"## M4 WPSH centre rule — {status} ({ok}/{len(records)} frames, {n_jump} flags)\n",
          f"Rule: search box {box}, local max within +/-{w['local_window_deg']} deg, "
          f"edge exclusion {w['edge_exclude_deg']} deg, line = H - {w['level_offset_gpm']} gpm. "
          f"Line drawn only for run hours {drawn_hours}. Flag (H JUMP) only if |dH| > {w['jump_flag_gpm']} gpm "
          f"between consecutive drawn runs at the same forecast hour; centre moves > {w['jump_flag_deg']} deg with "
          "similar H are reported but accepted.\n",
          md_table(["run", "FH", "valid", "H gpm", "centre", "line gpm", "#cand", "top candidates", "move deg", "dH",
                    "flag"], rows),
          "\nImages: artifact `stage0-images` -> `m4/wpsh_diag_*.png` (wide-domain diagnostic) and "
          "`m4/wpsh_composite_*.png` / `m4/wpsh_overlay_*.png` (production overlay).\n"]
    save("m4", res, "\n".join(md))


if __name__ == "__main__":
    main()

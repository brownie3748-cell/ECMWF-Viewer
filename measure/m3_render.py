"""M3: render time and image size per chart; projections for full runs and 10 retained runs."""

from __future__ import annotations

import statistics
import time

from measure.common import CFG, DATA, IMAGES, best_source, guarded, latest_main_run, load, md_table, save
from render import encode, fetch, grib, plot

STEPS = [120, 240]
GATE_KB = 190
SITE_LIMIT_GB = 1.0
N_MAIN, N_SHORT = 41, 25          # published steps per 00/12Z and 06/18Z run
N_TP_EXTRA = 24                   # 3-hourly tp-only steps (3, 9, ..., 141) per run


def jobs(cfg, F, P, run, step, window):
    gz = cfg["charts"]["gz"]["temp_style"]
    j = {
        "msl": lambda: plot.chart_msl(cfg, F, run, step),
        "tp": lambda: plot.chart_tp(cfg, F, P, run, step, window)[0],
        "rh700": lambda: plot.chart_rh700(cfg, F, run, step),
        "mfc925": lambda: plot.chart_mfc(cfg, F, 925, run, step)[0],
        "mfc850": lambda: plot.chart_mfc(cfg, F, 850, run, step)[0],
        "wpsh500": lambda: plot.overlay_wpsh(cfg, F, run, step)[0],
        "gz500_wide": lambda: plot.chart_gz(cfg, F, 500, run, step, gz, "wide"),
        "wpsh500_wide": lambda: plot.overlay_wpsh(cfg, F, run, step, "wide")[0],
    }
    for lv in (925, 850, 700, 500, 300, 200):
        j[f"gz{lv}"] = (lambda lv=lv: plot.chart_gz(cfg, F, lv, run, step, gz))
    extra = {f"gz500_{v}": (lambda v=v: plot.chart_gz(cfg, F, 500, run, step, v))
             for v in cfg["charts"]["gz"]["gz500_variants"]}
    return j, extra


@guarded("m3", "M3 render")
def main():
    run = latest_main_run()
    out = IMAGES / "m3"
    out.mkdir(parents=True, exist_ok=True)
    per_chart: dict[str, list] = {}
    extra_info: dict[str, list] = {}
    read_s, tp_fetch = [], []
    client = None
    for src in best_source():
        try:
            client = fetch.make_client(src, CFG, retries=2, retry_after=5)
            break
        except Exception:  # noqa: BLE001
            continue
    for step in STEPS:
        window = fetch.tp_window(step, CFG)
        r = client and fetch.fetch_step(client, run, step - window, DATA / "m3tp", CFG, tp_only=True)
        tp_fetch.append({"step": step - window, "seconds": round(r["seconds"], 2), "mb": round(r["bytes"] / 1e6, 2)})
        t0 = time.monotonic()
        F = grib.read(str(DATA / f"{run:%Y%m%d%H}_{step:03d}.grib2"), CFG["data"]["read_region"])
        P = grib.read(r["path"], CFG["data"]["read_region"])
        read_s.append(time.monotonic() - t0)
        main_jobs, extra = jobs(CFG, F, P, run, step, window)
        for group, js in (("main", main_jobs), ("extra", extra)):
            for name, fn in js.items():
                t0 = time.monotonic()
                png = fn().png_bytes()
                t_render = time.monotonic() - t0
                v, enc_t = encode.variants(png, CFG["output"]["png8_colors"], CFG["output"]["webp_quality"])
                for fmt, b in v.items():
                    (out / f"{name}_{run:%Y%m%d%H}_{step:03d}.{fmt}.{encode.EXT[fmt]}").write_bytes(b)
                rec = {"step": step, "render_s": round(t_render, 3),
                       **{f"{k}_enc_s": round(enc_t[k], 3) for k in v},
                       **{f"{k}_kb": round(len(b) / 1024, 1) for k, b in v.items()}}
                (per_chart if group == "main" else extra_info).setdefault(name, []).append(rec)

    fmts = encode.FORMATS
    rows = []
    for name, recs in list(per_chart.items()) + list(extra_info.items()):
        rows.append([name, f"{statistics.mean(r['render_s'] for r in recs):.2f}",
                     f"{statistics.mean(r['png8_enc_s'] for r in recs):.2f}",
                     f"{statistics.mean(r['webp_q_enc_s'] for r in recs):.2f}",
                     *[f"{statistics.mean(r[f + '_kb'] for r in recs):.0f}" for f in fmts]])
    n_img = len(per_chart)
    step_render = statistics.mean(sum(r[i]["render_s"] for r in per_chart.values()) for i in range(len(STEPS)))
    step_kb = {f: statistics.mean(sum(r[i][f + "_kb"] for r in per_chart.values()) for i in range(len(STEPS)))
               for f in fmts}
    m1 = load("m1") or {}
    dl = None
    if m1.get("ranking"):
        d = [x for x in m1["sources"][m1["ranking"][0]]["downloads"] if "seconds" in x]
        dl = statistics.mean(x["seconds"] for x in d)
    tp_s = statistics.mean(x["seconds"] for x in tp_fetch)
    read_mean = statistics.mean(read_s)
    proj_rows = []
    proj = {}
    for f in fmts:
        avg = step_kb[f] / n_img
        main_mb = step_kb[f] * N_MAIN / 1024
        short_mb = step_kb[f] * N_SHORT / 1024
        ten = (5 * main_mb + 5 * short_mb) / 1024
        eight = (4 * main_mb + 4 * short_mb) / 1024
        proj[f] = {"avg_kb": avg, "main_run_mb": main_mb, "short_run_mb": short_mb, "ten_runs_gb": ten,
                   "eight_runs_gb": eight, "gate_pass": avg <= GATE_KB}
        proj_rows.append([f, f"{avg:.0f}", f"{main_mb:.0f}", f"{short_mb:.0f}", f"{ten:.2f}", f"{eight:.2f}",
                          "PASS" if avg <= GATE_KB else "FAIL"])

    def run_time(nsteps, n_tp_extra, encode_s):
        render = nsteps * (step_render + encode_s + read_mean)
        download = nsteps * (dl or 0) + n_tp_extra * tp_s
        return render, download

    chosen = CFG["output"]["format"]
    enc_s = statistics.mean(sum(r[i][f"{chosen}_enc_s"] for r in per_chart.values()) for i in range(len(STEPS)))
    t_main = run_time(N_MAIN, N_TP_EXTRA, enc_s)
    t_short = run_time(N_SHORT, N_TP_EXTRA, enc_s)
    status = "PASS" if proj[chosen]["gate_pass"] else "FAIL"
    res = {"status": status, "run": f"{run:%Y%m%d%H}", "steps": STEPS, "per_chart": per_chart, "extra": extra_info,
           "read_s": read_s, "tp_fetch": tp_fetch, "step_render_s": step_render, "step_kb": step_kb,
           "projection": proj, "download_step_s": dl,
           "run_time_s": {"main": {"render": t_main[0], "download": t_main[1]},
                          "short": {"render": t_short[0], "download": t_short[1]}}}
    md = [f"## M3 render — chosen format `{chosen}`: **{status}** vs {GATE_KB} KB gate\n",
          f"Run `{run:%Y%m%d%H}`, steps {STEPS} (measured on this runner, single process). "
          f"Images per published step: {n_img}.\n",
          md_table(["chart", "render s (measured)", "PNG8 encode s", "WebP-q encode s", "PNG KB", "PNG8 KB",
                    "WebP lossless KB", f"WebP q{CFG['output']['webp_quality']} KB"], rows),
          f"\nRead GRIB+crop: {read_mean:.2f} s/step. Render of all {n_img} images: {step_render:.1f} s/step (measured). "
          f"tp-only fetch: {tp_s:.1f} s. 30-field step download (M1 fastest): {dl and round(dl, 1)} s.\n",
          "\n**Projection (estimated from the measured per-step numbers):**\n",
          md_table(["format", "avg KB/image", "00/12Z run MB (574 img)", "06/18Z run MB (350 img)",
                    "10 runs GB", "8 runs GB", f"<= {GATE_KB} KB gate"], proj_rows),
          f"\nWall time per run, sequential (estimated): 00/12Z render {t_main[0] / 60:.1f} min + download "
          f"{t_main[1] / 60:.1f} min; 06/18Z render {t_short[0] / 60:.1f} min + download {t_short[1] / 60:.1f} min. "
          f"With 4 parallel render processes on a 4-vCPU runner divide render by ~3-4 (estimated).\n"]
    save("m3", res, "\n".join(md))


if __name__ == "__main__":
    main()

"""M5: moisture-flux convergence sanity check (value range, sign, scale, smoothing)."""

from __future__ import annotations

import copy

import numpy as np

from measure.common import CFG, DATA, IMAGES, guarded, latest_main_run, md_table, save
from render import derive, grib, plot

STEP = 120
SIGMAS = [0, 1, 2, 4]


def variant_cfg(scale, sigma, label):
    c = copy.deepcopy(CFG)
    c["charts"]["mfc"]["scale"] = scale
    c["charts"]["mfc"]["smooth_sigma"] = sigma
    c["charts"]["mfc"]["title"] = "{level}hPa Moisture Flux(m/s) and Convergence(" + label + ")"
    return c


@guarded("m5", "M5 MFC")
def main():
    run = latest_main_run()
    F = grib.read(str(DATA / f"{run:%Y%m%d%H}_{STEP:03d}.grib2"), CFG["data"]["read_region"])
    d = CFG["domains"]["default"]
    S = F.crop(d["lat_min"], d["lat_max"], d["lon_min"], d["lon_max"])
    # tp of the preceding 3 h, for a physical sanity check (convergence should co-locate with rain)
    tp_path = DATA / "m3tp" / f"{run:%Y%m%d%H}_{STEP - 3:03d}_tp.grib2"
    acc = None
    if tp_path.exists():
        P = grib.read(str(tp_path), CFG["data"]["read_region"]).crop(d["lat_min"], d["lat_max"], d["lon_min"],
                                                                      d["lon_max"])
        acc = derive.tp_accum_mm(S[("tp", 0)], P[("tp", 0)])
    rows, stats = [], {}
    for lev in (850, 925):
        for sig in SIGMAS:
            qu, qv, div = derive.moisture_flux(S[("q", lev)], S[("u", lev)], S[("v", lev)], S.lat, S.lon, lev,
                                               S[("sp", 0)], sig)
            v = div[np.isfinite(div)] * 1e9
            pct = np.percentile(v, [1, 5, 50, 95, 99])
            corr = None
            if acc is not None:
                m = np.isfinite(div)
                corr = float(np.corrcoef(-div[m], acc[m])[0, 1])
            mean_rain_conv = float(acc[np.isfinite(div) & (div < 0)].mean()) if acc is not None else None
            mean_rain_div = float(acc[np.isfinite(div) & (div > 0)].mean()) if acc is not None else None
            stats[f"{lev}_s{sig}"] = {"pct_1_5_50_95_99_e9": pct.tolist(), "frac_below_-50e-9": float((v < -50).mean()),
                                     "frac_below_-50e-8": float((v < -500).mean()), "corr_conv_rain": corr,
                                     "rain_mm_conv": mean_rain_conv, "rain_mm_div": mean_rain_div,
                                     "masked_frac": float((~np.isfinite(div)).mean()),
                                     "qV_p99": float(np.nanpercentile(np.hypot(qu, qv), 99))}
            rows.append([lev, sig, *[f"{x:.0f}" for x in pct], f"{(v < -50).mean() * 100:.0f}%",
                         f"{(v < -500).mean() * 100:.1f}%", f"{corr:.2f}" if corr is not None else "-",
                         f"{mean_rain_conv:.2f} / {mean_rain_div:.2f}" if acc is not None else "-",
                         f"{(~np.isfinite(div)).mean() * 100:.0f}%"])
    out = IMAGES / "m5"
    out.mkdir(parents=True, exist_ok=True)
    variants = {
        "brief_E-9_sigma1": variant_cfg(1e9, 1.0, "E-9/s"),
        "E-9_sigma4": variant_cfg(1e9, 4.0, "E-9/s"),
        "E-8_sigma1": variant_cfg(1e8, 1.0, "E-8/s"),
        "E-8_sigma2": variant_cfg(1e8, 2.0, "E-8/s"),
    }
    for name, c in variants.items():
        for lev in (850, 925):
            fr, _ = plot.chart_mfc(c, F, lev, run, STEP)
            (out / f"mfc{lev}_{name}_{run:%Y%m%d%H}_{STEP:03d}.png").write_bytes(fr.png_bytes())
    # brief-scale saturation decides the verdict
    sat = stats["850_s1"]["frac_below_-50e-9"]
    sign_ok = all(s["corr_conv_rain"] is None or s["corr_conv_rain"] > 0 for s in stats.values())
    status = "PASS" if sat < 0.05 and sign_ok else "FAIL"
    res = {"status": status, "run": f"{run:%Y%m%d%H}", "step": STEP, "stats": stats}
    md = [f"## M5 MFC sanity — **{status}** (brief scale E-9/s, sigma=1)\n",
          f"Run `{run:%Y%m%d%H}` +{STEP} h, default domain, q in kg/kg, V in m/s, div(qV) on the sphere; "
          "values below are div(qV) x 1e9 (s^-1). Negative = convergence. sigma = gaussian smoothing in grid "
          "points (0.25 deg). Masked = level below ground (p > sp).\n",
          md_table(["level", "sigma", "p1", "p5", "p50", "p95", "p99", "% < -50 (E-9 bar saturates)",
                    "% < -500 (E-8 bar saturates)", "corr(conv, 3h rain)", "mean rain mm conv/div", "masked"],
                   rows),
          "\nSign check: positive corr(conv, rain) and more rain over convergence than divergence means the sign "
          "convention (shade negative div) is physically consistent.\n",
          f"Images: `m5/mfc*_{{variant}}_*.png` with variants {list(variants)}.\n"]
    save("m5", res, "\n".join(md))


if __name__ == "__main__":
    main()

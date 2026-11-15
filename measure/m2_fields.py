"""M2: field inventory (30 fields), units from GRIB metadata, tp accumulation basis."""

from __future__ import annotations

import numpy as np

from measure.common import CFG, DATA, best_source, guarded, latest_main_run, md_table, save
from render import derive, fetch, grib

TP_STEPS = [0, 3, 6, 9, 12, 117, 120, 138, 141, 144, 150, 156, 234, 240]


def expected_fields():
    exp = []
    for g in CFG["data"]["groups"]:
        if g["levtype"] == "sfc":
            exp += [(p, 0) for p in g["param"]]
        else:
            exp += [(p, lv) for lv in g["levelist"] for p in g["param"]]
    return exp


def fetch_tp_series(run):
    """tp-only files for TP_STEPS from the fastest working source."""
    out = {}
    for src in best_source():
        try:
            client = fetch.make_client(src, CFG, retries=2, retry_after=5)
            for st in TP_STEPS:
                if st not in out:
                    out[st] = fetch.fetch_step(client, run, st, DATA / "tp", CFG, tp_only=True)["path"]
            return out, src
        except Exception as e:  # noqa: BLE001
            print(f"tp fetch via {src} failed: {e!r}")
    return out, None


@guarded("m2", "M2 fields")
def main():
    run = latest_main_run()
    path = DATA / f"{run:%Y%m%d%H}_120.grib2"
    F = grib.read(str(path))
    exp = expected_fields()
    rows, missing = [], []
    for key in exp:
        if key not in F.meta:
            missing.append(key)
            continue
        m = F.meta[key]
        a = F.data[key]
        rows.append([m["shortName"], m["typeOfLevel"], m["level"], m["units"], m["name"], m["stepRange"],
                     m["stepType"], f"{np.nanmin(a):.4g}", f"{np.nanmax(a):.4g}"])
    extra = [k for k in F.meta if k not in exp]

    # ---- tp accumulation basis ----
    files, src = fetch_tp_series(run)
    reg = dict(CFG["domains"]["default"], **__import__("render.plot", fromlist=["x"]).domain_geometry(CFG["domains"]["default"])["bbox"])
    tp = {}
    tp_meta = {}
    for st, p in sorted(files.items()):
        G = grib.read(p, CFG["data"]["read_region"])
        tp[st] = G.crop(reg["lat_min"], reg["lat_max"], reg["lon_min"], reg["lon_max"])[("tp", 0)]
        tp_meta[st] = {k: G.meta[("tp", 0)][k] for k in ("stepRange", "stepType", "units")}
        u = np.unique(G[("tp", 0)])
        tp_meta[st]["packing_quantum_mm"] = float(np.diff(u).min() * 1000) if u.size > 1 else None
        tp_meta[st]["global_max_mm"] = float(u.max() * 1000)
    pairs = [(3, 0), (6, 3), (9, 6), (12, 9), (120, 117), (141, 138), (144, 141), (150, 144),
             (156, 150), (240, 234)]
    tp_rows = []
    ok_nonneg = True
    for b, a in pairs:
        if a in tp and b in tp:
            raw = (tp[b] - tp[a]) * 1000.0
            acc = derive.tp_accum_mm(tp[b], tp[a])
            neg = float(raw.min())
            quantum = max(tp_meta[a]["packing_quantum_mm"] or 0, tp_meta[b]["packing_quantum_mm"] or 0)
            if neg < -1.01 * quantum:   # anything beyond one packing step is a real decrease
                ok_nonneg = False
            tp_rows.append([f"{a}->{b}", b - a, f"{neg:.4f}", f"{quantum:.4f}", int((raw < 0).sum()), f"{acc.max():.1f}",
                            f"{acc.mean():.3f}", f"{(acc >= 0.1).mean() * 100:.1f}"])
    step0_max = float(np.abs(tp[0]).max() * 1000) if 0 in tp else None
    basis = sorted({v["stepRange"] for v in tp_meta.values()})
    cumulative = all(v["stepRange"] in ("0", f"0-{st}") for st, v in tp_meta.items())
    status = "PASS" if not missing and ok_nonneg and cumulative else "FAIL"
    res = {"status": status, "run": f"{run:%Y%m%d%H}", "step": 120, "missing": missing, "extra": extra,
           "fields": rows, "tp_source": src, "tp_meta": tp_meta, "tp_step0_max_mm": step0_max,
           "tp_cumulative_from_0": cumulative, "tp_diffs": tp_rows}
    md = [f"## M2 fields — **{status}**\n",
          f"Run `{run:%Y%m%d%H}` step +120 h. Fields found: {len(rows)}/{len(exp)}. Missing: {missing or 'none'}. "
          f"Extra: {extra or 'none'}.\n",
          md_table(["shortName", "typeOfLevel", "level", "units (GRIB)", "name", "stepRange", "stepType",
                    "min (global)", "max (global)"], rows),
          f"\n**tp basis (measured):** units = {sorted({v['units'] for v in tp_meta.values()})}, "
          f"stepRanges = {basis}, cumulative from step 0 = {cumulative}, tp(0) max = {step0_max} mm.\n",
          "Differences over the default domain (mm). Negative differences no larger than one GRIB packing "
          "quantum (12-bit CCSDS packing of the cumulative field) are packing noise and are clipped to 0.\n",
          md_table(["steps", "window h", "min raw diff", "GRIB packing quantum", "# negative pts", "max acc", "mean acc", "% pts >= 0.1 mm"],
                   tp_rows)]
    save("m2", res, "\n".join(md))


if __name__ == "__main__":
    main()

"""Offline tests for steps, model configs, the storage guard, poll planning and the manifest (no network)."""

import datetime as dt
import json
import pathlib
import tempfile
import unittest

import numpy as np

from render import config, derive, fetch, kim, site
from render import store as store_mod
from render.__main__ import _newer_complete, _poll_plan, recent_cycles

CFG = config.load()


def C(s):
    return dt.datetime.strptime(s, "%Y%m%d%H")


class Steps(unittest.TestCase):
    def test_published_steps(self):
        # IFS: every 3 h to 144 h, then every 6 h; AIFS: every 6 h
        self.assertEqual(len(fetch.published_steps(0, CFG)), 65)
        self.assertEqual(len(fetch.published_steps(6, CFG)), 49)
        self.assertEqual(fetch.published_steps(18, CFG)[-1], 144)
        self.assertEqual(fetch.published_steps(0, CFG)[47:51], [141, 144, 150, 156])
        aifs = config.for_model(CFG, "aifs")
        self.assertEqual(len(fetch.published_steps(0, aifs)), 41)
        self.assertEqual(len(fetch.published_steps(6, aifs)), 41)      # AIFS 06/18Z to +240 h as well

    def test_tp_window(self):
        self.assertEqual(fetch.tp_window(144, CFG), 3)
        self.assertEqual(fetch.tp_window(150, CFG), 6)
        aifs = config.for_model(CFG, "aifs")
        self.assertEqual(fetch.tp_window(6, aifs), 6)      # AIFS output is 6-hourly

    def test_recent_cycles(self):
        c = recent_cycles(3, dt.datetime(2026, 11, 14, 13, 40))
        self.assertEqual([x.hour for x in c], [12, 6, 0])


class Models(unittest.TestCase):
    def test_model_ids(self):
        self.assertEqual(config.model_ids(CFG), ["ifs", "ifs01", "aifs", "aifsens", "kimr"])

    def test_chart_lists(self):
        ifs = config.for_model(CFG, "ifs")
        self.assertEqual(len(ifs["site"]["charts"]), 18)          # + theta-e 925/850/700 (owner 2026-11-24)
        self.assertEqual(65 * 18 - 1, 1169)           # images per IFS 00Z run: no snow chart at +0h
        ids = [c["id"] for c in ifs["site"]["charts"]]
        self.assertEqual(ids[0], "msl")
        self.assertEqual(ids[-1], "gz200")
        hi = [c["id"] for c in config.for_model(CFG, "ifs01")["site"]["charts"]]
        for gone in ("gz300", "gz200", "gz500_wide", "wpsh500_wide"):
            self.assertNotIn(gone, hi)
        self.assertEqual(len(config.for_model(CFG, "aifs")["site"]["charts"]), 18)
        for mid in ("ifs", "ifs01", "aifs", "kimr"):
            ids = [c["id"] for c in config.for_model(CFG, mid)["site"]["charts"]]
            self.assertTrue({"ept925", "ept850", "ept700"} <= set(ids), mid)
        for mid in ("ifs", "ifs01", "aifs"):           # 2d and gh 1000 are in the ECMWF open data (measured 2026-11-22)
            self.assertIn("snow", [c["id"] for c in config.for_model(CFG, mid)["site"]["charts"]])
        kimr = config.for_model(CFG, "kimr")
        self.assertIn("snow", [c["id"] for c in kimr["site"]["charts"]])          # owner 2026-11-23: RH2 + SNOW
        self.assertNotIn(1000, [lv for g in kimr["data"]["groups"] for lv in g.get("levelist", [])])  # no thickness

    def test_overrides(self):
        hi = config.for_model(CFG, "ifs01")
        self.assertEqual(hi["data"]["resolution"], "0p1")
        self.assertEqual(hi["domains"]["default"]["barb_every"], 20)
        self.assertEqual(hi["domains"]["default"]["extent_m"], CFG["domains"]["default"]["extent_m"])  # merged
        self.assertEqual(CFG["data"]["resolution"], "0p25")                                         # base intact
        ai = config.for_model(CFG, "aifs")
        self.assertEqual(ai["data"]["model"], "aifs-single")
        self.assertNotIn("r", [p for g in ai["data"]["groups"] for p in g["param"]])

    def test_overlays_point_at_published_charts(self):
        for mid in config.model_ids(CFG):
            ids = {c["id"] for c in config.for_model(CFG, mid)["site"]["charts"]}
            for c in config.for_model(CFG, mid)["site"]["charts"]:
                if c.get("overlay_of"):
                    self.assertIn(c["overlay_of"], ids, mid)


class Humidity(unittest.TestCase):
    def test_rh_from_q(self):
        t = np.array([283.15], dtype=np.float32)          # 10 C, water saturation
        es = 611.21 * np.exp(17.502 * (t - 273.16) / (t - 32.19))
        p = 70000.0
        qs = 0.622 * es / (p - 0.378 * es)
        self.assertAlmostEqual(float(derive.relative_humidity(qs, t, 700)[0]), 100.0, places=2)
        self.assertAlmostEqual(float(derive.relative_humidity(qs / 2, t, 700)[0]), 50.0, delta=0.5)


def fake_stage(root: pathlib.Path, model: str, run: str, n: int, size: int = 100) -> pathlib.Path:
    d = root / model / run
    d.mkdir(parents=True)
    for i in range(n):
        (d / f"msl_{i:03d}.png").write_bytes(b"x" * size)
    (d / "meta.json").write_text(json.dumps({"run": run, "steps": [0], "wpsh": None, "render_version": 5}))
    return d


class Storage(unittest.TestCase):
    def cfg(self, **kv):
        c = config.load()
        c["storage"].update(kv)
        return c

    def test_upload_and_cap(self):
        with tempfile.TemporaryDirectory() as t:
            t = pathlib.Path(t)
            st = store_mod.LocalStore(self.cfg(max_bytes=1000), t / "bucket")
            sizes = st.list_sizes()
            d = fake_stage(t / "stage", "ifs", "2026111500", 5)          # ~5x100 B + meta
            st.upload_dir(d, "ifs/2026111500/", sizes, last="meta.json")
            self.assertTrue((t / "bucket/ifs/2026111500/meta.json").exists())
            d2 = fake_stage(t / "stage2", "ifs", "2026111506", 5)
            with self.assertRaises(store_mod.StorageCapError):
                st.upload_dir(d2, "ifs/2026111506/", sizes, last="meta.json")
            self.assertFalse((t / "bucket/ifs/2026111506").exists())       # nothing uploaded past the cap

    def test_reupload_replaces_and_drops_stale(self):
        with tempfile.TemporaryDirectory() as t:
            t = pathlib.Path(t)
            st = store_mod.LocalStore(self.cfg(), t / "bucket")
            sizes = st.list_sizes()
            st.upload_dir(fake_stage(t / "a", "ifs", "2026111500", 5), "ifs/2026111500/", sizes, last="meta.json")
            st.upload_dir(fake_stage(t / "b", "ifs", "2026111500", 3), "ifs/2026111500/", sizes, last="meta.json")
            self.assertEqual(sum(1 for k in sizes if k.startswith("ifs/2026111500/")), 4)
            self.assertEqual(st.list_sizes(), sizes)

    def test_request_budget(self):
        with tempfile.TemporaryDirectory() as t:
            t = pathlib.Path(t)
            st = store_mod.LocalStore(self.cfg(max_class_a_per_month=3), t / "bucket")
            sizes = {}
            with self.assertRaises(store_mod.StorageCapError):
                st.upload_dir(fake_stage(t / "a", "ifs", "2026111500", 5), "ifs/2026111500/", sizes, "meta.json")
            st.save_ops()
            st2 = store_mod.LocalStore(self.cfg(), t / "bucket")
            self.assertGreaterEqual(st2.month_ops()["class_a"], 1)        # counter persisted in _ops/


class Plan(unittest.TestCase):
    def test_plan_newest_first_all_models_and_prune(self):
        complete = {m: [C("2026111506"), C("2026111500")] for m in ("ifs", "ifs01", "aifs")}
        rv = CFG["site"]["render_version"]
        have = {"ifs": {"2026111500": {"run": "2026111500", "rv": rv},
                        "2026111418": {"run": "2026111418", "rv": rv}},
                "aifs": {"2026111500": {"run": "2026111500", "rv": rv - 1}},
                "old": {"2026111400": {"run": "2026111400", "rv": rv}}}
        todo, remove = _poll_plan(CFG, have, complete, rv)
        self.assertEqual(_poll_plan(CFG, have, complete, {"ifs": rv, "ifs01": rv, "aifs": rv - 1})[0][-1:],
                         [(C("2026111500"), "ifs01")])       # per-model version: aifs rv-1 is current
        self.assertEqual([(f"{c:%Y%m%d%H}", m) for c, m in todo],
                         [("2026111506", "ifs"), ("2026111506", "aifs"), ("2026111506", "ifs01"),
                          ("2026111500", "aifs"), ("2026111500", "ifs01")])
        self.assertEqual(remove, {"ifs": ["2026111418"], "old": ["2026111400"]})


class LateRuns(unittest.TestCase):
    def test_newer_cycle_found_once(self):
        class Src:
            def __init__(self, ok): self.ok = ok
            def run_complete(self, c): return c in self.ok
        newest = recent_cycles(1)[0]
        older = newest - dt.timedelta(hours=6)
        sources = {"ifs": (None, Src({newest, older})), "ifs01": (None, Src({older})), "aifs": (None, Src({newest, older}))}
        complete = {m: [older] for m in sources}
        found = _newer_complete(CFG, sources, complete, [])
        self.assertEqual(found, [(newest, "ifs"), (newest, "aifs")])
        self.assertEqual(_newer_complete(CFG, sources, complete, found), [])     # already queued


class Manifest(unittest.TestCase):
    def test_manifest_roundtrip(self):
        with tempfile.TemporaryDirectory() as t:
            st = store_mod.LocalStore(CFG, t)
            runs = {"ifs": {r: site.run_entry({"run": r, "steps": [0, 6], "wpsh": None, "render_version": 5,
                                                 "bytes": 9})
                            for r in ("2026111506", "2026111500")}}
            m = site.write_manifest(CFG, st, runs)
            self.assertEqual(m["schema_version"], 2)
            self.assertEqual([x["id"] for x in m["models"]], ["ifs", "ifs01", "aifs", "aifsens", "kimr"])   # kimz gone 2026-11-24
            ifs = m["models"][0]
            self.assertEqual([r["run"] for r in ifs["runs"]], ["2026111500", "2026111506"])   # oldest first
            self.assertEqual(ifs["latest"], "2026111506")
            self.assertNotIn("bytes", ifs["runs"][0])
            back = site.runs_from_manifest(st.get_json("index.json"))
            self.assertEqual(set(back["ifs"]), {"2026111500", "2026111506"})
            self.assertIn("{model}", m["path"])


if __name__ == "__main__":
    unittest.main()


class Snow(unittest.TestCase):
    def test_wet_bulb(self):
        import numpy as np
        from render import derive
        tw = derive.wet_bulb_c(np.array([20.0, 1.2, 5.0]), np.array([9.26, 1.2, -10.0]), np.array([1013.0, 1000, 1000]))
        self.assertAlmostEqual(tw[0], 13.8, delta=0.2)      # psychrometric table: 20 C, RH 50 % -> about 13.8 C
        self.assertAlmostEqual(tw[1], 1.2, places=4)        # saturated: Tw = T
        self.assertTrue(-10 < tw[2] < 5)

    def test_kma_snow_ratio(self):
        import numpy as np
        from render import derive
        one = np.array([1.0])
        # RAIN 1 mm: log term 0, the ratio is the constant of each of the four cases
        for t850, tsfc, b in ((-5, 1, 9.4944), (-5, -1, 14.914), (-10, 1, 11.203), (-10, -1, 20.784)):
            self.assertAlmostEqual(float(derive.kma_snow_ratio(one, np.array([tsfc]), np.array([t850]))[0]), b, places=4)
        self.assertEqual(float(derive.kma_snow_ratio(np.array([100.0]), np.array([1.0]), np.array([-5.0]))[0]), 0.0)
        r10 = derive.kma_snow_ratio(np.array([10.0]), np.array([-1.0]), np.array([-5.0]))[0]
        self.assertAlmostEqual(float(r10), -3.688 * np.log(10) + 14.914, places=4)        # natural log

    def test_kma_snow_condition_and_cm(self):
        import numpy as np
        from render import derive
        rain = np.array([1.0, 1.0, 1.0, 0.0])
        t = np.array([-1.0, 3.0, 3.0, -1.0])
        rh = np.array([90.0, 90.0, 60.0, 90.0])     # cold+moist: (1); warm+moist: none; warm+dry 60 < 66.4: (2)
        cond = derive.kma_snow_condition(rain, t, rh)
        self.assertEqual(cond.tolist(), [True, False, True, False])
        mask, cm = derive.kma_snow_cm(rain, 3, t, rh, np.full(4, -5.0))
        self.assertEqual(mask.tolist(), [True, False, True, False])                   # no rain: no snow
        self.assertAlmostEqual(float(cm[0]), 1.4914, places=4)                         # 1 mm x 14.914 / 10
        self.assertEqual(float(cm[1]), 0.0)
        self.assertAlmostEqual(float(derive.rh_from_dewpoint(np.array([5.0]), np.array([5.0]))[0]), 100.0)


class SnowArea(unittest.TestCase):
    def test_snow_area_from_model_snowfall(self):
        import numpy as np
        from render import derive
        m = derive.snow_area(np.array([0.05, 2.0, 2.0, 0.0]), np.array([0.05, 0.8, 1.2, 0.0]), 0.1, 0.5)
        self.assertEqual(m.tolist(), [False, False, True, False])
        for mid in ("ifs", "ifs01", "aifs"):
            self.assertIn("sf", config.for_model(CFG, mid)["data"]["accum_params"])


class SnowCutoff(unittest.TestCase):
    def test_rain_cutoff_removes_packing_noise(self):
        import numpy as np
        from render import derive
        rain = np.array([0.12, 0.3, 0.4])                 # 0.12 mm: packing noise of the cumulative tp at +123 h
        t, rh, t850 = np.full(3, -3.0), np.full(3, 90.0), np.full(3, -10.0)
        m, cm = derive.kma_snow_cm(rain, 3, t, rh, t850, min_rain3h_mm=0.25)
        self.assertEqual(m.tolist(), [False, True, True])
        m6, _ = derive.kma_snow_cm(rain, 6, t, rh, t850, min_rain3h_mm=0.25)   # 6 h: compared as a 3 h mean
        self.assertEqual(m6.tolist(), [False, False, False])
        self.assertEqual(config.for_model(CFG, "ifs")["charts"]["snow"]["rain_cutoff_mm3h"], 0.25)


class WpshSearch(unittest.TestCase):
    def _grid(self, centres):
        import numpy as np
        lat, lon = np.arange(-1.0, 46.01, 0.25), np.arange(109.0, 176.01, 0.25)
        LON, LAT = np.meshgrid(lon, lat)
        gh = 5800.0 + 0 * LAT
        for la, lo, amp, r in centres:
            gh = gh + amp * np.exp(-((LAT - la) ** 2 + (LON - lo) ** 2) / (2 * r * r))
        return gh, lat, lon

    def test_box_reaches_170e(self):
        from render import derive
        gh, lat, lon = self._grid([(25, 165, 150, 6)])       # centre east of the old 160E limit
        r = derive.wpsh_centre(gh, lat, lon, CFG)
        self.assertEqual(r["centre"], (25.0, 165.0))
        self.assertFalse(r["secondary"])

    def test_secondary_centre_when_main_is_outside(self):
        from render import derive
        # heights keep rising eastwards out of the box (main high beyond 170E): a small high at 20N 135E is the
        # highest point within +/- 2.5 deg but not within +/- 5 deg
        import numpy as np
        gh, lat, lon = self._grid([(20, 135, 10, 1.0)])
        gh = gh + (np.meshgrid(lon, lat)[0] - 109) * 3.0
        r = derive.wpsh_centre(gh, lat, lon, CFG)
        self.assertTrue(r["secondary"])
        self.assertAlmostEqual(r["centre"][0], 20.0)
        self.assertAlmostEqual(r["centre"][1], 135.0, delta=0.5)      # the ramp shifts the peak slightly east


class KimKoreaOnly(unittest.TestCase):
    def test_kim_cut_covers_the_korea_map(self):
        import numpy as np
        import pyproj
        r = config.for_model(CFG, "kimr")
        self.assertNotIn("zoom", r["site"])
        d = r["domains"]["default"]
        self.assertEqual(d["extent_m"], CFG["domains"]["korea"]["extent_m"])
        p = pyproj.Proj(proj="lcc", lat_1=30, lat_2=60, lat_0=30, lon_0=126, R=6371229.0)
        x0, x1, y0, y1 = d["extent_m"]
        xs, ys = np.linspace(x0, x1, 40), np.linspace(y0, y1, 40)
        lon, lat = p(np.r_[xs, xs, [x0] * 40, [x1] * 40], np.r_[[y0] * 40, [y1] * 40, ys, ys], inverse=True)
        lon1, lat1, lon2, lat2 = r["kma"]["box"]
        self.assertTrue(lon1 <= lon.min() - 1 and lon.max() + 1 <= lon2 + 0.2)   # map + 1 deg crop margin
        self.assertTrue(lat1 <= lat.min() - 1 and lat.max() + 1 <= lat2 + 0.2)


class KimHourly(unittest.TestCase):
    """Owner 2026-11-24: KIM regional surface charts every hour; the other models unchanged."""

    def test_steps(self):
        k = config.for_model(CFG, "kimr")
        h = fetch.hourly_steps(0, k)
        self.assertEqual(len(fetch.published_steps(0, k)) + len(h), 85)
        self.assertEqual(h[:4], [1, 2, 4, 5])
        self.assertTrue(fetch.is_hourly_only(1, k) and not fetch.is_hourly_only(3, k))
        self.assertEqual(fetch.hourly_steps(0, CFG), [])
        self.assertEqual([fetch.tp_window(s, k) for s in (1, 2, 4, 84)], [1, 2, 3, 3])

    def test_cost_and_fields(self):
        k = config.for_model(CFG, "kimr")
        calls, nbytes = kim.run_cost(k, 29, 56)
        self.assertEqual(calls, 783 + 56 * 7)
        self.assertLess(nbytes, 1.9e9)                 # owner budget about 1.8 GB per run (estimated 1.88 GB)
        src = kim.KimSource(k)
        self.assertEqual(len(src.field_keys(step=1)), 7)
        self.assertEqual(len(src.field_keys(step=3)), 27)
        self.assertEqual(src.field_keys(True, 1), [("tp", 0), ("sf", 0)])

    def test_manifest_flags(self):
        m = site.manifest(CFG, {"kimr": {"2026112400": {"run": "2026112400", "steps": [0, 3],
                                                       "hourly_steps": [1, 2], "wpsh": None, "rv": 15}}})
        k = next(x for x in m["models"] if x["id"] == "kimr")
        hourly = [c["id"] for c in k["charts"] if c.get("hourly")]
        self.assertEqual(sorted(hourly), ["msl", "t2m"])
        self.assertEqual(site.run_entry({"run": "x", "steps": [0], "wpsh": None, "hourly_steps": [1]})["hourly_steps"], [1])


class DerivedNew(unittest.TestCase):
    def test_theta_e(self):
        import numpy as np
        from render import derive
        te = derive.theta_e(np.array([293.15]), np.array([0.012]), 850.0)[0]
        self.assertTrue(340 < te < 348, te)            # about 344 K (Bolton 1980)
        q = derive.specific_humidity(np.array([80.0]), np.array([270.0]), 700.0)
        self.assertAlmostEqual(float(derive.relative_humidity(q, np.array([270.0]), 700.0)[0]), 80.0, places=2)

    def test_vorticity_sign(self):
        import numpy as np
        from render import derive
        lat = np.arange(30, 40.01, 0.25)
        lon = np.arange(120, 130.01, 0.25)
        u = np.repeat(-2.0 * (lat - 35)[:, None], len(lon), axis=1)   # westerly to the south: cyclonic shear
        z = derive.relative_vorticity(u, np.zeros_like(u), lat, lon)[20, 20]
        self.assertAlmostEqual(z * 1e5, 1.8, delta=0.1)
        z2 = derive.relative_vorticity(u[::-1], np.zeros_like(u), lat[::-1], lon)[20, 20]   # N->S grid (ECMWF)
        self.assertAlmostEqual(z2 * 1e5, 1.8, delta=0.1)


class AifsEns(unittest.TestCase):
    """Owner 2026-11-25: AIFS-ENS plume (all members' 850 hPa and 2 m temperature) as its own model."""

    def test_config(self):
        from render import ens, sources
        e = config.for_model(CFG, "aifsens")
        self.assertEqual([c["id"] for c in e["site"]["charts"]], ["plume_seoul"])
        self.assertEqual(len(ens.steps(e)), 61)                       # 0-360 h every 6 h (measured)
        self.assertIsInstance(sources.make_sources(e), ens.EnsSource)
        for mid in ("ifs", "aifs"):                                    # map models do not get the graph chart
            self.assertNotIn("plume_seoul", [c["id"] for c in config.for_model(CFG, mid)["site"]["charts"]])

    def test_bilinear(self):
        import numpy as np
        from render import ens
        ni, nj = 1440, 721
        lat = np.linspace(90, -90, nj)[:, None] * np.ones((1, ni))
        lon = np.arange(ni)[None, :] * 0.25 * np.ones((nj, 1))
        v = (lat * 2 + lon).ravel()
        self.assertAlmostEqual(ens.bilinear(v, ni, nj, 37.57, 126.97), 37.57 * 2 + 126.97, places=6)

    def test_plume_draws(self):
        import datetime as dt
        from render import ens
        e = config.for_model(CFG, "aifsens")
        series = {s: {"t850": {m: -5 + 0.1 * m - 0.01 * s for m in range(51)},
                      "t2m": {m: 3 + 0.1 * m for m in range(51)}} for s in ens.steps(e)}
        png = ens.plume(e, dt.datetime(2026, 11, 25, 0), series)
        self.assertEqual(png[:4], b"\x89PNG")

    def test_bilinear_grid_from_180w(self):
        """Open-data grids start at 180W (assuming 0E put Seoul in the Atlantic, 2026-11-25)."""
        import numpy as np
        from render import ens
        ni, nj = 1440, 721
        lat = np.linspace(90, -90, nj)[:, None] * np.ones((1, ni))
        lon = (-180 + np.arange(ni) * 0.25)[None, :] * np.ones((nj, 1))
        v = (lat * 2 + lon).ravel()
        self.assertAlmostEqual(ens.bilinear(v, ni, nj, 37.57, 126.97, 90, -180, 0.25, 0.25), 37.57 * 2 + 126.97, places=6)

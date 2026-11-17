"""Offline tests for steps, model configs, the storage guard, poll planning and the manifest (no network)."""

import datetime as dt
import json
import pathlib
import tempfile
import unittest

import numpy as np

from render import config, derive, fetch, site
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
        self.assertEqual(len(fetch.published_steps(6, aifs)), 25)

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
        self.assertEqual(config.model_ids(CFG), ["ifs", "ifs01", "aifs", "kimr"])

    def test_chart_lists(self):
        ifs = config.for_model(CFG, "ifs")
        self.assertEqual(len(ifs["site"]["charts"]), 14)
        self.assertEqual(65 * 14, 910)
        ids = [c["id"] for c in ifs["site"]["charts"]]
        self.assertEqual(ids[0], "msl")
        self.assertEqual(ids[-1], "gz200")
        hi = [c["id"] for c in config.for_model(CFG, "ifs01")["site"]["charts"]]
        for gone in ("gz300", "gz200", "gz500_wide", "wpsh500_wide"):
            self.assertNotIn(gone, hi)
        self.assertEqual(len(config.for_model(CFG, "aifs")["site"]["charts"]), 14)

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
            self.assertEqual([x["id"] for x in m["models"]], ["ifs", "ifs01", "aifs", "kimr", "kimz"])
            ifs = m["models"][0]
            self.assertEqual([r["run"] for r in ifs["runs"]], ["2026111500", "2026111506"])   # oldest first
            self.assertEqual(ifs["latest"], "2026111506")
            self.assertNotIn("bytes", ifs["runs"][0])
            back = site.runs_from_manifest(st.get_json("index.json"))
            self.assertEqual(set(back["ifs"]), {"2026111500", "2026111506"})
            self.assertNotIn("kimz", back)             # virtual zoom model: not pruned / re-planned on its own
            self.assertIn("{model}", m["path"])


if __name__ == "__main__":
    unittest.main()

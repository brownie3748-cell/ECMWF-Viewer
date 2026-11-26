"""Offline tests for the split poll jobs: publish.yml (--models ecmwf) and kim.yml (--models kim)."""

import json
import pathlib
import tempfile
import unittest

from render import config, site
from render import store as store_mod
from render.__main__ import _poll_plan, group_models, use_own_counter, write_manifest
from test_site import C

CFG = config.load()


def entry(run):
    return {"run": run, "steps": [0], "wpsh": None, "rv": 1}


class Groups(unittest.TestCase):
    def test_groups_split_the_models(self):
        ecmwf, kim_ = group_models(CFG, "ecmwf"), group_models(CFG, "kim")
        self.assertIn("kimr", kim_)
        self.assertNotIn("kimr", ecmwf)
        self.assertIn("aifs", ecmwf)
        self.assertEqual(sorted(ecmwf + kim_), sorted(config.model_ids(CFG)))

    def test_plan_leaves_other_models_alone(self):
        # the ECMWF job knows no complete KIM runs: it must neither render nor remove them
        have = {"kimr": {"2026112512": entry("2026112512")}, "gone": {"2026112500": entry("2026112500")}}
        todo, remove = _poll_plan(CFG, have, {"aifs": [C("2026112600")]}, 1, group_models(CFG, "ecmwf"))
        self.assertEqual(todo, [(C("2026112600"), "aifs")])
        self.assertEqual(remove, {"gone": ["2026112500"]})               # dropped model: the ECMWF job's
        _, remove = _poll_plan(CFG, have, {"kimr": [C("2026112512")]}, 1, group_models(CFG, "kim"), False)
        self.assertEqual(remove, {})


class SharedManifest(unittest.TestCase):
    def test_merge_keeps_the_other_jobs_runs(self):
        with tempfile.TemporaryDirectory() as t:
            st = store_mod.LocalStore(CFG, pathlib.Path(t))
            # both jobs start from the same manifest
            site.write_manifest(CFG, st, {"aifs": {"2026112518": entry("2026112518")},
                                          "kimr": {"2026112512": entry("2026112512")}})
            start = site.runs_from_manifest(st.get_json("index.json"))
            kim_runs = json.loads(json.dumps(start))
            kim_runs["kimr"]["2026112600"] = entry("2026112600")     # KIM job publishes 00Z
            write_manifest(CFG, st, kim_runs, group_models(CFG, "kim"))
            ec_runs = json.loads(json.dumps(start))                  # ECMWF job: still the old KIM list
            ec_runs["aifs"]["2026112600"] = entry("2026112600")
            write_manifest(CFG, st, ec_runs, group_models(CFG, "ecmwf"))
            got = site.runs_from_manifest(st.get_json("index.json"))
            self.assertEqual(sorted(got["kimr"]), ["2026112512", "2026112600"])
            self.assertEqual(sorted(got["aifs"]), ["2026112518", "2026112600"])


class Counters(unittest.TestCase):
    def test_own_counter_and_shared_limit(self):
        with tempfile.TemporaryDirectory() as t:
            st = store_mod.LocalStore(CFG, pathlib.Path(t))
            m = st.month
            st.put_bytes(f"_ops/{m}.json", json.dumps({"class_a": 100, "class_b": 0}).encode(), "no-cache")
            st.put_bytes(f"_ops/dfs-{m}.json", json.dumps({"class_a": 20, "class_b": 0}).encode(), "no-cache")
            k = store_mod.LocalStore(CFG, pathlib.Path(t))
            self.assertEqual(use_own_counter(k, "kim"), 120)
            self.assertEqual(k.month, f"kim-{m}")
            self.assertEqual(k.scfg["max_class_a_per_month"], CFG["storage"]["max_class_a_per_month"] - 120)
            k.ops["class_a"] += 5
            k.save_ops()
            self.assertEqual(st.get_json(f"_ops/kim-{m}.json")["class_a"], 6)   # + the counter write itself
            self.assertEqual(st.get_json(f"_ops/{m}.json")["class_a"], 100)  # the publish job's file untouched


if __name__ == "__main__":
    unittest.main()

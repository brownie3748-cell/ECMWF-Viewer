"""Offline tests for steps, pruning and the manifest (no network)."""

import datetime as dt
import json
import pathlib
import tempfile
import unittest

from render import config, fetch, site
from render.__main__ import recent_cycles

CFG = config.load()


def fake_run(sitedir: pathlib.Path, run: str, complete: bool = True, wpsh=None):
    d = sitedir / "runs" / run
    d.mkdir(parents=True)
    (d / "msl_000.png").write_bytes(b"x")
    if complete:
        steps = fetch.published_steps(int(run[-2:]), CFG)
        (d / "meta.json").write_text(json.dumps({"run": run, "steps": steps, "wpsh": wpsh}))


class Steps(unittest.TestCase):
    def test_published_steps(self):
        self.assertEqual(len(fetch.published_steps(0, CFG)), 41)
        self.assertEqual(len(fetch.published_steps(6, CFG)), 25)
        self.assertEqual(fetch.published_steps(18, CFG)[-1], 144)

    def test_tp_window(self):
        self.assertEqual(fetch.tp_window(144, CFG), 3)
        self.assertEqual(fetch.tp_window(150, CFG), 6)

    def test_recent_cycles(self):
        c = recent_cycles(3, dt.datetime(2026, 11, 14, 13, 40))
        self.assertEqual([x.hour for x in c], [12, 6, 0])

    def test_image_count_per_run(self):
        # 14 charts per step, no tp at step 0 -> 573 / 349 images
        n_charts = len(CFG["site"]["charts"])
        self.assertEqual(n_charts, 14)
        self.assertEqual(41 * n_charts - 1, 573)


class SiteOps(unittest.TestCase):
    def test_prune_and_manifest(self):
        with tempfile.TemporaryDirectory() as t:
            s = pathlib.Path(t)
            fake_run(s, "2026111300", wpsh={"0": [5940, 5890]})
            fake_run(s, "2026111306")
            fake_run(s, "2026111312", complete=False)          # partial leftover
            removed = site.prune(s, {"2026111306"} & set(site.complete_runs(s)))
            self.assertEqual(removed, ["2026111300", "2026111312"])
            m = site.write_manifest(CFG, s)
            self.assertEqual([r["run"] for r in m["runs"]], ["2026111306"])
            self.assertEqual(m["latest"], "2026111306")
            self.assertEqual(m["schema_version"], 1)
            data = json.loads((s / "index.json").read_text())
            self.assertIn("path", data)
            self.assertTrue((s / "NOTICE.txt").exists())

    def test_manifest_order_oldest_first(self):
        with tempfile.TemporaryDirectory() as t:
            s = pathlib.Path(t)
            for r in ("2026111312", "2026111300", "2026111306"):
                fake_run(s, r)
            m = site.write_manifest(CFG, s)
            self.assertEqual([r["run"] for r in m["runs"]], ["2026111300", "2026111306", "2026111312"])


if __name__ == "__main__":
    unittest.main()

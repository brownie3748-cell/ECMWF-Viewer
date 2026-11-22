"""KMA quota protection: resumable KIM runs, quota stops that keep stored runs, no KIM re-render for a version bump."""

import concurrent.futures as cf
import datetime as dt
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from render import config, kim, pipeline
from render import store as store_mod
from render.__main__ import _complete_runs, _poll_plan, _resume_hook

CFG = config.load()
KIMR = config.for_model(CFG, "kimr")
RUN = dt.datetime(2026, 11, 22, 0)


def fake_render_step(run_s, step, grib_path, tp_prev_path, outdir):
    pathlib.Path(outdir, f"msl_{step:03d}.png").write_bytes(b"x" * 10)
    return {"step": step, "seconds": 0.0, "bytes": 10, "images": 1, "wpsh": None}


class FakeSource:
    def __init__(self, fail_at=None):
        self.calls, self.fail_at = [], fail_at

    def fetch_step(self, run, step, outdir, tp_only=False):
        if not tp_only:
            if step == self.fail_at:
                raise kim.QuotaError("KMA API Hub daily limit reached (HTTP 403)")
            self.calls.append(step)
        return {"path": None, "seconds": 0.0, "source": "kma"}


def run(src, steps, resume, tmp):
    with mock.patch.object(pipeline.cf, "ProcessPoolExecutor", cf.ThreadPoolExecutor), \
            mock.patch.object(pipeline, "render_step", fake_render_step):
        return pipeline.render_run(KIMR, src, RUN, tmp / "site", tmp / "work", steps=steps, resume=resume)


class RenderResume(unittest.TestCase):
    def test_done_steps_are_not_downloaded_again(self):
        with tempfile.TemporaryDirectory() as t:
            got = []
            done = {"0": {"step": 0, "seconds": 0, "bytes": 10, "images": 1, "wpsh": None}}
            src = FakeSource()
            meta = run(src, [0, 3, 6], {"done": done, "on_step": lambda r, f: got.append((r["step"], f))},
                       pathlib.Path(t))
            self.assertEqual(src.calls, [3, 6])
            self.assertEqual(meta["steps"], [0, 3, 6])
            self.assertEqual(sorted(s for s, _ in got), [3, 6])
            self.assertTrue(all(len(f) == 1 for _, f in got))          # each step hands over its own images

    def test_deadline_defers_and_keeps_nothing_new(self):
        with tempfile.TemporaryDirectory() as t:
            src = FakeSource()
            with self.assertRaises(pipeline.Deferred):
                run(src, [0, 3], {"done": {}, "on_step": lambda r, f: None, "deadline": time.monotonic() - 1},
                    pathlib.Path(t))
            self.assertEqual(src.calls, [])

    def test_quota_stop_keeps_finished_steps(self):
        with tempfile.TemporaryDirectory() as t:
            got = []
            with self.assertRaises(kim.QuotaError):
                run(FakeSource(fail_at=6), [0, 3, 6, 9], {"done": {}, "on_step": lambda r, f: got.append(r["step"])},
                    pathlib.Path(t))
            self.assertEqual(sorted(got), [0, 3])


class Hook(unittest.TestCase):
    def test_steps_and_partial_go_to_the_store(self):
        with tempfile.TemporaryDirectory() as t:
            t = pathlib.Path(t)
            st = store_mod.LocalStore(CFG, t / "bucket")
            sizes, usage = {}, kim.new_usage()
            h = _resume_hook(st, "kimr", "2026112200", 10, sizes, usage, None)
            img = t / "msl_003.png"
            img.write_bytes(b"png")
            h["on_step"]({"step": 3, "seconds": 0, "bytes": 3, "images": 1, "wpsh": None}, [img])
            self.assertFalse(img.exists())
            self.assertIn("kimr/2026112200/msl_003.png", sizes)
            self.assertIsNotNone(st.get_json(kim.usage_key()))       # usage saved with every step
            again = _resume_hook(st, "kimr", "2026112200", 10, {}, usage, None)
            self.assertEqual(list(again["done"]), ["3"])
            self.assertEqual(_resume_hook(st, "kimr", "2026112200", 11, {}, usage, None)["done"], {})  # new version

    def test_final_upload_keeps_step_images(self):
        with tempfile.TemporaryDirectory() as t:
            t = pathlib.Path(t)
            st = store_mod.LocalStore(CFG, t / "bucket")
            sizes = {}
            st.put_bytes("kimr/2026112200/msl_003.png", b"png", "x")
            sizes["kimr/2026112200/msl_003.png"] = 3
            (t / "stage").mkdir()
            (t / "stage" / "meta.json").write_text("{}")
            st.upload_dir(t / "stage", "kimr/2026112200/", sizes, last="meta.json", keep_others=True)
            self.assertIn("kimr/2026112200/msl_003.png", sizes)


class QuotaWindow(unittest.TestCase):
    def test_quota_stop_keeps_stored_runs(self):
        """2026-11-22: a 403 on the first probe dropped every stored KIM run and the prune deleted them."""
        class Src:
            def run_complete(self, c):
                raise kim.QuotaError("KMA API Hub daily limit reached (HTTP 403)")
        cycles = [RUN - dt.timedelta(hours=6 * i) for i in range(8)]
        stored = {"2026112112": {"rv": 10}, "2026112100": {"rv": 10}, "2026112012": {"rv": 10}}
        notes = []
        with tempfile.TemporaryDirectory() as t:
            st = store_mod.LocalStore(CFG, t)
            out = _complete_runs(KIMR, st, "kimr", Src(), cycles, stored, 4, notes)
        self.assertEqual([f"{c:%Y%m%d%H}" for c in out], ["2026112112", "2026112100", "2026112012"])
        self.assertEqual(len(notes), 1)
        _, remove = _poll_plan(CFG, {"kimr": stored}, {"kimr": out}, 10)
        self.assertNotIn("kimr", remove)

    def test_partial_run_counts_as_complete_without_probe(self):
        class Src:
            def run_complete(self, c):
                raise AssertionError("no probe for a partly rendered run")
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, minute=0, second=0, microsecond=0)
        c = now.replace(hour=0 if now.hour >= 12 else 12) - (dt.timedelta(days=1) if now.hour < 12 else dt.timedelta(0))
        with tempfile.TemporaryDirectory() as t:
            st = store_mod.LocalStore(CFG, t)
            st.put_bytes(f"kimr/{c:%Y%m%d%H}/partial.json", b'{"rv": 10, "done": {}}', "x")
            self.assertEqual(_complete_runs(KIMR, st, "kimr", Src(), [c], {}, 4, []), [c])


class NoKimRerender(unittest.TestCase):
    def test_version_bump_rerenders_ecmwf_but_not_kim(self):
        c = RUN
        have = {"kimr": {"2026112200": {"rv": 1}}, "ifs": {"2026112200": {"rv": 1}}}
        todo, _ = _poll_plan(CFG, have, {"kimr": [c], "ifs": [c]}, 99)
        self.assertIn((c, "ifs"), todo)
        self.assertNotIn((c, "kimr"), todo)


if __name__ == "__main__":
    unittest.main()

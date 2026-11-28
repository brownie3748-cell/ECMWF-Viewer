"""강수 비교 (2026-11-28): the radar image's box, forecast-grid parsing / issuances, the models' comparison maps."""

import datetime as dt
import json
import pathlib
import tempfile
import unittest
from unittest import mock

import numpy as np

from render import compare, config, forecast_grid, pipeline, site
from render import store as store_mod

CFG = config.load()


class RadarBox(unittest.TestCase):
    def test_box_is_the_kim_map(self):
        """Measured M10: the KIM Korea map in the radar's projection, upper-left corner 118.79E 42.81N."""
        b = compare.radar_box(CFG)
        x0, x1, y0, y1 = b["extent"]
        self.assertAlmostEqual(x1 - x0, 1129680, delta=2000)
        self.assertAlmostEqual(y1 - y0, 1278594, delta=2000)
        self.assertAlmostEqual(b["lu"][0], 118.79, places=1)
        self.assertAlmostEqual(b["lu"][1], 42.81, places=1)
        u = compare.radar_url(CFG, dt.datetime(2026, 11, 28, 23), 3)
        self.assertIn("ACC=180", u)
        self.assertIn("DATE=202611282300", u)
        self.assertIn(f"IMG_XDIM={b['w']}", u)

    def test_wanted_hours(self):
        h = compare.wanted_hours(CFG, dt.datetime(2026, 11, 28, 23, 5))
        self.assertEqual(len(h), CFG["compare"]["keep_hours"])
        self.assertEqual(h[-1], dt.datetime(2026, 11, 28, 22))       # 23 h not finished before HH:12


class ForecastGrid(unittest.TestCase):
    def test_parse(self):
        v = np.full(forecast_grid.NX * forecast_grid.NY, -99.0)
        v[5] = 1.0
        text = ",\n".join(", ".join(f"{x:7.2f}" for x in v[i:i + 20]) for i in range(0, v.size, 20)).encode()
        g = forecast_grid.parse(text)
        self.assertEqual(g.shape, (253, 149))
        self.assertEqual(g[0, 5], 1.0)                               # row 0 = south (measured)
        self.assertIsNone(forecast_grid.parse(b"<html>504</html>"))

    def test_issuances_and_urls(self):
        hours = [dt.datetime(2026, 11, 28, 23) - dt.timedelta(hours=i) for i in range(24)][::-1]
        sh = forecast_grid.issuances(CFG, "shrt", hours)
        self.assertTrue(all(f.hour in (2, 5, 8, 11, 14, 17, 20, 23) for f in sh))
        vs = forecast_grid.issuances(CFG, "vsrt", hours)
        self.assertTrue(all(f.minute == 30 for f in vs))
        self.assertEqual(len(forecast_grid.valid_hours(CFG, "vsrt", vs[-1], hours)), 1)
        u = forecast_grid.url(CFG, "vsrt", dt.datetime(2026, 11, 28, 22, 30), dt.datetime(2026, 11, 28, 23))
        self.assertIn("nph-dfs_vsrt_grd?tmfc=202611282230&tmef=2026112823&vars=RN1", u)
        u = forecast_grid.url(CFG, "shrt", dt.datetime(2026, 11, 28, 20), dt.datetime(2026, 11, 28, 23))
        self.assertIn("nph-dfs_shrt_grd?tmfc=2026112820&tmef=2026112823&vars=PCP", u)

    def test_run_sums_windows_and_indexes(self):
        hours = [dt.datetime(2026, 11, 28, 23) - dt.timedelta(hours=i) for i in range(3)][::-1]
        g = np.full((253, 149), -99.0, np.float32)
        g[100:120, 50:70] = 1.0
        with tempfile.TemporaryDirectory() as t, \
                mock.patch.object(forecast_grid, "fetch", lambda *a: g), \
                mock.patch.object(forecast_grid, "frame", lambda c, p, fc, ef, w, acc: acc.tobytes()[:8] +
                                  np.float32(np.nanmax(acc)).tobytes()):
            st = store_mod.LocalStore(CFG, t)
            lines, out = forecast_grid.run(CFG, st, {}, hours, {})
            vs = out["vsrt"]["202611282030"]
            self.assertEqual(vs["1"], ["2026112821", "2026112822", "2026112823"])
            self.assertEqual(vs["3"], ["2026112823"])                # 21-23 h, all after 20:30
            b = (pathlib.Path(t) / "compare/fcst/vsrt/202611282030/3h_2026112823.webp").read_bytes()
            self.assertEqual(np.frombuffer(b[8:12], np.float32)[0], 3.0)   # three 1 h grids summed
            self.assertTrue(lines[-1].startswith("- forecast:"))


class ModelMaps(unittest.TestCase):
    def test_compare_charts_from_kept_tp(self):
        kim = config.for_model(CFG, "kimr")
        with tempfile.TemporaryDirectory() as t:
            gdir, rdir = pathlib.Path(t) / "g", pathlib.Path(t) / "r"
            (gdir / "tp").mkdir(parents=True)
            rdir.mkdir()
            lat, lon = np.arange(30, 45.01, 0.5), np.arange(118, 134.01, 0.5)
            for s in range(0, 7):
                np.savez(gdir / "tp" / f"{s}.npz", lat=lat, lon=lon, tp=np.full((lat.size, lon.size), s / 1000.0))
            calls = []

            class Pool:
                def submit(self, fn, *a):
                    calls.append(a)
                    f = mock.Mock()
                    f.result.return_value = {"step": a[1], "window": a[2], "bytes": 10}
                    return f
            out = pipeline.compare_charts(kim, Pool(), dt.datetime(2026, 11, 28, 0), gdir, rdir)
            self.assertEqual(out["cmp"], {"1": [1, 2, 3, 4, 5, 6], "3": [3, 4, 5, 6]})
            self.assertEqual(out["images"], 10)
        self.assertEqual(pipeline.compare_windows(config.for_model(CFG, "ifs")), [3])
        self.assertEqual(pipeline.compare_windows(config.for_model(CFG, "aifs")), [])   # owner: AIFS left out

    def test_chart_and_manifest(self):
        ifs = config.for_model(CFG, "ifs")
        lat, lon = np.arange(30, 45.01, 0.25), np.arange(118, 134.01, 0.25)
        acc = np.random.default_rng(0).gamma(0.5, 2.0, (lat.size, lon.size))
        fr = pipeline.plot.chart_compare(ifs, lat, lon, acc, dt.datetime(2026, 11, 28, 0), 6, 3)
        self.assertEqual(fr.png_bytes()[:4], b"\x89PNG")
        e = site.run_entry({"run": "2026112800", "steps": [0], "wpsh": None, "cmp": {"3": [3, 6]}})
        self.assertEqual(e["cmp"], {"3": [3, 6]})
        self.assertEqual(len(CFG["compare"]["levels"]), len(CFG["compare"]["colors"]))


if __name__ == "__main__":
    unittest.main()

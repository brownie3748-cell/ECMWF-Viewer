"""Offline tests for the KIM source: regridding, field placement, npz round trip, daily quota guard."""

import datetime as dt
import pathlib
import tempfile
import unittest

import numpy as np
import pyproj

from render import config, grib, kim

CFG = config.load()


def lambert_grid(f):
    """Synthetic KIMR-like Lambert grid (LoV 126, Latin 30/60, 30 km) with a field f(lat, lon)."""
    proj = pyproj.Proj(proj="lcc", lat_1=30, lat_2=60, lat_0=30, lon_0=126, R=6371229.0)
    x0, y0 = proj(110.62, 25.26)
    ni, nj, d = 110, 90, 30000.0
    X, Y = np.meshgrid(x0 + d * np.arange(ni), y0 + d * np.arange(nj))
    lon, lat = proj(X, Y, inverse=True)
    return {"gridType": "lambert", "lats": lat, "lons": lon % 360, "values": f(lat, lon % 360),
            "LoVInDegrees": 126.0, "Latin1InDegrees": 30.0, "Latin2InDegrees": 60.0, "LaDInDegrees": 30.0,
            "DxInMetres": d, "DyInMetres": d, "radius": 6371229.0}


class Regrid(unittest.TestCase):
    def test_lambert_linear_field_is_reproduced(self):
        f = lambda la, lo: 1000 + 3.0 * la - 2.0 * lo        # noqa: E731 - linear in lat/lon
        g = lambert_grid(f)
        lat = np.arange(28.0, 45.0, 0.5)
        lon = np.arange(115.0, 135.0, 0.5)
        out = kim.lambert_to_regular(g, lat, lon)
        LON, LAT = np.meshgrid(lon, lat)
        ok = np.isfinite(out)
        self.assertGreater(ok.mean(), 0.9)
        self.assertLess(np.abs(out[ok] - f(LAT, LON)[ok]).max(), 0.2)    # bilinear on a near-linear field

    def test_outside_domain_is_nan(self):
        g = lambert_grid(lambda la, lo: la * 0 + 1)
        out = kim.lambert_to_regular(g, np.array([0.0, 35.0]), np.array([60.0, 126.0]))
        self.assertTrue(np.isnan(out[0, 0]))
        self.assertAlmostEqual(float(out[1, 1]), 1.0, places=5)

    def test_place_regular_on_lattice(self):
        lat, lon = kim.target_grid(config.for_model(CFG, "kimg"))
        sub_lat = lat[100:110]
        sub_lon = lon[50:60]
        LON, LAT = np.meshgrid(sub_lon, sub_lat)
        g = {"lats": LAT, "lons": LON, "values": LAT * 10 + LON}
        out = kim.place_regular(g, lat, lon)
        np.testing.assert_allclose(out[100:110, 50:60], (LAT * 10 + LON).astype(np.float32), rtol=1e-6)
        self.assertTrue(np.isnan(out[0, 0]))


class Npz(unittest.TestCase):
    def test_roundtrip_through_grib_read(self):
        with tempfile.TemporaryDirectory() as t:
            p = pathlib.Path(t) / "x.npz"
            lat, lon = np.arange(20.0, 30.0, 1.0), np.arange(110.0, 120.0, 1.0)
            kim.save_npz(p, lat, lon, {("gh", 500): np.ones((10, 10)), ("msl", 0): np.zeros((10, 10))})
            F = grib.read(str(p), {"lat_min": 22, "lat_max": 25, "lon_min": 111, "lon_max": 113})
            self.assertEqual(F[("gh", 500)].shape, (4, 3))
            self.assertIn(("msl", 0), F.data)


class Quota(unittest.TestCase):
    def test_budget_and_probe_throttle(self):
        mc = config.for_model(CFG, "kimg")
        src = kim.KimSource(mc, usage={"calls": 17990, "bytes": 0})
        with self.assertRaises(kim.QuotaError):
            src.check_budget(*kim.run_cost(mc, 41))
        for mid in ("kimg", "kimr"):           # every KMA model must be probe-able (kimr had no probe_box)
            with self.subTest(mid):
                self._probe(config.for_model(CFG, mid))

    def _probe(self, mc):
        calls = []
        src = kim.KimSource(mc, usage=kim.new_usage())
        src._get = lambda url: calls.append(url) or b"GRIB"
        old = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, minute=0, second=0, microsecond=0) \
            - dt.timedelta(hours=12)
        run = old.replace(hour=0) if old.hour >= 12 else old.replace(hour=12) - dt.timedelta(days=1)
        self.assertEqual(src.run_complete(run), "kma")
        self.assertIsNone(src.run_complete(run))                 # probed less than 30 min ago
        self.assertEqual(len(calls), 1)
        self.assertNotIn("authKey", calls[0])
        self.assertIsNone(src.run_complete(run.replace(hour=6) if run.hour == 0 else run))  # 06Z / too new


class Config(unittest.TestCase):
    def test_kim_models(self):
        self.assertEqual(config.model_ids(CFG), ["ifs", "ifs01", "aifs", "kimr"])
        r = config.for_model(CFG, "kimr")
        ids = [c["id"] for c in r["site"]["charts"]]
        self.assertNotIn("wpsh500", ids)                         # regional domain does not reach 180E
        self.assertEqual(kim.run_cost(r, 29)[0], 638)
        g = config.for_model(CFG, "kimg")
        self.assertEqual(kim.run_cost(g, 41)[0], 1230)
        # one day of 00/12Z KIM regional (3-hourly, 29 steps) stays inside the guard
        calls = 2 * kim.run_cost(r, 29)[0]
        nbytes = 2 * kim.run_cost(r, 29)[1]
        self.assertLess(calls, CFG["kma_quota"]["max_calls_per_day"])
        self.assertLess(nbytes, CFG["kma_quota"]["max_bytes_per_day"])


if __name__ == "__main__":
    unittest.main()

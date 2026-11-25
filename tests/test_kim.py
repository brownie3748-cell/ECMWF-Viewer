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
        src = kim.KimSource(mc, usage=dict(kim.new_usage(), calls=17990))
        with self.assertRaises(kim.QuotaError):
            src.check_budget(*kim.run_cost(mc, 41))
        for mid in ("kimg", "kimr"):           # every KMA model must be probe-able (kimr had no probe_box)
            with self.subTest(mid):
                self._probe(config.for_model(CFG, mid))

    def _probe(self, mc):
        calls = []
        src = kim.KimSource(mc, usage=kim.new_usage())
        src._get = lambda url: calls.append(url) or b"GRIB"
        src.k["max_fetch_age_h"] = 12
        self.assertIsNone(src.run_complete(dt.datetime(2026, 1, 1, 0)))      # too old to fetch (max_fetch_age_h)
        src.k["max_fetch_age_h"] = 1e9
        self.assertEqual(src.run_complete(dt.datetime(2026, 1, 1, 0)), "kma")   # old run: no probe request
        self.assertEqual(calls, [])
        src.k["assume_complete_after_h"] = 10_000                             # probe path below
        old = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, minute=0, second=0, microsecond=0) \
            - dt.timedelta(hours=12)
        run = old.replace(hour=0) if old.hour >= 12 else old.replace(hour=12) - dt.timedelta(days=1)
        self.assertEqual(src.run_complete(run), "kma")
        self.assertIsNone(src.run_complete(run))                 # probed less than 30 min ago
        self.assertEqual(len(calls), 1)
        self.assertNotIn("authKey", calls[0])
        self.assertIsNone(src.run_complete(run.replace(hour=6) if run.hour == 0 else run))  # 06Z / too new


class HubLimit(unittest.TestCase):
    def test_403_daily_limit_stops_without_retry(self):
        from unittest import mock
        src = kim.KimSource(config.for_model(CFG, "kimr"), usage=kim.new_usage())
        src.key = "x"
        resp = mock.Mock(status_code=403, content="{\"message\": \"일일 최대 호출 용량 제한으로 사용할 수 없습니다.\"}".encode())
        with mock.patch.object(kim.requests, "get", return_value=resp) as g, mock.patch.object(kim.time, "sleep"):
            with self.assertRaises(kim.QuotaError):
                src._get("https://example.invalid/?a=1")
            self.assertEqual(g.call_count, 1)


class Config(unittest.TestCase):
    def test_kim_models(self):
        self.assertEqual(config.model_ids(CFG), ["ifs", "ifs01", "aifs", "aifsens", "kimr"])
        r = config.for_model(CFG, "kimr")
        ids = [c["id"] for c in r["site"]["charts"]]
        self.assertNotIn("wpsh500", ids)                         # regional domain does not reach 180E
        self.assertEqual(kim.run_cost(r, 29)[0], 783)              # 27 fields: + RH2, SNOW (owner 2026-11-23)
        g = config.for_model(CFG, "kimg")
        self.assertEqual(kim.run_cost(g, 41)[0], 1230)
        # one day of 00/12Z KIM regional (3-hourly, 29 steps) stays inside the guard
        calls = 2 * kim.run_cost(r, 29)[0]
        nbytes = 2 * kim.run_cost(r, 29, 56)[1]              # with the hourly surface steps
        self.assertLess(calls, CFG["kma_quota"]["max_calls_per_day"])
        self.assertLess(nbytes, CFG["kma_quota"]["max_bytes_per_day"])


if __name__ == "__main__":
    unittest.main()


class SnowFields(unittest.TestCase):
    def test_dewpoint_and_window_fields(self):
        td = kim.dewpoint_k(np.array([293.15, 273.15]), np.array([50.0, 100.0]))
        self.assertAlmostEqual(float(td[0]) - 273.15, 9.26, delta=0.1)     # 20 C, 50 % -> 9.3 C
        self.assertAlmostEqual(float(td[1]), 273.15, delta=0.01)
        src = kim.KimSource(config.for_model(CFG, "kimr"))
        self.assertEqual(src.field_keys(tp_only=True), [("tp", 0), ("sf", 0)])


class UsageDay(unittest.TestCase):
    """2026-11-25: the 24th's count (3.02 GB) was saved as the 25th's and the guard refused 2026112412."""

    def test_count_restarts_at_kst_midnight(self):
        import datetime as dt
        before = dt.datetime(2026, 11, 24, 14, 50, tzinfo=dt.timezone.utc)      # 23:50 KST
        after = dt.datetime(2026, 11, 24, 15, 10, tzinfo=dt.timezone.utc)       # 00:10 KST, the 25th
        u = kim.new_usage(before)
        u["calls"], u["bytes"], u["probes"] = 2522, 3.02e9, {"KIMR2026112412": 1.0}
        kim.roll_usage(u, after)
        self.assertEqual((u["day"], u["calls"], u["bytes"]), ("2026-11-25", 0, 0))
        self.assertIn("KIMR2026112412", u["probes"])
        kim.roll_usage(u, after)                                                   # same day: kept
        self.assertEqual(u["day"], "2026-11-25")

    def test_legacy_file_without_day_is_not_trusted(self):
        import tempfile, json
        from render import store as store_mod
        with tempfile.TemporaryDirectory() as t:
            st = store_mod.LocalStore(CFG, t)
            st.put_bytes(kim.usage_key(), json.dumps({"calls": 2542, "bytes": 3.0e9, "probes": {"a": 1}}).encode(), "x")
            u = kim.load_usage(st)
            self.assertEqual((u["calls"], u["bytes"], u["probes"]), (0, 0, {"a": 1}))
            u["calls"] = 5
            kim.save_usage(st, u)
            self.assertEqual(kim.load_usage(st)["calls"], 5)

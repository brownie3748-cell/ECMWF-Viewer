"""KMA forecast images (예보 tab): issuance times, range probe, publish / prune, against a fake hub."""

import datetime as dt
import hashlib
import json
import tempfile
import unittest
from unittest import mock

from render import config, dfs, store

CFG = config.load(None)


def _png(tag: str) -> bytes:
    return b"\x89PNG" + hashlib.sha1(tag.encode()).digest() * 40       # > 400 bytes, unique per tag


class FakeResp:
    def __init__(self, content, status=200):
        self.content, self.status_code = content, status


class FakeSession:
    """Short-range: data to fc + 88 h; ultra-short: to fc + 5 h; beyond: the product's "no data" map."""

    def __init__(self):
        self.calls = []

    def get(self, url, timeout=None):
        q = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))
        self.calls.append(q)
        prod = "vsrt" if "vsrt" in url else "shrt"
        fc = dt.datetime.strptime(q["tm_fc"], "%Y%m%d%H%M")
        ef = dt.datetime.strptime(q["tm_ef"], "%Y%m%d%H%M")
        if q["data1"] in ("TMP", "PCP"):
            return FakeResp(b"\x89PNG" + b"\0" * 168)                  # unknown element: 172-byte blank
        last = fc + dt.timedelta(hours=88 if prod == "shrt" else 5)
        if ef > last:
            return FakeResp(_png("nodata-" + prod))
        return FakeResp(_png(url))


class Issuance(unittest.TestCase):
    def test_latest_issue(self):
        now = dt.datetime(2026, 11, 26, 10, 14)
        self.assertEqual(dfs.latest_issue("shrt", CFG, now), dt.datetime(2026, 11, 26, 8))
        self.assertEqual(dfs.latest_issue("vsrt", CFG, now), dt.datetime(2026, 11, 26, 9))     # 10:00 from 10:15
        self.assertEqual(dfs.latest_issue("shrt", CFG, dt.datetime(2026, 11, 26, 2, 20)), dt.datetime(2026, 11, 25, 23))

    def test_elements(self):
        self.assertEqual(dfs.element_vars(CFG, "vsrt"), ["T1H", "PTY", "SKY", "RN1", "REH", "WSD"])
        self.assertIn("T3H", dfs.element_vars(CFG, "shrt"))               # TMP is blank on this API (measured)


class Publish(unittest.TestCase):
    def test_run_and_prune(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {"KMA_APIHUB_KEY": "k"}), \
                mock.patch.object(dfs.requests, "Session", FakeSession), mock.patch.object(dfs.time, "sleep"):
            st = store.open_store(CFG, f"local:{d}")
            now = dt.datetime(2026, 11, 26, 10, 14)
            lines = dfs.run(CFG, st, now=now)
            man = st.get_json("dfs/index.json")
            s = man["products"]["shrt"][-1]
            self.assertEqual(s["fc"], "202611260800")
            self.assertEqual(s["frames"]["T3H"][0], "2026112610")          # from the current hour
            self.assertEqual(s["frames"]["T3H"][-1], "2026113000")         # fc + 88 h = end of D+3
            self.assertEqual(len(s["frames"]["POP"]), len(s["frames"]["T3H"]))
            v = man["products"]["vsrt"][-1]
            self.assertEqual(v["fc"], "202611260900")
            self.assertEqual(v["frames"]["T1H"], [f"20261126{h:02d}" for h in range(10, 15)])
            self.assertTrue(st._get("dfs/shrt/202611260800/T3H_2026112610.png"))
            # same issuances again: nothing fetched
            again = dfs.run(CFG, st, now=now)
            self.assertTrue(any("already published" in x for x in again))
            # three hours later: a new short-range issuance, the oldest beyond keep_issuances is pruned
            for h in (13, 16):
                dfs.run(CFG, st, now=dt.datetime(2026, 11, 26, h, 40))
            man = st.get_json("dfs/index.json")
            self.assertEqual([x["fc"] for x in man["products"]["shrt"]], ["202611261100", "202611261400"])
            self.assertIsNone(st._get("dfs/shrt/202611260800/T3H_2026112610.png"))
            u = st.get_json("_ops/kmadfs-2026-11-26.json")
            self.assertGreater(u["calls"], 0)
            self.assertTrue(lines)


class NoReference(unittest.TestCase):
    def test_reference_lost_skips_product(self):
        """2026-11-26: connection resets lost the "no data" reference and the grey +6 h map was taken as data."""
        class Flaky(FakeSession):
            def get(self, url, timeout=None):
                q = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))
                if dt.datetime.strptime(q["tm_ef"], "%Y%m%d%H%M") - dt.datetime.strptime(q["tm_fc"], "%Y%m%d%H%M") \
                        > dt.timedelta(days=5):
                    raise ConnectionResetError(104, "Connection reset by peer")
                return super().get(url, timeout)
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {"KMA_APIHUB_KEY": "k"}), \
                mock.patch.object(dfs.requests, "Session", Flaky), mock.patch.object(dfs.time, "sleep"):
            st = store.open_store(CFG, f"local:{d}")
            lines = dfs.run(CFG, st, now=dt.datetime(2026, 11, 26, 10, 42))
            self.assertIsNone(st.get_json("dfs/index.json"))
            self.assertTrue(all("not available yet" in x for x in lines[:2]), lines)


if __name__ == "__main__":
    unittest.main()

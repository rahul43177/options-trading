import datetime as dt
import unittest

from research import positions
from research.pricing import price


class PositionTests(unittest.TestCase):
    def test_parse_pos(self):
        p = positions.parse_pos("P-ETH-2600-071026:-2:6.0:stop=3.51:liq=19.34")
        self.assertEqual((p["kind"], p["asset"], p["strike"], p["size"], p["entry"]),
                         ("put", "ETH", 2600.0, -2.0, 6.0))
        self.assertEqual((p["stop"], p["liq"]), (3.51, 19.34))
        self.assertEqual(p["expiry"].isoformat(), "2026-10-07T12:00:00+00:00")
        with self.assertRaises(ValueError):
            positions.parse_pos("P-ETH-2600-071026:-2")

    def test_spot_for_price_roundtrip(self):
        s = positions.spot_for_price(19.34, 2600, 52.0, 0.44, "put", 2722.0)
        self.assertIsNotNone(s)
        self.assertLess(s, 2722.0)
        self.assertAlmostEqual(price(s, 2600, 52.0 / 8760, 0.44, "put"), 19.34, places=3)
        c = positions.spot_for_price(300, 88000, 30.0, 0.40, "call", 86000.0)
        self.assertGreater(c, 86000.0)

    def test_assess_flags(self):
        now = dt.datetime(2026, 10, 5, 7, 0, tzinfo=dt.timezone.utc)
        pos = positions.parse_pos("P-ETH-2600-071026:-2:6.0:liq=19.34")      # no stop
        row = {"spot": 2722.0, "iv": 0.44, "bid": 3.5, "ask": 4.0}
        r = positions.assess(pos, row, equity=411, now=now, paths=300)
        self.assertTrue(any("no stop set" in f for f in r["flags"]))
        self.assertAlmostEqual(r["pnl_gross_if_closed_at_ask"], (6.0 - 4.0) * 2, places=6)
        self.assertGreater(r["delta_units"], 0)                 # short put = long delta
        self.assertGreater(r["p_liq"], r["p_itm"])              # margin, not the thesis, is the risk
        self.assertTrue(any("liquidation" in f and "likelier" in f for f in r["flags"]))

    def test_stop_guards_liquidation(self):
        now = dt.datetime(2026, 10, 5, 7, 0, tzinfo=dt.timezone.utc)
        pos = positions.parse_pos("P-ETH-2600-071026:-2:6.0:stop=12.42:liq=19.34")
        r = positions.assess(pos, {"spot": 2722.0, "iv": 0.44, "bid": 3.5, "ask": 4.0},
                             equity=411, now=now, paths=300)
        self.assertTrue(r["stop_guards_liq"])
        self.assertFalse(any("likelier" in f for f in r["flags"]))
        self.assertLess(r["liq_spot"], r["stop_spot"])           # liq further away than the stop
        self.assertAlmostEqual(r["loss_at_stop_pct_equity"], r["loss_at_stop"] / 411 * 100, places=1)

    def test_liq_before_stop_flag(self):
        now = dt.datetime(2026, 10, 5, 7, 0, tzinfo=dt.timezone.utc)
        pos = positions.parse_pos("P-ETH-2600-071026:-2:6.0:stop=20:liq=19.34")
        r = positions.assess(pos, {"spot": 2722.0, "iv": 0.44, "bid": 3.5, "ask": 4.0},
                             equity=411, now=now, paths=100)
        self.assertTrue(any("BEFORE your stop" in f for f in r["flags"]))


if __name__ == "__main__":
    unittest.main()

import math
import random
import unittest

from research import arrival
from research.entry_scan import cap_at_ask, valid_hours
from research.perp_analytics import atr as runtime_atr
from research.pricing import price, project_premium_arrival


def _table():
    # tiny hand-made table: time grows with distance, P(arrive) falls with distance
    row = {str(m): {"n": 100, "p_hit": max(0.05, 1 - 0.2 * m), "q25": 0.5 * m ** 1.3,
                    "q50": 1.5 * m ** 1.3, "q75": 4.0 * m ** 1.3} for m in arrival.M_GRID}
    return {"table": {"24.0": row, "48.0": {k: dict(v, p_hit=min(1, v["p_hit"] + 0.1)) for k, v in row.items()}}}


class ArrivalHelperTests(unittest.TestCase):
    def test_quantile(self):
        self.assertEqual(arrival.quantile([1, 2, 3, 4, 5], 0.5), 3)
        self.assertAlmostEqual(arrival.quantile([0, 10], 0.25), 2.5)

    def test_wilder_atr_matches_runtime(self):
        rng = random.Random(1)
        bars, p = [], 100.0
        for i in range(60):
            p *= math.exp(rng.gauss(0, 0.01))
            bars.append({"time": i * 3600, "high": p * 1.004, "low": p * 0.996, "close": p})
        self.assertAlmostEqual(arrival.wilder_atr(bars), runtime_atr(bars), places=12)

    def test_first_touch(self):
        m5 = [{"time": i * 300, "high": 100 + i, "low": 99 - i, "close": 100} for i in range(30)]
        self.assertAlmostEqual(arrival.first_touch_hours(m5, 0, 105, True, 24), 5 * 300 / 3600)
        self.assertIsNone(arrival.first_touch_hours(m5, 0, 1000, True, 24))
        self.assertAlmostEqual(arrival.first_touch_hours(m5, 0, 96, False, 24), 3 * 300 / 3600)

    def test_lookup_monotone_and_capped(self):
        t = _table()
        a, b = arrival.lookup(0.5, 24, t), arrival.lookup(2.0, 24, t)
        self.assertLess(a["q50"], b["q50"])
        self.assertGreater(a["p_hit"], b["p_hit"])
        self.assertLessEqual(a["q25"], a["q50"])
        self.assertLessEqual(a["q50"], a["q75"])
        mid = arrival.lookup(1.0, 36, t)                      # halfway between H rows
        self.assertAlmostEqual(mid["p_hit"], (arrival.lookup(1.0, 24, t)["p_hit"] +
                                              arrival.lookup(1.0, 48, t)["p_hit"]) / 2, places=9)
        far = arrival.lookup(6.0, 2.0, t)                     # can't arrive after expiry
        self.assertLessEqual(far["q75"], 2.0)
        self.assertEqual(arrival.lookup(0, 24, t)["q75"], 0.0)
        self.assertIsNone(arrival.lookup(1.0, 24, {}))

    def test_band_order_and_cap(self):
        arr = {"q25": 1.0, "q50": 4.0, "q75": 12.0, "p_hit": 0.7}
        for kind, strike in (("put", 2600.0), ("call", 2800.0)):
            zone = 2690.0 if kind == "put" else 2740.0
            b = project_premium_arrival(strike, kind, 0.42, 52.0, zone, arr)
            self.assertLessEqual(b["floor"], b["base"])
            self.assertLessEqual(b["base"], b["ceiling"])
            self.assertEqual(b["valid_hours"], 12.0)
            self.assertAlmostEqual(b["floor"], price(zone, strike, 40.0 / 8760, 0.42, kind), places=9)
        b = {"floor": 5.0, "base": 9.0, "ceiling": 14.0, "rest_here": 5.0, "valid_hours": 24}
        c = cap_at_ask(b, 7.4)
        self.assertTrue(c["capped"])
        self.assertEqual((c["floor"], c["floor_raw"]), (7.4, 5.0))
        self.assertNotIn("capped", cap_at_ask(b, 4.0))        # floor already above ask: untouched
        self.assertEqual(valid_hours(c), 24)


class RestAtSimulationTest(unittest.TestCase):
    """End-to-end Monte-Carlo check of the rest@ machinery on a synthetic market.

    Fit the arrival table on one simulated series, then on an INDEPENDENT series price the band
    for many (anchor, zone) pairs and check that the real premium at the first touch is >= rest@
    on ~75% of touches (the design promise) and >= base on ~50%. IV is held flat, as measured on
    Delta (IV at the touch ~1.0x the projection IV)."""

    @staticmethod
    def _series(seed, days, sigma_ann=0.5, s0=100.0):
        rng = random.Random(seed)
        dt5 = 5 / (60 * 24 * 365)
        m5, p = [], s0
        for i in range(days * 288):
            o = p
            sub = [p]
            for _ in range(5):                                 # 1-minute sub-steps for wicks
                p *= math.exp(-0.5 * sigma_ann ** 2 * dt5 / 5 + sigma_ann * math.sqrt(dt5 / 5) * rng.gauss(0, 1))
                sub.append(p)
            m5.append({"time": i * 300, "open": o, "high": max(sub), "low": min(sub), "close": p})
        h1 = [{"time": m5[i]["time"], "open": m5[i]["open"],
               "high": max(b["high"] for b in m5[i:i + 12]), "low": min(b["low"] for b in m5[i:i + 12]),
               "close": m5[i + 11]["close"]} for i in range(0, len(m5) - 11, 12)]
        return m5, h1

    def test_rest_at_fill_rate_matches_design(self):
        m5a, h1a = self._series(11, 90)
        table = {"table": arrival.table_from_samples(arrival.build_samples(m5a, h1a, step_h=3))}
        m5b, h1b = self._series(22, 80)
        iv, H, strike_off = 0.5, 48.0, 0.97
        filled_rest = filled_base = touches = 0
        t5 = [b["time"] for b in m5b]
        for i in range(arrival.ATR_WINDOW_H, len(h1b) - int(H), 6):
            a = arrival.wilder_atr(h1b[i - arrival.ATR_WINDOW_H:i])
            k = t5.index(h1b[i]["time"])
            spot = m5b[k]["open"]
            for m in (0.5, 1.0, 2.0):
                zone = spot - m * a
                arr = arrival.lookup(m, H, table)
                band = project_premium_arrival(zone * strike_off, "put", iv, H, zone, arr, iv_bump=0.0)
                hit = arrival.first_touch_hours(m5b, k, zone, False, H)
                if hit is None:
                    continue
                touches += 1
                real = price(zone, zone * strike_off, max(H - hit, 0.25) / 8760, iv, "put")
                filled_rest += real >= band["floor"] - 1e-12
                filled_base += real >= band["base"] - 1e-12
        self.assertGreater(touches, 100)
        self.assertAlmostEqual(filled_rest / touches, 0.75, delta=0.08)
        self.assertAlmostEqual(filled_base / touches, 0.50, delta=0.10)


if __name__ == "__main__":
    unittest.main()

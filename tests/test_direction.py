import unittest

import numpy as np
import pandas as pd

from research.direction import build, combine, poc, structure_votes, trend_vote, WEIGHTS


def _walk(n=2600, seed=7):
    rng = np.random.default_rng(seed)
    close = 80000 * np.exp(np.cumsum(rng.normal(0, 0.004, n)))
    openp = np.r_[close[0], close[:-1]]
    high = np.maximum(openp, close) * (1 + rng.uniform(0, 0.002, n))
    low = np.minimum(openp, close) * (1 - rng.uniform(0, 0.002, n))
    idx = pd.date_range("2025-01-06", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({"open": openp, "high": high, "low": low, "close": close,
                         "volume": rng.uniform(10, 100, n)}, index=idx)


class RuleTests(unittest.TestCase):
    def test_trend_vote(self):
        up = pd.Series(np.linspace(100, 200, 80))
        dn = pd.Series(np.linspace(200, 100, 80))
        self.assertEqual(trend_vote(up).iloc[-1], 1)
        self.assertEqual(trend_vote(dn).iloc[-1], -1)

    def test_structure_break_up_then_down(self):
        high = np.array([10, 11, 12, 15, 12, 11, 10, 11, 12, 13, 16, 14, 13, 12, 8, 7, 6], float)
        low = high - 1
        close = high - 0.5
        v = structure_votes(high, low, close, lr=2)
        self.assertEqual(v[10], 1)                     # close 15.5 breaks the confirmed 15 swing high
        self.assertEqual(v[-1], -1)                    # later breaks below a confirmed swing low

    def test_poc_finds_heavy_price(self):
        prices = np.r_[np.full(50, 100.0), np.linspace(90, 110, 50)]
        vols = np.r_[np.full(50, 10.0), np.ones(50)]
        self.assertAlmostEqual(poc(prices, vols, bins=40), 100.0, delta=0.6)

    def test_combine_thresholds(self):
        self.assertEqual(combine({"weekly": 1, "daily": 1})[1], "PUT")          # 2+2 = 4
        self.assertEqual(combine({"weekly": -1, "h4": -1})[1], "CALL")          # -3, daily 0
        self.assertEqual(combine({"weekly": 1, "daily": -1, "h1": 1})[1], "STAND ASIDE")

    def test_never_fight_weekly_daily_gate(self):
        # strongly bearish short-term votes inside a weekly uptrend must NOT become a CALL signal
        pullback = {"weekly": 1, "daily": 0, "h4": -1, "h1": -1, "structure": -1, "vprofile": -1, "flow": -1}
        score, side = combine(pullback)
        self.assertEqual(score, -3)
        self.assertEqual(side, "STAND ASIDE")
        self.assertEqual(combine({"location": -1, "weekly": 1, "daily": 1})[0], 4)   # location is info-only


class NoLookaheadTest(unittest.TestCase):
    def test_features_identical_when_future_is_removed(self):
        h1 = _walk()
        full = build(h1)
        cols = list(WEIGHTS) + ["score"]
        for cut in (1500, 1900, 2333):
            part = build(h1.iloc[:cut])
            a = full[cols].iloc[:cut]
            b = part[cols]
            pd.testing.assert_frame_equal(a, b, check_dtype=False, obj=f"cut={cut}")


if __name__ == "__main__":
    unittest.main()

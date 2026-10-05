import unittest

from research.zones import bollinger, classic_pivots, cluster_levels, _round_arrival


class PivotTests(unittest.TestCase):
    def test_classic_pivots_formula(self):
        p = classic_pivots(110.0, 90.0, 100.0)
        self.assertAlmostEqual(p["P"], 100.0)
        self.assertAlmostEqual(p["R1"], 110.0)   # 2*100 - 90
        self.assertAlmostEqual(p["S1"], 90.0)    # 2*100 - 110
        self.assertAlmostEqual(p["R2"], 120.0)   # P + (H-L)
        self.assertAlmostEqual(p["S2"], 80.0)    # P - (H-L)
        # R/S ordering is sane
        self.assertGreater(p["R3"], p["R2"])
        self.assertLess(p["S3"], p["S2"])


class BollingerTests(unittest.TestCase):
    def test_flat_series_zero_width(self):
        lo, mid, hi = bollinger([100.0] * 20, 20, 2.0)
        self.assertEqual((lo, mid, hi), (100.0, 100.0, 100.0))

    def test_too_short_returns_none(self):
        self.assertIsNone(bollinger([100.0] * 5, 20))

    def test_band_brackets_mean(self):
        lo, mid, hi = bollinger([100 + (i % 2) for i in range(20)], 20, 2.0)
        self.assertLess(lo, mid)
        self.assertGreater(hi, mid)


class ClusterTests(unittest.TestCase):
    def test_resistance_side_filter_and_order(self):
        spot = 100.0
        cands = [(101.0, "a"), (99.0, "below"), (108.0, "b")]
        bands = cluster_levels(spot, cands, "resistance", 0.02)  # tol = 2.0
        # the below-spot level is dropped; nearest band first
        self.assertEqual(bands[0]["sources"], ["a"])
        self.assertTrue(all(b["lo"] > spot for b in bands))

    def test_near_vs_structural_split(self):
        spot = 1000.0
        # 1010/1012 cluster (tol 5), 1050 is its own band further out
        cands = [(1010.0, "x"), (1012.0, "y"), (1050.0, "z")]
        bands = cluster_levels(spot, cands, "resistance", 0.005)  # tol = 5.0
        self.assertEqual(len(bands), 2)
        self.assertEqual(bands[0]["n_sources"], 2)
        self.assertEqual(bands[0]["arrival"], 1010.0)   # resistance arrival = lower edge
        self.assertEqual(bands[1]["sources"], ["z"])

    def test_width_cap_defeats_chaining(self):
        # Five levels each within tol of the next (single-linkage would chain all into one band).
        # With the width cap they must split so the far levels become a separate STRUCTURAL band.
        spot = 1000.0
        cands = [(996.0, "a"), (992.0, "b"), (988.0, "c"), (984.0, "d"), (980.0, "e")]
        # tol = 5 (each gap is 4, so pairwise-adjacent), max_span = 6 (caps band width)
        bands = cluster_levels(spot, cands, "support", 0.005, max_span_frac=0.006)
        self.assertGreater(len(bands), 1)
        for b in bands:
            self.assertLessEqual(b["hi"] - b["lo"], 6.0 + 1e-9)

    def test_support_arrival_is_upper_edge(self):
        spot = 1000.0
        cands = [(980.0, "x"), (978.0, "y")]
        bands = cluster_levels(spot, cands, "support", 0.005)
        self.assertEqual(bands[0]["arrival"], 980.0)    # support arrival = upper edge (reached first)

    def test_round_arrival_rounds_away_from_spot(self):
        self.assertEqual(_round_arrival(85124.0, 100.0, "resistance"), 85200.0)
        self.assertEqual(_round_arrival(85124.0, 100.0, "support"), 85100.0)
        self.assertEqual(_round_arrival(2671.0, 10.0, "support"), 2670.0)
        self.assertEqual(_round_arrival(2670.0, 10.0, "resistance"), 2670.0)   # on-tick stays put

    def test_rounded_zones_stay_on_their_side_of_spot(self):
        # live regression: ETH spot 2,700.20 — nearest-rounding sent BOTH arrivals to 2,700
        spot = 2700.20
        res = cluster_levels(spot, [(2704.23, "R2"), (2704.9, "BB-up")], "resistance", 0.0045)[0]
        sup = cluster_levels(spot, [(2696.09, "EMA20"), (2690.0, "POC")], "support", 0.0045)[0]
        r = _round_arrival(res["arrival"], 10.0, "resistance")
        s = _round_arrival(sup["arrival"], 10.0, "support")
        self.assertGreater(r, spot)
        self.assertLess(s, spot)
        self.assertNotEqual(r, s)
        for step in (1.0, 10.0, 100.0):
            for spot in (2700.2, 85311.9, 85292.0):
                for off in (0.01, 0.4, 3.0, 49.0, 120.0):
                    self.assertGreater(_round_arrival(spot + off, step, "resistance"), spot)
                    self.assertLess(_round_arrival(spot - off, step, "support"), spot)

    def test_round_arrival_rejects_unknown_side(self):
        with self.assertRaises(ValueError):
            _round_arrival(100.0, 10.0, "nearest")



class StructuralGapTests(unittest.TestCase):
    def test_structural_too_close_support_and_resistance(self):
        from research.zones import structural_too_close
        near = {"lo": 2690.0, "hi": 2709.0, "arrival": 2709.0}
        close = {"lo": 2683.0, "hi": 2684.0, "arrival": 2684.0}      # 6 below near.lo = 0.22%
        far = {"lo": 2656.0, "hi": 2663.0, "arrival": 2663.0}        # 27 below = 1.0%
        self.assertTrue(structural_too_close(near, close, 2720.0))
        self.assertFalse(structural_too_close(near, far, 2720.0))
        r_near = {"lo": 86554.0, "hi": 87276.0, "arrival": 86554.0}
        r_struct = {"lo": 87400.0, "hi": 87500.0, "arrival": 87400.0}  # 124 above = 0.14%
        self.assertTrue(structural_too_close(r_near, r_struct, 86200.0))
        self.assertFalse(structural_too_close(None, far, 2720.0))


if __name__ == "__main__":
    unittest.main()
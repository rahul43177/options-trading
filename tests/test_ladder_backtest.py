import math
import unittest

from research.ladder_backtest import _pick


class PickTests(unittest.TestCase):
    T = 48 / 8760

    def test_best_never_closer_than_scanner_put(self):
        # shelf edge ABOVE the gate (not binding): the old rule took the highest-delta strike
        for shelf in (90000.0, 84500.0, 83500.0, math.nan):
            scan, best = _pick(True, 85400.0, 84300.0, shelf, self.T, 0.30)
            self.assertFalse(math.isnan(scan))
            self.assertLessEqual(best, scan)

    def test_best_never_closer_than_scanner_call(self):
        for shelf in (80000.0, 86500.0, 87500.0, math.nan):
            scan, best = _pick(False, 85400.0, 86500.0, shelf, self.T, 0.30)
            self.assertFalse(math.isnan(scan))
            self.assertGreaterEqual(best, scan)

    def test_binding_shelf_pushes_best_further(self):
        # scanner pick is 83,600 (|d| 0.166); a shelf edge at 83,300 binds -> BEST moves past it
        scan, best = _pick(True, 85400.0, 84300.0, 83300.0, self.T, 0.30)
        self.assertEqual(scan, 83600.0)
        self.assertLessEqual(best, 83300.0)
        self.assertLess(best, scan)


if __name__ == "__main__":
    unittest.main()

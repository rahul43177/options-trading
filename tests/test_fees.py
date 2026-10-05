import unittest

from research import fees


class OptionFeeTests(unittest.TestCase):
    # Real fills from Delta's asset history, 05-Oct-2026 (fee incl. 18% GST)
    def test_premium_cap_binds_on_otm_eth_put(self):
        # P-ETH-2620-071026 buy 1.5 ETH @ 5.00, ETH ~2,728 -> 0.30975
        self.assertAlmostEqual(fees.option_fee(5.00, 1.5, 2728.0), 0.30975, places=5)
        # P-ETH-2600-071026 buy 2.0 ETH @ 3.70 -> 0.30562
        self.assertAlmostEqual(fees.option_fee(3.70, 2.0, 2728.0), 0.30562, places=5)

    def test_notional_binds_on_rich_premium(self):
        # P-ETH-2620-051026 sell 4.43 ETH @ 9.99 with ETH ~2,668: 0.0001*2668*4.43*1.18 = 1.3947
        self.assertAlmostEqual(fees.option_fee(9.99, 4.43, 2668.0), 0.0001 * 2668 * 4.43 * 1.18, places=6)
        self.assertLess(fees.option_fee(9.99, 4.43, 2668.0), fees.option_fee(9.99, 4.43, None))

    def test_no_spot_uses_cap_upper_bound(self):
        self.assertAlmostEqual(fees.option_fee(6.0, 2.0), 0.035 * 12 * 1.18, places=9)

    def test_rate_and_round_trip(self):
        self.assertAlmostEqual(fees.option_fee_rate(147.0, 85_500.0), 0.035 * 1.18, places=9)
        self.assertEqual(fees.option_fee(0, 1.0, 100.0), 0.0)
        net = fees.net_short_pnl(6.0, 3.7, 2.0, 2728.0)
        self.assertAlmostEqual(net, 4.6 - (0.4956 + 0.30562), places=3)   # 0.4956 = real sell fee

    def test_perp_fee(self):
        # ETHUSD market sell, notional 2051.588 -> fee 1.2104 (0.05% taker + GST)
        self.assertAlmostEqual(fees.perp_fee(2051.588, 1.0), 1.2104, places=3)
        self.assertAlmostEqual(fees.perp_fee(2061.637, 1.0, fees.PERP_MAKER_RATE), 0.4865, places=3)


if __name__ == "__main__":
    unittest.main()

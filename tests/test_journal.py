import unittest

from research import journal

T = "+05:30 IST Asia/Kolkata"


def order(time, contract, side, price, units, fee, realized, status="closed"):
    return {"Time": f"2026-10-05 {time}{T}", "Contract": contract, "Side": side,
            "Exec.Price": str(price), "Order Value": str(units), "Trading Fees": str(fee),
            "Cashflow": "", "Realised P&L": str(realized), "Order Type": "limit_order", "Status": status}


def asset(time, typ, amount, balance, contract=""):
    return {"Date": f"2026-10-05 {time}{T}", "Asset Symbol": "USD", "Transaction type": typ,
            "Amount with GST": str(amount), "Balance": str(balance), "Contract/Fund": contract}


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.orders = [
            order("10:00:00.0", "P-ETH-2600-071026", "sell", 6.0, 2.0, 0.4956, 0),
            order("12:00:00.0", "P-ETH-2600-071026", "buy", 3.7, 2.0, 0.30562, 4.6),
            order("12:01:00.0", "P-ETH-2600-071026", "buy", 0.5, 2.0, 0, 0, status="cancelled"),
            order("11:00:00.0", "ETHUSD", "sell", 2664.4, 2051.588, 1.21, 0),
            order("11:30:00.0", "ETHUSD", "buy", 2677.45, 2061.637, 0.49, -10.05),
        ]
        self.assets = [
            asset("09:00:00.0", "deposit", 400, 400),
            asset("13:00:00.0", "withdrawal", -50, 340),
        ]

    def test_parse_option(self):
        o = journal.parse_option("C-BTC-86000-021026")
        self.assertEqual((o["kind"], o["asset"], o["strike"]), ("call", "BTC", 86000.0))
        self.assertEqual(o["expiry"].isoformat(), "2026-10-02T12:00:00+00:00")
        self.assertIsNone(journal.parse_option("ETHUSD"))

    def test_build(self):
        res = journal.build(self.orders, self.assets)
        by = {r["contract"]: r for r in res["contracts"]}
        put = by["P-ETH-2600-071026"]
        self.assertEqual(put["category"], "short put")
        self.assertEqual(put["fills"], 2)                         # cancelled order ignored
        self.assertAlmostEqual(put["net"], 4.6 - 0.80122, places=4)
        self.assertAlmostEqual(put["capture_pct"], (6.0 - 3.7) / 6.0 * 100, places=1)
        self.assertAlmostEqual(put["hold_hours"], 2.0, places=2)
        self.assertAlmostEqual(put["notional_x_equity"], 2.0 * 2600 / 400, places=1)
        self.assertAlmostEqual(put["stop_loss_pct_equity"], 12.0 / 400 * 100, places=1)   # 1x credit
        self.assertFalse(any(f.startswith("oversized") for f in put["flags"]))  # 3% <= 5% limit
        self.assertEqual(by["ETHUSD"]["category"], "perp")
        self.assertAlmostEqual(res["account"]["growth"], 340 + 50 - 400)
        self.assertEqual(res["options"]["wins"], 1)

    def test_size_flag_threshold(self):
        small = [order("10:00:00.0", "P-ETH-2600-071026", "sell", 6.0, 1.0, 0.25, 0),
                 order("12:00:00.0", "P-ETH-2600-071026", "buy", 3.0, 1.0, 0.12, 3.0)]
        res = journal.build(small, self.assets)
        r = res["contracts"][0]
        self.assertAlmostEqual(r["stop_loss_pct_equity"], 1.5, places=2)   # 6/400
        self.assertFalse(any(f.startswith("oversized") for f in r["flags"]))


if __name__ == "__main__":
    unittest.main()

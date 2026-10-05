import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from research import desk
from research.desk import _reconcile


class ReconcileTests(unittest.TestCase):
    def test_states(self):
        eng = {"symbol": "P-BTC-83800-061026", "setup_id": "BTC-PUT-near"}
        same = {"symbol": "P-BTC-83800-061026", "setup_id": "BTC-PUT-near"}
        other_band = {"symbol": "P-BTC-83800-061026", "setup_id": "BTC-PUT-structural"}
        other_contract = {"symbol": "P-BTC-83200-061026", "setup_id": "BTC-PUT-near"}
        self.assertEqual(_reconcile(eng, same)["status"], "AGREE")
        self.assertTrue(_reconcile(eng, same)["agree"])
        r = _reconcile(eng, other_band)
        self.assertEqual(r["status"], "SAME_CONTRACT_DIFFERENT_BAND")
        self.assertFalse(r["agree"])                       # symbol match alone is NOT agreement
        self.assertEqual(_reconcile(eng, other_contract)["status"], "OVERRIDE")
        self.assertEqual(_reconcile(eng, None)["status"], "NO_DESK_BEST")
        self.assertFalse(_reconcile({}, None)["agree"])


class DeskRunTests(unittest.TestCase):
    def test_same_contract_different_band_is_not_agreement(self):
        # One contract, two PUT bands. The engine favours the ARMED "near" band (trigger-state
        # score); the desk favours the "structural" band because only its zone is reachable
        # before expiry. Old symbol-only reconciliation reported this as AGREE.
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        option = {
            "symbol": "P-BTC-80-030126", "type": "PUT", "strike": 80.0, "spot": 100.0,
            "expiry": now + timedelta(hours=48), "bid": 2.0, "ask": 2.1, "delta": -0.16,
            "iv": 0.50, "oi": 100.0, "timestamp_us": int(now.timestamp() * 1_000_000),
            "product_status": "operational", "bid_size": 50.0, "ask_size": 50.0,
            "contract_value": 1.0,
        }
        common = {"asset": "BTC", "side": "put", "buffer": 5,
                  "weekly_bias": "bullish", "daily_bias": "bullish", "confirmation": False}
        context = {
            "generated_at_utc": now.isoformat(),
            "setups": [
                {**common, "band": "near", "zone": 90, "state": "ARMED"},          # 10 away: unreachable
                {**common, "band": "structural", "zone": 99.99, "state": "WAIT"},  # 0.01 away: reachable
            ],
        }
        with mock.patch.object(desk, "load_options", return_value=[option]), \
             mock.patch.object(desk, "underlying_atr_1h", return_value=0.01):
            res = desk.run(context, now=now)
        rec = res["reconciliation"]
        self.assertEqual(rec["engine_best_symbol"], rec["desk_best_symbol"])
        self.assertEqual(rec["engine_best_setup_id"], "BTC-PUT-near")
        self.assertEqual(rec["desk_best_setup_id"], "BTC-PUT-structural")
        self.assertEqual(rec["status"], "SAME_CONTRACT_DIFFERENT_BAND")
        self.assertFalse(rec["agree"])


class FeeSizingHoldingTests(unittest.TestCase):
    def test_fee_drag_and_net_ev(self):
        r = 0.035 * 1.18                       # cap-binding rate incl. GST
        drag = desk.fee_drag_per_credit(0.16, r)
        self.assertAlmostEqual(drag, r * (1 + 0.84 * 0.5 + 0.16 * 2.0), places=9)
        _, d = desk._premium_grade(0.16, 0.4, None, 3.0, r)
        self.assertAlmostEqual(d["ev_per_credit_gross"], 0.84 - 0.32, places=2)
        self.assertAlmostEqual(d["ev_per_credit"], round(0.52 - drag, 2), places=2)
        _, d0 = desk._premium_grade(0.16, 0.4, None, 3.0)   # default fee_rate=0 -> unchanged
        self.assertEqual(d0["ev_per_credit"], d0["ev_per_credit_gross"])

    def test_max_units_for_risk(self):
        u = desk.max_units_for_risk(6.0, 411, 2.0, 2722.0)
        from research import fees
        # open fee at 6.0 is cap-bound; the stop buyback at 12.0 is notional-bound (0.0001*2722)
        loss_per_unit = 6.0 + fees.option_fee(6.0, 1, 2722.0) + fees.option_fee(12.0, 1, 2722.0)
        self.assertAlmostEqual(fees.option_fee(12.0, 1, 2722.0), 0.0001 * 2722 * 1.18, places=9)
        self.assertAlmostEqual(u, 411 * 0.02 / loss_per_unit, places=6)
        self.assertIsNone(desk.max_units_for_risk(6.0, 0, 2.0, 2722.0))

    def test_already_held_flag_and_best_not_held(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        def opt(sym, strike, delta, bid):
            return {"symbol": sym, "type": "PUT", "strike": strike, "spot": 100.0,
                    "expiry": now + timedelta(hours=48), "bid": bid, "ask": bid * 1.03, "delta": delta,
                    "iv": 0.50, "oi": 100.0, "timestamp_us": int(now.timestamp() * 1_000_000),
                    "product_status": "operational", "bid_size": 50.0, "ask_size": 50.0,
                    "contract_value": 0.01}
        chain = [opt("P-BTC-80-030126", 80.0, -0.12, 2.0), opt("P-BTC-82-030126", 82.0, -0.16, 2.6)]
        context = {"generated_at_utc": now.isoformat(),
                   "setups": [{"asset": "BTC", "side": "put", "buffer": 5, "band": "near", "zone": 99,
                               "state": "VALID", "confirmation": True,
                               "weekly_bias": "bullish", "daily_bias": "bullish"}],
                   "sizing": {"equity": 400, "risk_pct": 2}}
        with mock.patch.object(desk, "load_options", return_value=chain), \
             mock.patch.object(desk, "underlying_atr_1h", return_value=0.5):
            base = desk.run(context, now=now)
            best_sym = base["best"]["symbol"]
            self.assertFalse(base["best"]["already_held"])
            self.assertIsNotNone(base["best"]["sizing"]["max_units"])
            held = desk.run({**context, "positions": [{"symbol": best_sym, "size": -2}]}, now=now)
        self.assertEqual(held["best"]["symbol"], best_sym)          # ranking itself unchanged
        self.assertTrue(held["best"]["already_held"])
        self.assertTrue(any(f.startswith("ALREADY HELD") for f in held["best"]["flags"]))
        self.assertIsNotNone(held["best_not_held"])
        self.assertNotEqual(held["best_not_held"]["symbol"], best_sym)
        other = [c for c in held["candidates"] if c["symbol"] != best_sym][0]
        self.assertTrue(any("same asset+side" in f for f in other["flags"]))


if __name__ == "__main__":
    unittest.main()

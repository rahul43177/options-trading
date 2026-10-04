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


if __name__ == "__main__":
    unittest.main()

import unittest
from datetime import datetime, timedelta, timezone

from research.engine import analyze


class EngineTests(unittest.TestCase):
    def test_one_best_and_missing_evidence_is_explicit(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        base = {
            "type": "CALL", "spot": 100.0, "expiry": now + timedelta(hours=48),
            "bid": 2.0, "ask": 2.1, "delta": 0.16, "iv": 0.50,
            "timestamp_us": int(now.timestamp() * 1_000_000), "product_status": "operational",
            "bid_size": 50.0, "ask_size": 50.0, "contract_value": 1.0,
        }
        chains = {"BTC": [
            {**base, "symbol": "C-BTC-120-030126", "strike": 120.0, "oi": 100.0},
            {**base, "symbol": "C-BTC-125-030126", "strike": 125.0, "oi": 30.0, "delta": 0.12},
        ]}
        context = {
            "generated_at_utc": now.isoformat(),
            "setups": [{"asset": "BTC", "side": "call", "zone": 100, "buffer": 10,
                        "state": "ARMED", "confirmation": False,
                        "weekly_bias": "bearish", "daily_bias": "bearish"}],
        }
        result = analyze(context, chains=chains, now=now)
        self.assertEqual(sum(bool(x["is_best"]) for x in result["candidates"]), 1)
        self.assertEqual(result["best_candidate"]["symbol"], "C-BTC-120-030126")
        self.assertEqual(result["best_candidate"]["marker"], "◄ BEST")
        self.assertIn("iv_rv_edge_unverified", result["best_candidate"]["evidence_gaps"])
        self.assertIn("account_margin_unverified", result["best_candidate"]["evidence_gaps"])


if __name__ == "__main__":
    unittest.main()

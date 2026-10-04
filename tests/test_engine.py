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

    def test_two_bands_same_side_stay_traceable(self):
        # NEAR (WAIT) and STRUCTURAL (VALID+confirmed) PUT bands on one asset: every candidate must
        # carry its band, the same contract may qualify under both, and the decision must use the
        # WINNING band's confirmation — not whichever same-side setup happens to be listed first.
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        base = {
            "type": "PUT", "spot": 100.0, "expiry": now + timedelta(hours=48),
            "bid": 2.0, "ask": 2.1, "delta": -0.16, "iv": 0.50,
            "timestamp_us": int(now.timestamp() * 1_000_000), "product_status": "operational",
            "bid_size": 50.0, "ask_size": 50.0, "contract_value": 1.0, "oi": 100.0,
        }
        chains = {"BTC": [{**base, "symbol": "P-BTC-80-030126", "strike": 80.0}]}
        # no evidence gaps, so the decision actually turns on the winning band's confirmation
        common = {"asset": "BTC", "side": "put", "buffer": 5,
                  "weekly_bias": "bullish", "daily_bias": "bullish",
                  "realized_vol_forecast": 0.30, "event_risk": "low", "macro_risk": "low"}
        context = {
            "generated_at_utc": now.isoformat(),
            "account_state": {"as_of_utc": now.isoformat(), "available_balance": 1000,
                              "initial_margin": 0, "maintenance_margin": 0},
            "setups": [
                {**common, "band": "near", "zone": 95, "state": "WAIT", "confirmation": False},
                {**common, "band": "structural", "zone": 90, "state": "VALID", "confirmation": True},
            ],
        }
        result = analyze(context, chains=chains, now=now)
        eligible = [c for c in result["candidates"] if c.get("eligible")]
        self.assertEqual(sorted(c["band"] for c in eligible), ["near", "structural"])
        self.assertEqual(len({c["setup_id"] for c in eligible}), 2)
        self.assertEqual(sum(bool(c["is_best"]) for c in result["candidates"]), 1)
        # the VALID band scores higher on trigger state, so it must win AND drive the decision
        self.assertEqual(result["best_candidate"]["band"], "structural")
        self.assertEqual(result["best_candidate"]["evidence_gaps"], [])
        self.assertEqual(result["decision"], "PAPER_CANDIDATE")

    def test_setup_ids_unique_and_deterministic(self):
        from research.engine import setup_ids
        setups = [{"asset": "btc", "side": "put", "band": "near"},
                  {"asset": "BTC", "side": "PUT", "band": "near"},
                  {"asset": "ETH", "side": "call"},
                  {"id": "custom", "asset": "ETH", "side": "put"}]
        ids = setup_ids(setups)
        self.assertEqual(ids, ["BTC-PUT-near", "BTC-PUT-near#1", "ETH-CALL-2", "custom"])
        self.assertEqual(ids, setup_ids(setups))


if __name__ == "__main__":
    unittest.main()

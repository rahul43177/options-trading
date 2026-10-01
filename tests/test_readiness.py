import json
import tempfile
import unittest
from pathlib import Path

from research.readiness import audit
from research.storage import connect, store_chain


class ReadinessTests(unittest.TestCase):
    def test_tiny_dataset_stays_collecting(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db = root / "x.sqlite"
            con = connect(db)
            chain = [{
                "symbol": "C-BTC-100-010126", "product_id": 1, "strike_price": "100",
                "contract_type": "call_options", "spot_price": "90", "mark_price": "2",
                "quotes": {"best_bid": "1.9", "best_ask": "2.1"}, "greeks": {},
            }]
            store_chain(con, chain, 1)
            con.execute("INSERT INTO collector_runs VALUES (?,?,?)", (1, 1, "fixture.json"))
            con.commit()
            con.close()
            log = root / "proj.jsonl"
            log.write_text(json.dumps({"row": "realized"}) + "\n", encoding="utf-8")
            result = audit(db, log)
            self.assertEqual(result["status"], "COLLECTING")
            self.assertIn("both_assets_present", result["evidence_gaps"])
            self.assertIn("baseline_30d_dense_5m_coverage", result["evidence_gaps"])


if __name__ == "__main__":
    unittest.main()

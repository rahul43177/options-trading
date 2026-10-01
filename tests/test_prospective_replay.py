import json
import tempfile
import unittest
from pathlib import Path

from research.prospective_replay import replay
from research.storage import connect, store_chain


class ProspectiveReplayTests(unittest.TestCase):
    def test_short_uses_future_bid_then_future_ask(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "x.sqlite"
            con = connect(db)
            symbol = "C-BTC-100-010126"
            def row(bid, ask):
                return [{"symbol": symbol, "contract_value": "1", "quotes": {"best_bid": bid, "best_ask": ask}, "greeks": {}}]
            con.execute(
                "INSERT INTO strategy_decisions(ts_us,decision,details_json) VALUES (?,?,?)",
                (100, "PAPER_CANDIDATE", json.dumps({"best_candidate": {"symbol": symbol}})),
            )
            store_chain(con, row("10", "11"), 200)
            store_chain(con, row("4", "5"), 300)
            con.commit()
            con.close()
            result = replay(db, take_profit=0.5)
            self.assertEqual(result["summary"]["closed_replays"], 1)
            self.assertEqual(result["trades"][0]["entry_bid"], 10.0)
            self.assertEqual(result["trades"][0]["exit_ask"], 5.0)
            self.assertEqual(result["trades"][0]["gross_pnl_quote_currency"], 5.0)


if __name__ == "__main__":
    unittest.main()

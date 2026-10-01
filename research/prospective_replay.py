"""Replay recorded paper candidates against later executable Delta quotes.

For a short option, entry is the first future bid and exit is a later ask.  This
avoids the optimistic mark-to-mark convention used by the historical prototype.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from .config import ROOT


DEFAULT_DB = ROOT / "data/live_option_chain/delta_live.sqlite"


def _candidate(details: dict[str, Any]) -> dict[str, Any] | None:
    candidate = details.get("best_candidate") or details.get("candidate")
    return candidate if isinstance(candidate, dict) else None


def replay(db_path: Path = DEFAULT_DB, take_profit: float = 0.50,
           stop_loss: float = 1.00, max_hold_hours: float = 72.0) -> dict[str, Any]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    decisions = con.execute(
        """SELECT ts_us, decision, details_json FROM strategy_decisions
           WHERE decision IN ('PAPER_CANDIDATE','BEST_CONDITIONAL_MORE_DATA_REQUIRED')
           ORDER BY ts_us"""
    ).fetchall()
    trades: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    busy_until = 0
    for decision in decisions:
        if decision["ts_us"] < busy_until:
            skipped.append({"decision_ts_us": decision["ts_us"], "reason": "overlapping_position"})
            continue
        try:
            details = json.loads(decision["details_json"])
        except (json.JSONDecodeError, TypeError):
            skipped.append({"decision_ts_us": decision["ts_us"], "reason": "invalid_decision_json"})
            continue
        candidate = _candidate(details)
        symbol = candidate.get("symbol") if candidate else None
        if not symbol:
            skipped.append({"decision_ts_us": decision["ts_us"], "reason": "candidate_symbol_missing"})
            continue
        entry = con.execute(
            """SELECT * FROM option_snapshots WHERE symbol=? AND snapshot_ts_us>?
               AND bid>0 AND ask>0 AND ask>=bid ORDER BY snapshot_ts_us LIMIT 1""",
            (symbol, decision["ts_us"]),
        ).fetchone()
        if not entry:
            skipped.append({"decision_ts_us": decision["ts_us"], "symbol": symbol, "reason": "no_future_executable_entry_quote"})
            continue
        entry_price = float(entry["bid"])
        deadline = int(entry["snapshot_ts_us"] + max_hold_hours * 3_600_000_000)
        future = con.execute(
            """SELECT * FROM option_snapshots WHERE symbol=? AND snapshot_ts_us>?
               AND snapshot_ts_us<=? AND ask>0 ORDER BY snapshot_ts_us""",
            (symbol, entry["snapshot_ts_us"], deadline),
        ).fetchall()
        if not future:
            skipped.append({"decision_ts_us": decision["ts_us"], "symbol": symbol, "reason": "no_future_exit_quote"})
            continue
        exit_row = None
        reason = "time_exit"
        for row in future:
            ask = float(row["ask"])
            if ask <= entry_price * (1 - take_profit):
                exit_row, reason = row, "take_profit"
                break
            if ask >= entry_price * (1 + stop_loss):
                exit_row, reason = row, "stop_loss"
                break
        exit_row = exit_row or future[-1]
        contract_value = float(entry["contract_value"] or 1.0)
        gross = (entry_price - float(exit_row["ask"])) * contract_value
        busy_until = int(exit_row["snapshot_ts_us"])
        trades.append({
            "symbol": symbol,
            "decision_ts_us": decision["ts_us"],
            "entry_ts_us": entry["snapshot_ts_us"], "entry_bid": entry_price,
            "exit_ts_us": exit_row["snapshot_ts_us"], "exit_ask": float(exit_row["ask"]),
            "exit_reason": reason, "contract_value": contract_value,
            "gross_pnl_quote_currency": round(gross, 8),
            "fees_included": False,
        })
    con.close()
    wins = sum(t["gross_pnl_quote_currency"] > 0 for t in trades)
    return {
        "mode": "PROSPECTIVE_EXECUTABLE_QUOTE_REPLAY",
        "assumptions": {
            "short_entry": "first future best bid",
            "short_exit": "future best ask",
            "take_profit_fraction": take_profit,
            "stop_loss_fraction_of_premium": stop_loss,
            "max_hold_hours": max_hold_hours,
            "overlap": "one position at a time",
        },
        "summary": {
            "recorded_candidate_decisions": len(decisions),
            "closed_replays": len(trades),
            "skipped": len(skipped),
            "wins": wins,
            "gross_win_rate": round(wins / len(trades), 4) if trades else None,
            "gross_pnl_quote_currency": round(sum(t["gross_pnl_quote_currency"] for t in trades), 8),
        },
        "evidence_gaps": ["fees_and_taxes_not_applied", "queue_position_and_partial_fills_unobserved"],
        "trades": trades,
        "skipped_details": skipped,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--take-profit", type=float, default=0.50)
    parser.add_argument("--stop-loss", type=float, default=1.00)
    parser.add_argument("--max-hold-hours", type=float, default=72.0)
    args = parser.parse_args()
    print(json.dumps(replay(args.db, args.take_profit, args.stop_loss, args.max_hold_hours), indent=2))


if __name__ == "__main__":
    main()

"""Evidence-readiness audit for the options research engine.

This answers "more data required for what?" with explicit, machine-checkable
gates.  Passing these gates makes a strategy suitable for prospective research;
it does not enable live trading.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

from .config import ROOT, UNDERLYINGS


DEFAULT_DB = ROOT / "data/live_option_chain/delta_live.sqlite"
DEFAULT_PROJECTIONS = ROOT / "data/proj_log.jsonl"


def _asset(symbol: str) -> str | None:
    parts = symbol.split("-")
    return parts[1] if len(parts) > 2 else None


def _projection_counts(path: Path) -> tuple[int, int]:
    projected = realized = 0
    if not path.exists():
        return projected, realized
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            kind = json.loads(line).get("row")
        except (json.JSONDecodeError, TypeError):
            continue
        projected += kind == "projection"
        realized += kind == "realized"
    return projected, realized


def audit(db_path: Path = DEFAULT_DB, projections_path: Path = DEFAULT_PROJECTIONS) -> dict[str, Any]:
    if not db_path.exists():
        return {
            "status": "COLLECTING",
            "evidence_gaps": ["no_option_snapshot_database"],
            "next_milestone": "Start the BTC+ETH collector and keep it running.",
        }

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    runs = con.execute(
        "SELECT snapshot_ts_us, option_count FROM collector_runs ORDER BY snapshot_ts_us"
    ).fetchall()
    rows = con.execute(
        "SELECT snapshot_ts_us,symbol,bid,ask FROM option_snapshots"
    ).fetchall()
    closed = con.execute(
        "SELECT count(*) FROM paper_positions WHERE closed_ts_us IS NOT NULL OR state='CLOSED'"
    ).fetchone()[0]
    con.close()

    timestamps = [int(x["snapshot_ts_us"]) for x in runs]
    span_days = (max(timestamps) - min(timestamps)) / 86_400_000_000 if len(timestamps) > 1 else 0.0
    cadence_minutes = None
    if len(timestamps) > 1:
        cadence_minutes = median(
            (b - a) / 60_000_000 for a, b in zip(timestamps, timestamps[1:]) if b > a
        )
    assets = sorted({a for x in rows if (a := _asset(x["symbol"]))})
    latest_ts = max(timestamps) if timestamps else 0
    cut_30 = latest_ts - 30 * 86_400_000_000
    cut_90 = latest_ts - 90 * 86_400_000_000
    per_asset_30 = {
        asset: len({x["snapshot_ts_us"] for x in rows if _asset(x["symbol"]) == asset and x["snapshot_ts_us"] >= cut_30})
        for asset in UNDERLYINGS
    }
    per_asset_90 = {
        asset: len({x["snapshot_ts_us"] for x in rows if _asset(x["symbol"]) == asset and x["snapshot_ts_us"] >= cut_90})
        for asset in UNDERLYINGS
    }
    expected_30 = 30 * 24 * 12
    expected_90 = 90 * 24 * 12
    coverage_30 = min(per_asset_30.values(), default=0) / expected_30
    coverage_90 = min(per_asset_90.values(), default=0) / expected_90
    invalid_quotes = sum(
        x["bid"] is None or x["ask"] is None or x["bid"] <= 0 or x["ask"] <= 0 or x["ask"] < x["bid"]
        for x in rows
    )
    projected, realized = _projection_counts(projections_path)

    gates = {
        "both_assets_present": set(UNDERLYINGS).issubset(assets),
        "baseline_30d_dense_5m_coverage": span_days >= 30 and coverage_30 >= 0.80,
        "target_90d_dense_5m_coverage": span_days >= 90 and coverage_90 >= 0.80,
        "at_least_100_closed_paper_trades": closed >= 100,
        "at_least_50_realized_zone_observations": realized >= 50,
        "valid_executable_quotes": bool(rows) and invalid_quotes / len(rows) <= 0.01,
    }
    gaps = [name for name, passed in gates.items() if not passed]
    if not gates["baseline_30d_dense_5m_coverage"] or not gates["both_assets_present"]:
        status = "COLLECTING"
    elif not all((gates["at_least_100_closed_paper_trades"], gates["at_least_50_realized_zone_observations"])):
        status = "RESEARCH_READY"
    else:
        status = "VALIDATION_READY"

    return {
        "status": status,
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "metrics": {
            "collector_runs": len(runs),
            "option_rows": len(rows),
            "assets": assets,
            "calendar_span_days": round(span_days, 4),
            "median_cadence_minutes": round(cadence_minutes, 3) if cadence_minutes is not None else None,
            "snapshots_by_asset_last_30d": per_asset_30,
            "minimum_asset_30d_coverage_ratio": round(coverage_30, 6),
            "snapshots_by_asset_last_90d": per_asset_90,
            "minimum_asset_90d_coverage_ratio": round(coverage_90, 6),
            "invalid_quote_rows": invalid_quotes,
            "closed_paper_trades": closed,
            "projection_rows": projected,
            "realized_zone_rows": realized,
        },
        "gates": gates,
        "evidence_gaps": gaps,
        "meaning": (
            "The engine may rank conditional paper candidates, but the observed executable "
            "sample is not yet large or diverse enough to claim a repeatable edge."
            if gaps else
            "Minimum prospective evidence gates passed; independent validation and human risk review remain required."
        ),
        "next_milestone": (
            "Collect BTC and ETH every 5 minutes for at least 30 days, then continue toward 90 days; "
            "record zone triggers, bid-at-entry, ask-at-exit, rejected/no-fill cases, fees, and margin snapshots."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--projections", type=Path, default=DEFAULT_PROJECTIONS)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = audit(args.db, args.projections)
    if args.json:
        print(json.dumps(result, indent=2))
        return
    print(f"Evidence status: {result['status']}")
    for key, value in result.get("metrics", {}).items():
        print(f"  {key}: {value}")
    print("Gaps:")
    for gap in result.get("evidence_gaps", []):
        print(f"  - {gap}")
    print(result.get("next_milestone", ""))


if __name__ == "__main__":
    main()

"""Portable SQLite store; every snapshot keeps its original JSON payload."""
from __future__ import annotations
import json, sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
SCHEMA = '''
CREATE TABLE IF NOT EXISTS option_snapshots (
 snapshot_ts_us INTEGER NOT NULL, symbol TEXT NOT NULL, product_id INTEGER,
 expiry_ts TEXT, strike REAL, option_type TEXT, spot REAL, bid REAL, ask REAL,
 mark REAL, bid_iv REAL, ask_iv REAL, mark_iv REAL, delta REAL, gamma REAL,
 theta REAL, vega REAL, oi REAL, volume REAL, bid_size REAL, ask_size REAL,
 contract_value REAL, raw_json TEXT NOT NULL, PRIMARY KEY(snapshot_ts_us, symbol));
CREATE TABLE IF NOT EXISTS collector_runs (snapshot_ts_us INTEGER PRIMARY KEY, option_count INTEGER NOT NULL, raw_path TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper_positions (id INTEGER PRIMARY KEY, opened_ts_us INTEGER, closed_ts_us INTEGER, symbol TEXT, side TEXT, contracts INTEGER, entry_price REAL, exit_price REAL, state TEXT, reason TEXT, pnl_usd REAL);
CREATE TABLE IF NOT EXISTS paper_orders (id INTEGER PRIMARY KEY, ts_us INTEGER, symbol TEXT, side TEXT, price REAL, status TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS paper_fills (id INTEGER PRIMARY KEY, ts_us INTEGER, order_id INTEGER, price REAL, contracts INTEGER);
CREATE TABLE IF NOT EXISTS account_snapshots (ts_us INTEGER PRIMARY KEY, equity_usd REAL, reserve_usd REAL, margin_usd REAL);
CREATE TABLE IF NOT EXISTS strategy_decisions (id INTEGER PRIMARY KEY, ts_us INTEGER, decision TEXT, details_json TEXT);
CREATE TABLE IF NOT EXISTS risk_events (id INTEGER PRIMARY KEY, ts_us INTEGER, severity TEXT, event TEXT, details_json TEXT);
CREATE INDEX IF NOT EXISTS idx_option_snapshots_symbol_ts ON option_snapshots(symbol, snapshot_ts_us);
CREATE INDEX IF NOT EXISTS idx_option_snapshots_ts_type ON option_snapshots(snapshot_ts_us, option_type);
CREATE INDEX IF NOT EXISTS idx_strategy_decisions_ts ON strategy_decisions(ts_us);
'''
def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con
def number(v: Any) -> float | None:
    try: return float(v) if v is not None else None
    except (TypeError, ValueError): return None
def expiry_utc(symbol: str | None) -> str | None:
    """Delta option symbols end DDMMYY; expiry is 17:30 IST (12:00 UTC)."""
    try:
        value = datetime.strptime(str(symbol).split("-")[-1], "%d%m%y").replace(
            hour=12, tzinfo=timezone.utc
        )
        return value.isoformat()
    except (TypeError, ValueError):
        return None
def store_chain(con: sqlite3.Connection, chain: list[dict[str, Any]], snapshot_us: int) -> int:
    rows=[]
    for x in chain:
        q=x.get('quotes') or {}; g=x.get('greeks') or {}
        symbol=x.get('symbol')
        rows.append((snapshot_us,symbol,x.get('product_id'),expiry_utc(symbol),number(x.get('strike_price')),x.get('contract_type'),number(x.get('spot_price')),number(q.get('best_bid')),number(q.get('best_ask')),number(x.get('mark_price')),number(q.get('bid_iv')),number(q.get('ask_iv')),number(q.get('mark_iv')),number(g.get('delta')),number(g.get('gamma')),number(g.get('theta')),number(g.get('vega')),number(x.get('oi')),number(x.get('volume')),number(q.get('bid_size')),number(q.get('ask_size')),number(x.get('contract_value')),json.dumps(x,sort_keys=True)))
    con.executemany('''INSERT OR IGNORE INTO option_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',rows)
    return len(rows)

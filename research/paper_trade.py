"""Read-only paper evaluation: it records decisions only; no order call exists."""
from __future__ import annotations
import json,time
from .collector import collect_once
from .config import ROOT,LIVE_TRADING_ENABLED
from .storage import connect

def main() -> None:
    if LIVE_TRADING_ENABLED: raise RuntimeError('Live trading is intentionally disabled.')
    n=collect_once(); con=connect(ROOT/'data/live_option_chain'/'delta_live.sqlite'); ts=int(time.time()*1_000_000)
    row=con.execute('''SELECT symbol,strike,spot,bid,ask,mark_iv,delta,oi FROM option_snapshots WHERE snapshot_ts_us=(SELECT max(snapshot_ts_us) FROM option_snapshots) AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY abs(abs(delta)-0.15), oi DESC LIMIT 1''').fetchone()
    decision={'mode':'PAPER_ONLY','observed_option_records':n,'candidate':dict(zip(['symbol','strike','spot','bid','ask','mark_iv','delta','oi'],row)) if row else None,'rule':'No entry: true Delta historical premium edge has not yet been established. Continue collecting.'}
    con.execute('INSERT INTO strategy_decisions(ts_us,decision,details_json) VALUES (?,?,?)',(ts,'NO_TRADE_MORE_DATA_REQUIRED',json.dumps(decision))); con.commit(); con.close(); print(json.dumps(decision,indent=2))
if __name__=='__main__': main()

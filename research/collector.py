from __future__ import annotations
import argparse, json, time
from datetime import datetime, timezone
from pathlib import Path
from .delta_api import DeltaPublicClient
from .storage import connect, store_chain
from .config import ROOT, UNDERLYINGS

def collect_once(root: Path = ROOT, assets: tuple[str, ...] = tuple(UNDERLYINGS)) -> int:
    now = datetime.now(timezone.utc); snapshot_us = int(now.timestamp()*1_000_000)
    client = DeltaPublicClient()
    chain = []
    for asset in assets:
        chain.extend(client.option_chain(underlying=asset))
    raw = root/'data/live_option_chain'/f"chain_{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    payload = {'snapshot_ts_us': snapshot_us, 'assets': list(assets), 'rows': chain}
    raw.parent.mkdir(parents=True,exist_ok=True); raw.write_text(json.dumps(payload,indent=2),encoding='utf-8')
    con=connect(root/'data/live_option_chain'/'delta_live.sqlite')
    n=store_chain(con,chain,snapshot_us)
    con.execute('INSERT OR REPLACE INTO collector_runs VALUES (?,?,?)',(snapshot_us,n,str(raw)))
    con.commit(); con.close(); return n
def main() -> None:
    p=argparse.ArgumentParser(); p.add_argument('--once',action='store_true'); p.add_argument('--interval',type=int,default=300)
    p.add_argument('--assets',default=','.join(UNDERLYINGS),help='comma-separated underlyings (default: BTC,ETH)'); a=p.parse_args()
    assets=tuple(x.strip().upper() for x in a.assets.split(',') if x.strip())
    unknown=set(assets)-set(UNDERLYINGS)
    if unknown: p.error(f"unsupported assets: {', '.join(sorted(unknown))}")
    while True:
        try: print(f"collected {collect_once(assets=assets)} option records for {','.join(assets)}",flush=True)
        except Exception as exc: print(f"collector error: {exc}",flush=True)
        if a.once: return
        time.sleep(max(60,a.interval))
if __name__=='__main__': main()

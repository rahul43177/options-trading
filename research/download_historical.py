"""Download untouched, paginated-by-time Delta BTCUSD candles with request evidence."""
from __future__ import annotations
import argparse, csv, hashlib, json, time
from datetime import datetime, timezone
from pathlib import Path
from .config import ROOT
from .delta_api import DeltaPublicClient

MAX_BARS=4000
def download(days: int=730, resolution_minutes: int=5, root: Path=ROOT) -> Path:
    end=int(time.time()//(resolution_minutes*60)*(resolution_minutes*60)); start=end-days*86400
    span=MAX_BARS*resolution_minutes*60
    client=DeltaPublicClient(); all_rows=[]; requests=[]
    # non-overlapping [a,b] blocks; endpoint returns reverse chronological order
    for a in range(start,end,span):
        b=min(a+span,end); rows=client.candles('BTCUSD',f'{resolution_minutes}m',a,b)
        all_rows.extend(rows); requests.append({'start':a,'end':b,'returned':len(rows)})
        print(f"{datetime.fromtimestamp(a,timezone.utc).date()} -> {len(rows)}",flush=True)
    out=root/'data/raw/delta_btc'; out.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    raw=out/f'BTCUSD_{resolution_minutes}m_{days}d_{stamp}.json'
    raw.write_text(json.dumps(all_rows,indent=2),encoding='utf-8')
    digest=hashlib.sha256(raw.read_bytes()).hexdigest()
    (out/f'{raw.stem}.metadata.json').write_text(json.dumps({'endpoint':'/v2/history/candles','symbol':'BTCUSD','resolution':f'{resolution_minutes}m','requested_start_epoch':start,'requested_end_epoch':end,'downloaded_at_utc':stamp,'requests':requests,'raw_sha256':digest},indent=2),encoding='utf-8')
    return raw
def main() -> None:
    p=argparse.ArgumentParser();p.add_argument('--days',type=int,default=730);p.add_argument('--resolution-minutes',type=int,default=5); a=p.parse_args(); print(download(a.days,a.resolution_minutes))
if __name__=='__main__':main()

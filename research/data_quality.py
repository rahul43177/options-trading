from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from .backtest import load_candles
from .config import ROOT

def report(path: Path, resolution_seconds: int = 300) -> str:
    raw = json.loads(path.read_text()); candles = load_candles(path)
    duplicate_count = len(raw) - len({int(x['time']) for x in raw}); gaps=[]; suspicious=[]; zero_volume=0
    for a,b in zip(candles,candles[1:]):
        gap=int(b['time'])-int(a['time'])
        if gap != resolution_seconds: gaps.append({'after':a['time'],'before':b['time'],'seconds':gap})
    for c in candles:
        o,h,l,cl=map(float,(c['open'],c['high'],c['low'],c['close']))
        if float(c.get('volume',0)) == 0: zero_volume += 1
        if l > min(o,cl) or h < max(o,cl) or l > h: suspicious.append(c)
    return f'''# Data quality report

- Source: `{path}`
- Total raw candles: {len(raw)}
- Unique candles: {len(candles)}
- Date range UTC: {datetime.fromtimestamp(candles[0]['time'],timezone.utc).isoformat()} to {datetime.fromtimestamp(candles[-1]['time'],timezone.utc).isoformat()}
- Duplicate timestamps: {duplicate_count}
- Missing/non-5-minute intervals: {len(gaps)}
- Zero-volume candles: {zero_volume}
- Impossible OHLC relationships: {len(suspicious)}

## Cleaning decisions

No observations were deleted. Exact timestamp duplicates are deterministically deduplicated only in the analysis view (last raw occurrence wins); raw JSON remains unchanged. Timestamps are Unix seconds and are converted only to UTC.

## First 100 gaps

```json
{json.dumps(gaps[:100],indent=2)}
```
'''

def main() -> None:
    src=sorted(p for p in (ROOT/'data/raw/delta_btc').glob('BTCUSD_5m_*.json') if '.metadata.' not in p.name)[-1]
    (ROOT/'data_quality_report.md').write_text(report(src)); print(ROOT/'data_quality_report.md')
if __name__=='__main__': main()

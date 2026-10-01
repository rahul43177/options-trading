from __future__ import annotations
import argparse,json
from datetime import datetime,timezone
from pathlib import Path
from .backtest import load_candles,metrics,monte_carlo,run
from .config import BacktestConfig,ROOT
def latest_raw(root:Path)->Path:
    files=sorted(p for p in (root/'data/raw/delta_btc').glob('BTCUSD_5m_*.json') if '.metadata.' not in p.name)
    if not files: raise FileNotFoundError('Run python -m research.download_historical first')
    return files[-1]
def main()->None:
 p=argparse.ArgumentParser();p.add_argument('--input',type=Path);a=p.parse_args();source=a.input or latest_raw(ROOT); candles=load_candles(source);cfg=BacktestConfig(); trades=run(candles,cfg)
 rid=datetime.now(timezone.utc).strftime('%Y-%m-%d_run_%H%M%S');out=ROOT/'backtests/runs'/rid;out.mkdir(parents=True)
 (out/'config.json').write_text(json.dumps(cfg.__dict__,indent=2,default=list)); (out/'trades.json').write_text(json.dumps([t.__dict__ for t in trades],indent=2))
 groups={}
 for t in trades: groups.setdefault((t.dte,t.delta,t.tp),[]).append(t)
 ranked=[]
 for key,group in groups.items():
  item={'dte':key[0],'delta':key[1],'take_profit':key[2],**metrics(group)}; ranked.append(item)
 ranked.sort(key=lambda x:x['expectancy_usd'],reverse=True)
 # Monte Carlo must be one frozen strategy, never a mixture of parameter alternatives.
 best=ranked[0] if ranked else None
 candidate=groups[(best['dte'],best['delta'],best['take_profit'])] if best else []
 summary={'evidence_level':'LEVEL 1 — REAL DELTA BTC CANDLES + SYNTHETIC/MODELED OPTIONS','source':str(source),'candle_count':len(candles),'range_utc':[datetime.fromtimestamp(candles[0]['time'],timezone.utc).isoformat(),datetime.fromtimestamp(candles[-1]['time'],timezone.utc).isoformat()],'configurations_tested':len(ranked),'aggregate_metrics_not_a_portfolio':metrics(trades),'ranked_model_configurations':ranked,'monte_carlo_of_best_in_sample_model_configuration':monte_carlo(candidate)}
 (out/'summary.json').write_text(json.dumps(summary,indent=2));(out/'README.md').write_text('MODELLED Black-Scholes option values. This is not a Delta historical option-chain replay.\n');print(json.dumps(summary,indent=2))
if __name__=='__main__':main()

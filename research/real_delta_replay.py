"""Historical Delta India MARK-price replay for the user's directional two-day hypothesis.

This consumes real, queryable `MARK:<expired option symbol>` candles. Marks are not
historical executable bid/ask, so output is deliberately labelled mark-to-mark.
"""
from __future__ import annotations
import csv,json,math,time
from datetime import datetime,timezone
from pathlib import Path
from .config import ROOT
from .delta_api import DeltaPublicClient
from .backtest import load_candles

INR_PER_USD=85.0 # MODEL conversion only; source/assumption is recorded in the log.
STARTING_INR=20_000.0
CONTRACTS=1 # fixed one 0.001 BTC option contract; no unsafe leverage/margin scaling
TRIGGER_USD=500.0; DISTANCE_USD=1000.0; HOLD_SECONDS=2*86400; FEE_RATE=0.0001

def epoch(s:str)->int:return int(datetime.fromisoformat(s.replace('Z','+00:00')).timestamp())
def closest_mark(rows:list[dict],target:int)->dict|None:
    # Causality: use first completed observed candle at/after the requested action.
    usable=[r for r in rows if int(r['time'])>=target and r.get('close') is not None]
    return min(usable,key=lambda x:int(x['time'])) if usable else None
def choose(products:list[dict], ts:int, spot:float, direction:str)->dict|None:
    kind='call_options' if direction=='up' else 'put_options'; desired=spot+(DISTANCE_USD if direction=='up' else -DISTANCE_USD)
    candidates=[]
    for p in products:
        if p.get('contract_type')!=kind or epoch(p['launch_time'])>ts or epoch(p['settlement_time'])<=ts:continue
        expiry=epoch(p['settlement_time']); hours=(expiry-ts)/3600; strike=float(p['strike_price'])
        if not 36<=hours<=60 or (direction=='up' and strike<spot) or (direction=='down' and strike>spot):continue
        candidates.append(p)
    if not candidates:return None
    return min(candidates,key=lambda p:(abs(float(p['strike_price'])-desired),abs((epoch(p['settlement_time'])-ts)-HOLD_SECONDS)))
def main()->None:
    candles=load_candles(sorted(p for p in (ROOT/'data/raw/delta_btc').glob('BTCUSD_5m_*.json') if '.metadata.' not in p.name)[-1])
    client=DeltaPublicClient(); cache=ROOT/'data/raw/delta_options';cache.mkdir(parents=True,exist_ok=True); expiry_cache={}
    balance=STARTING_INR; logs=[]; used_days=set()
    # 12:00 UTC is fixed before evaluation. Signal uses past 60 minutes, then enters next 5-min mark.
    for i in range(12,len(candles)-12*24*3):
        ts=int(candles[i]['time']); dt=datetime.fromtimestamp(ts,timezone.utc)
        if dt.hour!=12 or dt.date() in used_days: continue
        used_days.add(dt.date()); spot=float(candles[i]['close']); change=spot-float(candles[i-12]['close'])
        if abs(change)<TRIGGER_USD:continue
        direction='up' if change>0 else 'down'; expiry_date=datetime.fromtimestamp(ts+HOLD_SECONDS,timezone.utc).date().isoformat()
        if expiry_date not in expiry_cache:
            payload=client.get('/products',{'states':'expired','contract_types':'call_options,put_options','expiry':expiry_date,'page_size':3000})
            expiry_cache[expiry_date]=[{k:p.get(k) for k in ('symbol','contract_type','launch_time','settlement_time','strike_price','contract_value','taker_commission_rate','settlement_price','id')} for p in payload['result'] if (p.get('underlying_asset') or {}).get('symbol')=='BTC']
        product=choose(expiry_cache[expiry_date],ts,spot,direction)
        if not product:continue
        settle=epoch(product['settlement_time']); entry_target=ts+300; exit_target=min(ts+HOLD_SECONDS,settle)
        try:
            marks=client.candles('MARK:'+product['symbol'],'5m',max(epoch(product['launch_time']),ts),settle)
            entry=closest_mark(marks,entry_target); exit_=closest_mark(marks,exit_target)
        except Exception as exc:
            logs.append({'entry_signal_utc':dt.isoformat(),'status':'SKIPPED_API_ERROR','symbol':product['symbol'],'error':str(exc)});continue
        if not entry or not exit_:
            logs.append({'entry_signal_utc':dt.isoformat(),'status':'SKIPPED_MISSING_MARK','symbol':product['symbol']});continue
        entry_px=float(entry['close']);exit_px=float(exit_['close']); cv=float(product['contract_value']); fee_usd=(entry_px+exit_px)*cv*CONTRACTS*FEE_RATE
        pnl_usd=(entry_px-exit_px)*cv*CONTRACTS-fee_usd; pnl_inr=pnl_usd*INR_PER_USD; before=balance;balance+=pnl_inr
        logs.append({'status':'COMPLETED_MARK_TO_MARK','entry_signal_utc':dt.isoformat(),'entry_mark_utc':datetime.fromtimestamp(entry['time'],timezone.utc).isoformat(),'exit_mark_utc':datetime.fromtimestamp(exit_['time'],timezone.utc).isoformat(),'direction':direction,'symbol':product['symbol'],'strike_usd':float(product['strike_price']),'spot_entry_usd':spot,'one_hour_move_usd':change,'contract_value_btc':cv,'contracts':CONTRACTS,'entry_mark_usd':entry_px,'exit_mark_usd':exit_px,'fee_rate_assumption':FEE_RATE,'fee_usd':fee_usd,'pnl_usd':pnl_usd,'pnl_inr':pnl_inr,'balance_before_inr':before,'balance_after_inr':balance,'data':'REAL_DELTA_HISTORICAL_MARK_CANDLES'})
        print(f"{len(logs)} {product['symbol']} {pnl_inr:+.2f} INR -> {balance:.2f}",flush=True)
    run=ROOT/'backtests/runs'/datetime.now(timezone.utc).strftime('%Y-%m-%d_real_mark_replay_%H%M%S');run.mkdir(parents=True)
    (run/'contracts_by_expiry.json').write_text(json.dumps(expiry_cache,indent=2))
    (run/'trade_log.json').write_text(json.dumps(logs,indent=2));
    complete=[x for x in logs if x['status']=='COMPLETED_MARK_TO_MARK'];
    with (run/'trade_log.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=sorted({k for x in logs for k in x}));w.writeheader();w.writerows(logs)
    wins=sum(x['pnl_inr']>0 for x in complete); summary={'evidence_level':'REAL DELTA HISTORICAL OPTION MARK CANDLES (NOT HISTORICAL BID/ASK FILLS)','starting_balance_inr':STARTING_INR,'final_balance_inr':balance,'net_pnl_inr':balance-STARTING_INR,'completed_trades':len(complete),'skipped_records':len(logs)-len(complete),'wins':wins,'losses':len(complete)-wins,'win_rate':wins/len(complete) if complete else None,'strategy':'BTC +/- $500 over prior 1h; short OTM option ~$1000 away; nearest 36–60h expiry; hold to min(2d, settlement); fixed 1 contract; mark-to-mark P&L','critical_limitations':['marks are not executable bid/ask','no historical margin/liquidation/top-up simulation','INR/USD=85 is a model conversion','one contract only; no leverage scaling']}
    (run/'summary.json').write_text(json.dumps(summary,indent=2));print(json.dumps(summary,indent=2));print(run)
if __name__=='__main__':main()

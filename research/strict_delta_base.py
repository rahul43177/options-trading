"""Strict real-Delta historical MARK replay of the user-supplied base strategy.

No option price is synthesized. Missing exact timestamps are skipped and retained.
"""
from __future__ import annotations
import argparse,csv,json,math,random,statistics
from collections import defaultdict
from datetime import datetime,timezone
from pathlib import Path
from .config import ROOT
from .delta_api import DeltaPublicClient
from .backtest import load_candles

START_INR=20_000.; FX=85.; HOLD=48*3600; BASE_THRESHOLD=500.; BASE_DISTANCE=1000.
TP_LEVELS=(.25,.50,.60,.70,.75,.80,.90); STOP_MULTIPLES=(1.5,2.,2.5,3.,4.,5.)

def ep(s:str)->int:return int(datetime.fromisoformat(s.replace('Z','+00:00')).timestamp())
def slim(p): return {k:p.get(k) for k in ('symbol','contract_type','launch_time','settlement_time','strike_price','contract_value','taker_commission_rate','settlement_price','id')}
class Replay:
 def __init__(self): self.client=DeltaPublicClient();self.products={};self.marks={}
 def contracts(self,day:str):
  if day not in self.products:
   x=self.client.get('/products',{'states':'expired','contract_types':'call_options,put_options','expiry':day,'page_size':3000})['result'];self.products[day]=[slim(p) for p in x if (p.get('underlying_asset') or {}).get('symbol')=='BTC']
  return self.products[day]
 def path(self,p,start,end):
  # A contract may be selected by several hourly signals. Fetch its complete listed life once.
  k=p['symbol']
  if k not in self.marks:self.marks[k]=self.client.candles('MARK:'+p['symbol'],'5m',ep(p['launch_time']),ep(p['settlement_time']))
  return {int(x['time']):float(x['close']) for x in self.marks[k] if start<=int(x['time'])<=end and x.get('close') is not None}
 def select(self,ts,spot,direction,distance=BASE_DISTANCE,window=(36,60)):
  day=datetime.fromtimestamp(ts+HOLD,timezone.utc).date().isoformat(); kind='call_options' if direction=='UP' else 'put_options';desired=spot+(distance if direction=='UP' else -distance);best=[]
  for p in self.contracts(day):
   if p['contract_type']!=kind:continue
   hrs=(ep(p['settlement_time'])-ts)/3600;strike=float(p['strike_price'])
   if not window[0]<=hrs<=window[1] or ep(p['launch_time'])>ts:continue
   if (direction=='UP' and strike<spot) or (direction=='DOWN' and strike>spot):continue
   best.append(p)
  return min(best,key=lambda p:(abs(float(p['strike_price'])-desired),abs(ep(p['settlement_time'])-(ts+HOLD)))) if best else None

def execution(entry,exit,fee,model):
 # Worst-price adjustment to MARK: conservative 5bp, stress 25bp each leg.
 bps={'MARK':0,'CONSERVATIVE':5,'STRESS':25}[model]/10000
 e=entry*(1-bps);x=exit*(1+bps);return e,x,(e+x)*fee
def pnl_short(entry,exit,cv,fee,model='MARK'):
 e,x,fees=execution(entry,exit,fee,model);return (e-x-fees)*cv,fees
def metrics(trades):
 p=[x['net_pnl_inr'] for x in trades];wins=[x for x in p if x>0]; losses=[x for x in p if x<=0]; eq=START_INR;peak=eq;dd=0;ws=ls=bestw=bestl=0
 for v in p:
  eq+=v;peak=max(peak,eq);dd=max(dd,peak-eq)
  if v>0:ws+=1;ls=0
  else:ls+=1;ws=0
  bestw=max(bestw,ws);bestl=max(bestl,ls)
 return {'trades':len(p),'wins':len(wins),'losses':len(losses),'win_rate':len(wins)/len(p) if p else None,'gross_profit_inr':sum(wins),'gross_loss_inr':sum(losses),'net_pnl_inr':sum(p),'expectancy_inr':statistics.mean(p) if p else None,'profit_factor':sum(wins)/-sum(losses) if losses and sum(losses) else None,'largest_win_inr':max(p) if p else None,'largest_loss_inr':min(p) if p else None,'max_drawdown_inr':dd,'max_drawdown_pct':dd/START_INR,'longest_win_streak':bestw,'longest_loss_streak':bestl,'average_mae_inr':statistics.mean(x['mae_inr'] for x in trades) if p else None,'average_mfe_inr':statistics.mean(x['mfe_inr'] for x in trades) if p else None}
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--days',type=int,default=730);args=ap.parse_args()
 candles=load_candles(sorted(p for p in (ROOT/'data/raw/delta_btc').glob('BTCUSD_5m_*.json') if '.metadata.' not in p.name)[-1]);
 if args.days>0: candles=candles[-args.days*288:]
 r=Replay();logs=[];first=int(candles[0]['time'])
 # Hourly sequence anchored to the actual, complete historical 5m grid; past-only one-hour move.
 for i in range(12,len(candles)-12):
  ts=int(candles[i]['time'])
  if (ts-first)%3600:continue
  spot=float(candles[i]['close']);move=spot-float(candles[i-12]['close'])
  if abs(move)<BASE_THRESHOLD:continue
  direction='UP' if move>0 else 'DOWN'
  common={'signal_utc':datetime.fromtimestamp(ts,timezone.utc).isoformat(),'signal_spot_usd':spot,'one_hour_move_usd':move,'direction':direction,'entry_target_utc':datetime.fromtimestamp(ts+300,timezone.utc).isoformat()}
  try:p=r.select(ts,spot,direction)
  except Exception as exc:logs.append({**common,'status':'SKIP_PRODUCT_API_ERROR','error':str(exc)});continue
  if not p: logs.append({**common,'status':'SKIP_NO_36_60H_EXPIRY'});continue
  settle=ep(p['settlement_time']);entry_t=ts+300; exit_t=min(entry_t+HOLD,settle)
  details={**common,'symbol':p['symbol'],'strike_usd':float(p['strike_price']),'target_strike_usd':spot+(BASE_DISTANCE if direction=='UP' else -BASE_DISTANCE),'actual_distance_usd':abs(float(p['strike_price'])-spot),'distance_error_usd':abs(abs(float(p['strike_price'])-spot)-BASE_DISTANCE),'expiry_utc':p['settlement_time'],'hours_to_expiry':(settle-entry_t)/3600,'contract_value_btc':float(p['contract_value']),'contracts':1}
  try:path=r.path(p,ts,settle)
  except Exception as exc:logs.append({**details,'status':'SKIP_MARK_API_ERROR','error':str(exc)});continue
  if entry_t not in path:logs.append({**details,'status':'MISSING_ENTRY_MARK'});continue
  entry=path[entry_t]
  if exit_t in path: exit=path[exit_t];mechanism='MARK_EXIT'
  elif exit_t==settle and p.get('settlement_price') is not None:exit=float(p['settlement_price']);mechanism='SETTLEMENT_EXIT'
  else:logs.append({**details,'status':'MISSING_EXIT','entry_mark_usd':entry});continue
  between=[v for t,v in path.items() if entry_t<=t<=exit_t];cv=float(p['contract_value']);fee=float(p.get('taker_commission_rate') or .0001);net_usd,fees=pnl_short(entry,exit,cv,fee)
  logs.append({**details,'status':'COMPLETED','entry_mark_usd':entry,'exit_mark_usd':exit,'exit_mechanism':mechanism,'gross_pnl_inr':(entry-exit)*cv*FX,'entry_fee_inr':entry*cv*fee*FX,'exit_fee_inr':exit*cv*fee*FX,'fee_inr':fees*cv*FX,'slippage_inr':0,'net_pnl_inr':net_usd*FX,'mae_inr':max(0,max(between)-entry)*cv*FX,'mfe_inr':max(0,entry-min(between))*cv*FX,'max_option_mark_usd':max(between),'min_option_mark_usd':min(between)})
 # Chronological base account: one position at a time, exactly as a ₹20k sequential account.
 completed=sorted((x for x in logs if x['status']=='COMPLETED'),key=lambda x:x['signal_utc']);eligible=[];open_until=''
 for x in completed:
  if x['signal_utc']<open_until:x['portfolio_status']='SKIP_OVERLAPPING_ONE_POSITION';continue
  x['portfolio_status']='TRADED';open_until=x['exit_mechanism'] and x['expiry_utc'] if x['exit_mechanism']=='SETTLEMENT_EXIT' else datetime.fromisoformat(x['entry_target_utc'].replace('Z','+00:00')).timestamp()+HOLD
  if isinstance(open_until,float):open_until=datetime.fromtimestamp(open_until,timezone.utc).isoformat()
  eligible.append(x)
 bal=START_INR
 for x in eligible:x['balance_before_inr']=bal;bal+=x['net_pnl_inr'];x['balance_after_inr']=bal
 # Sensitivity: same strict completed paths, no selection/rerun claim.
 scenarios=[]
 for model in ('MARK','CONSERVATIVE','STRESS'):
  for x in eligible:
   net,fees=pnl_short(x['entry_mark_usd'],x['exit_mark_usd'],x['contract_value_btc'],float(x.get('taker_commission_rate',.0001) or .0001),model);scenarios.append({'scenario':'EXEC_'+model,'symbol':x['symbol'],'net_pnl_inr':net*FX,'source_trade':x['signal_utc']})
  
 for tp in TP_LEVELS:
  for x in eligible:
   # use stored extrema only: exact hit timing not retained in log, so mark as conservative endpoint proxy
   trigger=x['entry_mark_usd']*(1-tp);exitp=min(x['exit_mark_usd'],trigger) if x['min_option_mark_usd']<=trigger else x['exit_mark_usd'];net,_=pnl_short(x['entry_mark_usd'],exitp,x['contract_value_btc'],.0001);scenarios.append({'scenario':f'TP_{int(tp*100)}_ENDPOINT_PROXY','symbol':x['symbol'],'net_pnl_inr':net*FX,'source_trade':x['signal_utc']})
 for stop in STOP_MULTIPLES:
  for x in eligible:
   # Exact intrabar stop fill cannot be known from marks; exit threshold is a stated proxy.
   exitp=x['entry_mark_usd']*stop if x['max_option_mark_usd']>=x['entry_mark_usd']*stop else x['exit_mark_usd'];net,_=pnl_short(x['entry_mark_usd'],exitp,x['contract_value_btc'],.0001);scenarios.append({'scenario':f'STOP_{stop}X_ENDPOINT_PROXY','symbol':x['symbol'],'net_pnl_inr':net*FX,'source_trade':x['signal_utc']})
 run=ROOT/'backtests/runs'/datetime.now(timezone.utc).strftime('%Y-%m-%d_strict_base_%H%M%S');run.mkdir(parents=True)
 for name,data in [('trade_log.json',logs),('scenario_rows.json',scenarios),('contracts_by_expiry.json',r.products)]: (run/name).write_text(json.dumps(data,indent=2))
 fields=sorted({k for x in logs for k in x});
 with (run/'trade_log.csv').open('w',newline='') as f:w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(logs)
 group=defaultdict(list)
 for x in scenarios:group[x['scenario']].append(x['net_pnl_inr'])
 sr=[{'scenario':k,'trades':len(v),'net_pnl_inr':sum(v),'average_pnl_inr':statistics.mean(v),'win_rate':sum(q>0 for q in v)/len(v)} for k,v in group.items()]
 with (run/'scenario_results.csv').open('w',newline='') as f:w=csv.DictWriter(f,fieldnames=sr[0].keys());w.writeheader();w.writerows(sr)
 eq=[];b=START_INR
 for x in eligible:b=x['balance_after_inr'];eq.append({'signal_utc':x['signal_utc'],'balance_inr':b,'drawdown_inr':max(y['balance_inr'] for y in eq)-b if eq else 0})
 with (run/'equity_curve.csv').open('w',newline='') as f:w=csv.DictWriter(f,fieldnames=['signal_utc','balance_inr','drawdown_inr']);w.writeheader();w.writerows(eq)
 coverage={'total_hourly_signals':len(logs),'completed_raw':len(completed),'traded_one_position':len(eligible),'skipped':len(logs)-len(completed),'coverage_completed_pct':len(completed)/len(logs) if logs else 0,'coverage_traded_pct':len(eligible)/len(logs) if logs else 0};(run/'data_coverage_report.csv').write_text('metric,value\n'+'\n'.join(f'{k},{v}' for k,v in coverage.items()))
 summary={'evidence':'REAL DELTA HISTORICAL MARKS; NOT BID/ASK EXECUTION','base_strategy':'hourly ±$500 1h signal; OTM $1000; 36–60h; exact T+5m; 1 contract; 48h/settlement','coverage':coverage,'base_metrics':metrics(eligible),'starting_balance_inr':START_INR,'final_balance_inr':bal,'limitations':['historical marks, not executable bid/ask','historical account margin and liquidation unavailable','TP/stop rows are endpoint proxies, not exact fill simulation','no parameter optimisation conclusion permitted']};(run/'summary.json').write_text(json.dumps(summary,indent=2));print(json.dumps(summary,indent=2));print(run)
if __name__=='__main__':
 try: main()
 except Exception:
  import traceback
  (ROOT/'strict_replay_error.log').write_text(traceback.format_exc())
  raise

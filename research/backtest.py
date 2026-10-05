"""Level-1 BTC-path backtest. Option values are MODELLED, not historical Delta observations."""
from __future__ import annotations
import csv, math, random, statistics
from dataclasses import dataclass,asdict
from datetime import datetime,timezone
from pathlib import Path
from typing import Iterable
from .config import BacktestConfig
from .pricing import price,strike_for_delta
from .fees import option_fee

@dataclass
class Trade:
 strategy:str; entry_ts:int; exit_ts:int; dte:int; delta:float; tp:float; kind:str; strike:float; entry:float; exit:float; pnl_usd:float; mae_usd:float; mfe_usd:float; exit_reason:str; max_reserve_used_usd:float
def load_candles(path:Path)->list[dict]:
    import json
    raw=json.loads(path.read_text()); seen={}
    for x in raw: seen[int(x['time'])]=x
    return [seen[k] for k in sorted(seen)]
def annual_vol(closes:list[float])->float:
    if len(closes)<3:return .6
    rs=[math.log(b/a) for a,b in zip(closes,closes[1:]) if a>0 and b>0]
    return max(.10,min(3.0,statistics.pstdev(rs)*math.sqrt(365*24*12)))
def run(candles:list[dict], config:BacktestConfig, strategy:str='naked') -> list[Trade]:
    lookback=config.lookback_days*24*60//config.candle_minutes; step=24*60//config.candle_minutes
    out=[]; last_day=None
    for i in range(lookback,len(candles)-step):
        ts=int(candles[i]['time']); now=datetime.fromtimestamp(ts,timezone.utc)
        if now.hour!=config.entry_hour_utc or now.date()==last_day: continue
        last_day=now.date(); spot=float(candles[i]['close']); vol=annual_vol([float(c['close']) for c in candles[i-lookback:i+1]])
        # Directional trigger benchmark: +$500/-$500 1hr determines which side is sold.
        change=spot-float(candles[max(0,i-12)]['close']); kind='call' if change>=0 else 'put'
        for dte in config.dtes:
          horizon=min(len(candles)-1,i+dte*step); t=dte/365
          for target in config.deltas:
           strike=strike_for_delta(spot,t,vol,target,kind); entry=price(spot,strike,t,vol,kind)
           # $ per BTC; contract represents 0.001 BTC. Model executable sell: BSM less 1 tick.
           entry=max(0,entry-config.slippage_ticks*config.tick_size)
           if entry<=0:continue
           for tp in config.take_profits:
            close_target=entry*(1-tp); worst=0.; best=0.; exit_price=None; exit_ts=None; reason='time'
            for j in range(i+1,horizon+1):
             remain=max(0,(horizon-j)/(365*step)); mark=price(float(candles[j]['close']),strike,remain,vol,kind)
             pnl=(entry-mark)*.001*config.contracts
             worst=min(worst,pnl);best=max(best,pnl)
             # risk engine: 5% entire 30k account model converted to USD guard. Intentionally conservative.
             if pnl <= -(config.initial_inr/config.usd_inr*.05): exit_price=mark+config.slippage_ticks*config.tick_size;exit_ts=int(candles[j]['time']);reason='account_loss_guard';break
             if mark<=close_target: exit_price=mark+config.slippage_ticks*config.tick_size;exit_ts=int(candles[j]['time']);reason='take_profit';break
            if exit_price is None:
             exit_price=price(float(candles[horizon]['close']),strike,0,vol,kind)+config.slippage_ticks*config.tick_size;exit_ts=int(candles[horizon]['time'])
            # Delta's real option fee per side: min(0.01% x notional, 3.5% x premium) + 18% GST
            # (research.fees, verified on live fills). The old line applied the 0.01% NOTIONAL rate
            # to the PREMIUM, understating fees ~350x on OTM strikes.
            fees=option_fee(entry,.001*config.contracts,spot)+option_fee(exit_price,.001*config.contracts,spot)
            pnl=(entry-exit_price)*.001*config.contracts-fees
            # MODEL reserve: first $15k INR is trade capital, remainder reserve. premium MTM alone cannot reproduce exchange margin.
            reserve=max(0,-worst-config.initial_inr/config.usd_inr/2)
            out.append(Trade(strategy,ts,exit_ts,dte,target,tp,kind,strike,entry,exit_price,pnl,worst,best,reason,reserve))
    return out
def metrics(trades:list[Trade])->dict:
    pnls=[t.pnl_usd for t in trades]; wins=[p for p in pnls if p>0]; losses=[p for p in pnls if p<=0]
    grosswin=sum(wins); grossloss=-sum(losses); equity=0; peak=0; maxdd=0
    for p in pnls: equity+=p;peak=max(peak,equity);maxdd=max(maxdd,peak-equity)
    return {'trades':len(pnls),'win_rate':sum(p>0 for p in pnls)/len(pnls) if pnls else 0,'avg_pnl_usd':statistics.mean(pnls) if pnls else 0,'expectancy_usd':statistics.mean(pnls) if pnls else 0,'profit_factor':grosswin/grossloss if grossloss else None,'max_drawdown_usd':maxdd,'worst_trade_usd':min(pnls) if pnls else 0,'max_reserve_used_usd':max((t.max_reserve_used_usd for t in trades),default=0),'risk_exits':sum(t.exit_reason=='account_loss_guard' for t in trades),'tp_exits':sum(t.exit_reason=='take_profit' for t in trades)}
def monte_carlo(trades:list[Trade],runs:int=10000,seed:int=20260811)->dict:
    rng=random.Random(seed);pnls=[t.pnl_usd for t in trades]
    if not pnls:return {'runs':runs,'note':'no trades'}
    endings=[]; dd25=dd50=ruin=0
    for _ in range(runs):
      seq=[rng.choice(pnls) for _ in pnls]; eq=30000/85;peak=eq;dd=0
      for p in seq: eq+=p;peak=max(peak,eq);dd=max(dd,(peak-eq)/peak if peak else 1)
      endings.append(eq);dd25+=dd>=.25;dd50+=dd>=.5;ruin+=eq<=0
    endings.sort();return {'runs':runs,'ending_usd_mean':statistics.mean(endings),'ending_usd_p05':endings[int(.05*(runs-1))],'ending_usd_p01':endings[int(.01*(runs-1))],'p_drawdown_25':dd25/runs,'p_drawdown_50':dd50/runs,'p_ruin':ruin/runs}

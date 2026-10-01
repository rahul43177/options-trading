"""Model-only Black-Scholes utilities. These are never presented as Delta quotes."""
from __future__ import annotations
from math import erf, exp, log, pi, sqrt
def norm_cdf(x:float)->float:return .5*(1+erf(x/sqrt(2)))
def norm_pdf(x:float)->float:return exp(-.5*x*x)/sqrt(2*pi)
def d1(spot:float,strike:float,t:float,vol:float,r:float=0)->float:return (log(spot/strike)+(r+.5*vol*vol)*t)/(vol*sqrt(t))
def price(spot:float,strike:float,t:float,vol:float,kind:str,r:float=0)->float:
    if t<=0:return max(0.0,(spot-strike) if kind=='call' else (strike-spot))
    x=d1(spot,strike,t,vol,r); y=x-vol*sqrt(t)
    return spot*norm_cdf(x)-strike*exp(-r*t)*norm_cdf(y) if kind=='call' else strike*exp(-r*t)*norm_cdf(-y)-spot*norm_cdf(-x)
def delta(spot:float,strike:float,t:float,vol:float,kind:str)->float:
    if t<=0:return (1. if spot>strike else 0.) if kind=='call' else (-1. if spot<strike else 0.)
    v=norm_cdf(d1(spot,strike,t,vol)); return v if kind=='call' else v-1
def strike_for_delta(spot:float,t:float,vol:float,target_abs_delta:float,kind:str)->float:
    # deterministic grid search avoids an inverse-normal dependency.
    lo,hi=(spot,spot*4) if kind=='call' else (spot*.05,spot)
    for _ in range(80):
        mid=(lo+hi)/2; current=abs(delta(spot,mid,t,vol,kind))
        if (kind=='call' and current>target_abs_delta) or (kind=='put' and current<target_abs_delta): lo=mid
        else: hi=mid
    return (lo+hi)/2

def implied_vol(target:float,spot:float,strike:float,t:float,kind:str='call',lo:float=0.01,hi:float=5.0)->float|None:
    """Bisection IV from a premium — fallback when the chain has no mark_iv."""
    if t<=0 or not target or target<=0: return None
    for _ in range(100):
        mid=(lo+hi)/2
        if price(spot,strike,t,mid,kind)>target: hi=mid
        else: lo=mid
    return (lo+hi)/2

def _reprice(zone:float,strike:float,remaining_hours:float,vol:float,kind:str)->float:
    return price(zone,strike,max(remaining_hours,0.25)/(24*365),vol,kind)

def project_premium(strike:float,kind:str,iv:float,hours_to_expiry:float,zone:float,
                    hours_to_zone:float,iv_bump:float=0.15,slow:float=1.5,fast:float=0.5)->dict:
    """Premium of an option WHEN spot reaches `zone`, as a BAND, via Black-Scholes reprice.

    Replaces the old `ask + delta*ds + 0.5*gamma*ds^2` snapshot, which froze both time and
    IV. Here we reprice at spot=zone under three scenarios so a seller can rest a limit with
    confidence instead of a single false-precision number:

      floor  — slow arrival (theta eats more of the clock), IV flat  -> LOWEST at the zone.
               Rest your SELL limit here: it still fills even on a grind into the level.
      base   — expected arrival (ATR-derived hours_to_zone), IV flat.
      ceiling— fast arrival + IV +iv_bump (vol pump on a quick approach) -> the upside.

    Times are in HOURS; `iv` is the option's own mark_iv as a decimal (e.g. 0.37).
    """
    hz=max(0.0,min(hours_to_zone,hours_to_expiry))
    floor  =_reprice(zone,strike,hours_to_expiry-min(hz*slow,hours_to_expiry*0.98),iv,kind)
    base   =_reprice(zone,strike,hours_to_expiry-hz,iv,kind)
    ceiling=_reprice(zone,strike,hours_to_expiry-hz*fast,iv*(1+iv_bump),kind)
    return {"floor":floor,"base":base,"ceiling":ceiling,"rest_here":floor,"hours_to_zone":hz}

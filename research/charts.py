"""Dependency-free SVG charts for the model run; labels preserve evidence level."""
from __future__ import annotations
import json
from pathlib import Path
from .config import ROOT

def svg_path(values:list[float], width:int=900,height:int=360)->str:
    lo,hi=min(values),max(values); span=hi-lo or 1; pts=[]
    for i,v in enumerate(values): pts.append(f"{45+i*(width-65)/max(1,len(values)-1):.1f},{height-35-(v-lo)*(height-65)/span:.1f}")
    return ' '.join(pts)
def wrap(title:str,body:str)->str:
 return f'''<svg xmlns="http://www.w3.org/2000/svg" width="900" height="360" viewBox="0 0 900 360"><style>text{{font-family:Arial,sans-serif;fill:#222}}.s{{font-size:12px;fill:#555}}</style><rect width="100%" height="100%" fill="white"/><text x="45" y="25" font-size="17">{title}</text><text class="s" x="45" y="345">LEVEL 1: real Delta BTC candles + synthetic option model — not Delta historical options P&amp;L</text>{body}</svg>'''
def main()->None:
 runs=sorted((ROOT/'backtests/runs').glob('*/trades.json')); trades=json.loads(runs[-1].read_text()); out=ROOT/'charts';out.mkdir(exist_ok=True)
 best=[t for t in trades if t['dte']==7 and t['delta']==.3 and t['tp']==.7]; equity=[];x=0
 for t in best:x+=t['pnl_usd'];equity.append(x)
 p=svg_path(equity); out.joinpath('model_best_equity_curve.svg').write_text(wrap('Model equity curve: 7 DTE / 30Δ / 70% TP',f'<line x1="45" y1="45" x2="45" y2="325" stroke="#999"/><line x1="45" y1="325" x2="880" y2="325" stroke="#999"/><polyline fill="none" stroke="#1677aa" stroke-width="2" points="{p}"/>'))
 ranked=json.loads((runs[-1].parent/'summary.json').read_text())['ranked_model_configurations']; vals=[x['expectancy_usd'] for x in ranked]; mn=min(vals); mx=max(vals); bars=[]
 for i,v in enumerate(vals):
  h=abs(v)/(max(abs(mn),abs(mx)) or 1)*230;y=175-h if v>=0 else 175;bars.append(f'<rect x="{50+i*10}" y="{y}" width="7" height="{h}" fill="{"#1677aa" if v>=0 else "#b44"}"/>')
 out.joinpath('model_configuration_expectancy.svg').write_text(wrap('Model expectancy across 75 configurations',f'<line x1="45" y1="175" x2="880" y2="175" stroke="#999"/>{"".join(bars)}'))
if __name__=='__main__':main()

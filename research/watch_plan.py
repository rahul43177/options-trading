"""
Read-only BTC-price-anchored watch plan.

For a resistance (call-short) and a support (put-short) zone, prints the single best liquid
candidate per side and the premium BAND it should be worth WHEN BTC reaches the level, keyed
on the BTC SPOT PRICE you watch on your chart: "when BTC reaches X -> rest a SELL limit at Y".

The discipline this encodes (learned the hard way): do NOT market-short the call at today's
ask while BTC is still below the zone — that sells too cheap and rides the premium up as BTC
climbs in. Instead rest a maker SELL limit at the band's floor; it only fills once BTC tags
the level, and if BTC then rejects (your thesis) you are short from a good price.

Data only. You still need your own rejection/reversal confirmation, you never fade the
higher-timeframe trend, and you place every limit yourself.

    python -m research.watch_plan --resistance 86000 --support 83000 --min-oi 10 --max-hours 72
"""
from __future__ import annotations
import argparse, datetime as dt
from .entry_scan import load_options, band_for, _iv_of
from .perp_analytics import underlying_atr_1h
from .config import UNDERLYINGS, DEFAULT_BUFFER
from . import proj_log


def best(opts, side, zone, buf, dmin, dmax, max_h, min_oi, spot, atr_1h, k, iv_bump):
    now = dt.datetime.now(dt.timezone.utc)
    cands = []
    for o in opts:
        if o["type"] != side or None in (o["bid"], o["ask"], o["delta"], o["strike"]):
            continue
        h = (o["expiry"] - now).total_seconds() / 3600 if o["expiry"] else None
        if h is None or not (0 < h <= max_h):
            continue
        if side == "CALL" and o["strike"] < zone + buf:
            continue
        if side == "PUT" and o["strike"] > zone - buf:
            continue
        if not (dmin <= abs(o["delta"]) <= dmax) or o["oi"] < min_oi:
            continue
        o["dte_h"] = h
        o["band"] = band_for(o, zone, atr_1h, k, iv_bump)
        o["spr_pct"] = o["spread"] / o["mid"] * 100 if o["mid"] else 0.0
        cands.append(o)
    cands.sort(key=lambda x: abs(abs(x["delta"]) - 0.16))
    return cands[0] if cands else None


def _leg(arrow, verb, btc_level, pct, sign, side, confirm, o, asset="BTC"):
    print(f"\n  {arrow} WHEN {asset} {verb} {btc_level:,.0f}  ({sign}{pct:.1f}% from spot)  "
          f"AND {confirm} confirms:")
    if not o:
        print(f"       -> no liquid {side} strike in your band right now (skip / widen filters)")
        return
    b = o.get("band")
    print(f"       -> SHORT {side}   {o['symbol']}   |delta| {abs(o['delta']):.2f} | "
          f"OI {o['oi']:.0f} | spread {o['spr_pct']:.1f}%"
          + ("  [THIN OI — hard to exit]" if o["oi"] < 15 else ""))
    if b:
        print(f"          REST a maker SELL limit ~{b['floor']:.0f}   (fills even on a slow grind into the zone)")
        print(f"          expected at zone ~{b['base']:.0f}   ·   up to ~{b['ceiling']:.0f} if {asset} spikes in fast")
    print(f"          (current ask is only ~{o['ask']:.0f} — selling there NOW, before the zone, "
          f"sells too cheap and rides the move up)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--resistance", type=float, required=True, help="call-short zone (BTC price)")
    ap.add_argument("--support", type=float, required=True, help="put-short zone (BTC price)")
    ap.add_argument("--buffer", type=float, default=None, help="min $ OTM past the zone (default: per-asset)")
    ap.add_argument("--dmin", type=float, default=0.10)
    ap.add_argument("--dmax", type=float, default=0.22)
    ap.add_argument("--max-hours", type=float, default=48.0)
    ap.add_argument("--min-oi", type=float, default=10.0)
    ap.add_argument("--asset", choices=sorted(UNDERLYINGS), default="BTC", help="underlying (default BTC)")
    ap.add_argument("--atr-k", type=float, default=0.5, help="net progress per hour = k x ATR(1h)")
    ap.add_argument("--iv-bump", type=float, default=0.15, help="IV expansion for the band ceiling")
    ap.add_argument("--no-log", action="store_true", help="don't append the plan to proj_log")
    a = ap.parse_args()
    if a.buffer is None:
        a.buffer = DEFAULT_BUFFER[a.asset]

    opts = load_options(a.asset)
    spot = next((o["spot"] for o in opts if o["spot"]), None)
    atr_1h = underlying_atr_1h(UNDERLYINGS[a.asset])
    up = (a.resistance - spot) / spot * 100
    dn = (spot - a.support) / spot * 100

    print("=" * 84)
    print(f"  {a.asset} WATCH PLAN   |   spot now {spot:,.0f}   |   read-only, you confirm + place the limit")
    atr_txt = f"ATR(1h) {atr_1h:,.0f} -> theta-aware band" if atr_1h else "ATR unavailable (instantaneous band)"
    print(f"  {atr_txt}   |   rest@floor = slow arrival / flat IV; ceiling = fast + IV +{a.iv_bump*100:.0f}%")
    print("=" * 84)

    c = best(opts, "CALL", a.resistance, a.buffer, a.dmin, a.dmax, a.max_hours, a.min_oi, spot, atr_1h, a.atr_k, a.iv_bump)
    _leg("^", "rises to", a.resistance, up, "+", "CALL", "a bearish rejection", c, a.asset)

    p = best(opts, "PUT", a.support, a.buffer, a.dmin, a.dmax, a.max_hours, a.min_oi, spot, atr_1h, a.atr_k, a.iv_bump)
    _leg("v", "falls to", a.support, dn, "-", "PUT", "a bullish reversal", p, a.asset)

    if not a.no_log:
        for o, zone, side in ((c, a.resistance, "call"), (p, a.support, "put")):
            if o and o.get("band"):
                proj_log.log_projection(o["symbol"], o["strike"], side, spot, zone,
                                        _iv_of(o), o["dte_h"], o["band"], atr_1h, a.atr_k, a.iv_bump)

    print("\n  Rest the maker SELL at the FLOOR of the band — it fills even if price grinds in slowly,")
    print("  and you pocket more if it spikes. If price never reaches the zone the limit never fills")
    print("  (that's fine). Band assumes theta over the ATR-estimated travel time; a vol spike lifts")
    print("  it, a stall lowers it. Don't fade the higher-timeframe trend. Not advice — you trade.\n")


if __name__ == "__main__":
    main()

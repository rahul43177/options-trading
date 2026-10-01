"""
Independent verification of a proposed perp short — DO NOT trust a single feed.

The screen and the trade-card read one live snapshot; TradingView reads another. This
module is the third opinion: it re-pulls the Delta ticker AND raw candles, recomputes
ATR / RSI / swing structure from scratch, checks the geometry and reward:risk, and —
critically — reports how STALE each feed is and whether they agree. If the candle feed
lags the ticker (as it can in a delayed/sandboxed environment), it says so loudly, so a
level is never trusted just because a script printed it. It also recomputes lots,
leverage and the liquidation price independently so the sizing is pure math you can audit.

    python -m research.futures_verify --coin DOGE --entry 0.0958 --tp 0.0905 --sl 0.0972 \
        --equity 353 --risk 1

Read-only, public data only. Never trades. Not advice.
"""
from __future__ import annotations
import argparse
import datetime as dt
import time

from .delta_api import DeltaPublicClient


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _ema(vals, n):
    k = 2 / (n + 1)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def _rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    g = l = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        g += max(d, 0)
        l += max(-d, 0)
    ag, al = g / n, l / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def _atr(bars, n=14):
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < n:
        return None
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def _swings(bars, w, kind):
    out = []
    for i in range(w, len(bars) - w):
        v = bars[i][kind]
        if kind == "high" and all(v >= bars[j]["high"] for j in range(i - w, i + w + 1) if j != i):
            out.append(round(v, 8))
        if kind == "low" and all(v <= bars[j]["low"] for j in range(i - w, i + w + 1) if j != i):
            out.append(round(v, 8))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--side", choices=["short", "long"], default="short",
                    help="short (sell resistance) or long (buy support)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--symbol")
    g.add_argument("--coin")
    ap.add_argument("--entry", type=float, required=True)
    ap.add_argument("--tp", type=float, required=True)
    ap.add_argument("--sl", type=float, required=True)
    ap.add_argument("--equity", type=float, default=353.0, help="account equity in the perp's quote ccy (USD)")
    ap.add_argument("--risk", type=float, default=1.0, help="percent of equity to risk")
    ap.add_argument("--max-stale-h", type=float, default=6.0, help="warn if a feed lags more than this")
    a = ap.parse_args()

    sym = a.symbol or f"{a.coin.upper()}USD"
    c = DeltaPublicClient()
    t = next((x for x in c.get("/tickers", {"contract_types": "perpetual_futures"})["result"]
              if str(x.get("symbol", "")).upper() == sym.upper()), None)
    if not t:
        print(f"  No ticker for {sym}."); return
    mark = _f(t.get("mark_price")); hi = _f(t.get("mark_high_24h")); lo = _f(t.get("mark_low_24h"))
    fund = _f(t.get("funding_rate")) or 0.0
    cv = _f(t.get("contract_value")) or 1.0
    maxlev = _f(t.get("leverage")) or 100.0
    now = int(time.time())

    # Bounded, RECENT windows: the /history/candles endpoint caps rows (~240) and returns the
    # EARLIEST bars in an over-wide range, so keep each request under the cap and ending at now,
    # then sort ascending so [-1] is the true newest bar (not a stale one).
    b1 = c.candles(sym, "1h", now - 6 * 86400, now)      # ~144 bars
    b4 = c.candles(sym, "4h", now - 20 * 86400, now)     # ~120 bars
    for b in (b1, b4):
        b.sort(key=lambda r: r["time"])
        for r in b:
            for k in ("open", "high", "low", "close"):
                r[k] = _f(r[k])
    c1 = [x["close"] for x in b1]
    c4 = [x["close"] for x in b4]
    atr1, atr4 = _atr(b1), _atr(b4)
    rsi1, rsi4 = _rsi(c1), _rsi(c4)
    e20, e50 = _ema(c4[-60:], 20), _ema(c4[-60:], 50)
    highs, lows = _swings(b4[-40:], 2, "high")[-3:], _swings(b4[-40:], 2, "low")[-3:]
    rec_hi = max(x["high"] for x in b4[-30:])
    last_ct = b1[-1]["time"]
    lag_h = (now - last_ct) / 3600
    cndl_px = b1[-1]["close"]
    px_gap = abs(cndl_px - mark) / mark * 100 if mark else None

    is_long = a.side == "long"
    label = "LONG" if is_long else "SHORT"
    entry, tp, sl = a.entry, a.tp, a.sl
    if is_long:
        stopf = (entry - sl) / entry               # stop sits BELOW entry
        rr = (tp - entry) / (entry - sl) if entry > sl else None
    else:
        stopf = (sl - entry) / entry               # stop sits ABOVE entry
        rr = (entry - tp) / (sl - entry) if sl > entry else None

    print("=" * 88)
    print(f"  INDEPENDENT VERIFY  {sym}  [{label}]   entry {entry:g} / TP {tp:g} / SL {sl:g}")
    print("=" * 88)
    print("  DATA FRESHNESS  (ticker 'time' field lags ~24h and is ignored; price is the check)")
    print(f"    server now   {dt.datetime.fromtimestamp(now, dt.timezone.utc):%Y-%m-%d %H:%M}Z")
    print(f"    newest 1h bar {dt.datetime.fromtimestamp(last_ct, dt.timezone.utc):%Y-%m-%d %H:%M}Z  "
          f"(lags {lag_h:.1f}h)   close {cndl_px:g}")
    print(f"    ticker mark  {mark:g}   (candle-vs-mark gap {px_gap:.2f}%)")
    if lag_h > a.max_stale_h:
        print(f"    ! CANDLES STALE (>{a.max_stale_h:.0f}h) — re-pull before trusting indicators.")
    elif px_gap and px_gap > 2:
        print(f"    ! candle/mark price gap {px_gap:.1f}% — feeds disagree, re-pull before real entry.")
    else:
        print("    OK — feeds fresh and agree; indicators below are current.")

    print("  INDEPENDENT INDICATORS (from candles — see freshness note)")
    print(f"    ATR 1h {atr1:.6g} ({atr1 / entry * 100:.2f}% of entry) | 4h {atr4:.6g} ({atr4 / entry * 100:.2f}%)")
    print(f"    RSI14 1h {rsi1:.1f} | 4h {rsi4:.1f}    4h EMA20 {'>' if e20 > e50 else '<'} EMA50 -> {'UP' if e20 > e50 else 'DOWN'}")
    print(f"    last swing highs {highs}   swing lows {lows}")

    print("  CHECKS")
    if is_long:
        geo_ok = sl < entry < tp
        geo_desc = "tp>entry>sl"
        stop_dist = entry - sl
    else:
        geo_ok = tp < entry < sl
        geo_desc = "tp<entry<sl"
        stop_dist = sl - entry
    mark_note = f"entry {(entry-mark)/mark*100:+.2f}% vs mark ({'resting' if ((is_long and entry<mark) or (not is_long and entry>mark)) else 'ARMED/through'})"
    print(f"    geometry {geo_desc} : {'PASS' if geo_ok else 'FAIL'}   ({mark_note})")
    svs = stop_dist / atr1 if atr1 else 0
    print(f"    stop vs 1h ATR : {svs:.2f}x  {'PASS' if svs >= 1 else 'TOO TIGHT (stale ATR)'}")
    print(f"    reward:risk    : {rr:.2f}:1  {'PASS' if rr and rr >= 2 else 'THIN'}")

    risk_usd = a.equity * a.risk / 100
    notional = risk_usd / stopf
    lot_usd = cv * entry
    lots = max(1, round(notional / lot_usd))
    act_notional = lots * lot_usd
    act_risk = act_notional * stopf
    safe_lev = min(maxlev, int(max(1, 1 / (2 * stopf))))
    liq_sign = "-" if is_long else "+"
    carry = act_notional * fund * 3 / 100 * (-1 if is_long else 1)  # + = credited to your side
    print("  SIZING (independent math)")
    print(f"    {lots} lots (1 lot = {cv:g} {sym.replace('USD','')})  notional ~{act_notional:,.0f}  "
          f"stop-out ~{act_risk:,.2f}  ({a.risk:.1f}% of {a.equity:,.0f})")
    print(f"    leverage <= {safe_lev}x (max {maxlev:g}x)  |  liq ~{liq_sign}{100/safe_lev:.1f}% vs stop {liq_sign}{stopf*100:.2f}%")
    print(f"    funding {fund:+.3f}%/8h -> ~{carry:+,.2f}/day to the {a.side}")
    print("  Read-only, public data. Not advice. Re-pull fresh before any real order.\n")


if __name__ == "__main__":
    main()

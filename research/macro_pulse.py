"""
Read-only crypto MACRO / REGIME pulse — no CoinGecko, no key (Delta India public API).

The TradingView `bitcoin_market_pulse` tool depends on CoinGecko and frequently 403s.
This is the fallback the futures-entry / options-entry skills call for: it frames the
regime straight from Delta Exchange India's public /tickers and /history/candles —
the same feed the screener and trade-cards use — so an alt long/short read always has
a BTC anchor even when CoinGecko is down.

What it reports (all live, public, read-only):
  BTC & ETH perp   mark, 24h %, funding, basis (perp vs spot), OI$, turnover$, and
                   position in the 24h range (rngPos ~1 = at the high, ~0 at the low).
  BTC trend        1D and 4h EMA20-vs-EMA50, RSI(14), ATR% — from raw candles.
  Alt lean         ETH 24h % minus BTC 24h % (ETH leading = risk-on for alts; BTC
                   leading / both red = alt caution) as a dominance proxy.
  Regime label     a plain composite you can act on: RISK_OFF / ALT_CAUTION /
                   NEUTRAL / ALT_FAVORABLE.

Note: true BTC.D dominance and total market cap are NOT on the Delta feed. For those,
use TradingView symbols CRYPTOCAP:BTC.D and CRYPTOCAP:TOTAL. This pulse uses BTC's own
trend plus BTC-vs-ETH relative strength as the on-exchange proxy for the same question.

    python -m research.macro_pulse
    python -m research.macro_pulse --coins BTC,ETH,SOL
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


def _clip01(x):
    return 0.0 if x < 0 else (1.0 if x > 1 else x)


def snapshot(client: DeltaPublicClient, coins: list[str]) -> dict[str, dict]:
    want = {f"{c.upper()}USD" for c in coins}
    out: dict[str, dict] = {}
    for t in client.get("/tickers", {"contract_types": "perpetual_futures"})["result"]:
        sym = str(t.get("symbol", "")).upper()
        if sym not in want:
            continue
        mark = _f(t.get("mark_price"))
        spot = _f(t.get("spot_price"))
        hi, lo = _f(t.get("mark_high_24h")), _f(t.get("mark_low_24h"))
        out[sym] = {
            "symbol": sym,
            "mark": mark, "spot": spot, "hi": hi, "lo": lo,
            "chg24": _f(t.get("mark_change_24h")) or 0.0,
            "funding": _f(t.get("funding_rate")) or 0.0,
            "basis_pct": ((mark - spot) / spot * 100) if (spot and spot > 0) else 0.0,
            "rng_pos": _clip01((mark - lo) / (hi - lo)) if (hi and lo and hi > lo) else None,
            "oi_usd": _f(t.get("oi_value_usd")) or 0.0,
            "turnover_usd": _f(t.get("turnover_usd")) or 0.0,
        }
    return out


def trend(client: DeltaPublicClient, symbol: str) -> dict:
    now = int(time.time())
    b1d = client.candles(symbol, "1d", now - 120 * 86400, now)
    b4 = client.candles(symbol, "4h", now - 20 * 86400, now)
    res = {}
    for tag, bars in (("1D", b1d), ("4h", b4)):
        bars.sort(key=lambda r: r["time"])
        for r in bars:
            for k in ("open", "high", "low", "close"):
                r[k] = _f(r[k])
        closes = [x["close"] for x in bars]
        if len(closes) < 55:
            res[tag] = None
            continue
        e20, e50 = _ema(closes[-60:], 20), _ema(closes[-60:], 50)
        atr = _atr(bars)
        res[tag] = {
            "ema20": e20, "ema50": e50,
            "dir": "UP" if e20 > e50 else "DOWN",
            "rsi": _rsi(closes),
            "atr_pct": (atr / closes[-1] * 100) if atr and closes[-1] else None,
            "close": closes[-1],
        }
    return res


def regime(btc: dict, alt_lean: float, btc_tr: dict) -> tuple[str, str]:
    d1 = btc_tr.get("1D") or {}
    h4 = btc_tr.get("4h") or {}
    up_1d = d1.get("dir") == "UP"
    up_4h = h4.get("dir") == "UP"
    chg = btc["chg24"]
    if chg <= -3 or (not up_1d and not up_4h):
        lab = "RISK_OFF"
        why = "BTC heavy / rolling over — alt shorts favoured, alt longs fight the tape."
    elif up_1d and up_4h and chg >= 1 and alt_lean > 0:
        lab = "ALT_FAVORABLE"
        why = "BTC firm and ETH leading it — alt longs have the wind; alt shorts are counter-trend."
    elif up_1d and not up_4h:
        lab = "ALT_CAUTION"
        why = "BTC up on the daily but stalling on 4h — mixed; wait for the 4h to pick a side."
    else:
        lab = "NEUTRAL"
        why = "No decisive BTC regime — trade the setup, keep size modest."
    return lab, why


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", default="BTC,ETH",
                    help="comma list; first is the anchor (default BTC,ETH)")
    a = ap.parse_args()
    coins = [c.strip().upper() for c in a.coins.split(",") if c.strip()]
    c = DeltaPublicClient()
    now = dt.datetime.now(dt.timezone.utc)
    snap = snapshot(c, coins)
    btc = snap.get("BTCUSD")
    eth = snap.get("ETHUSD")
    btc_tr = trend(c, "BTCUSD") if btc else {}

    print("=" * 92)
    print("  DELTA INDIA — READ-ONLY MACRO / REGIME PULSE   (public API, no CoinGecko, no key)")
    print(f"  {now:%Y-%m-%d %H:%M UTC}")
    print("=" * 92)

    hdr = (f"  {'coin':<6} {'mark':>12} {'chg24%':>7} {'rngPos':>6} {'fund%':>7} "
           f"{'basis%':>7} {'OI$':>8} {'turn$':>9}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for sym in (f"{x}USD" for x in coins):
        r = snap.get(sym)
        if not r:
            print(f"  {sym.replace('USD',''):<6}  (no live perp ticker)")
            continue
        rp = f"{r['rng_pos']:>6.2f}" if r["rng_pos"] is not None else f"{'-':>6}"
        print(f"  {sym.replace('USD',''):<6} {r['mark']:>12,.6g} {r['chg24']:>7.2f} {rp} "
              f"{r['funding']:>7.4f} {r['basis_pct']:>7.3f} {r['oi_usd']/1e6:>7.1f}M "
              f"{r['turnover_usd']/1e6:>8.1f}M")

    if btc and btc_tr:
        print("\n  BTC TREND (from candles)")
        for tag in ("1D", "4h"):
            tr = btc_tr.get(tag)
            if not tr:
                print(f"    {tag:<3} (insufficient candles)")
                continue
            print(f"    {tag:<3} EMA20 {'>' if tr['ema20']>tr['ema50'] else '<'} EMA50 -> {tr['dir']:<4} "
                  f"| RSI14 {tr['rsi']:.1f} | ATR {tr['atr_pct']:.2f}%")

    alt_lean = (eth["chg24"] - btc["chg24"]) if (eth and btc) else 0.0
    if eth and btc:
        lean = "ETH leading BTC (risk-on for alts)" if alt_lean > 0 else \
               ("BTC leading ETH (alt caution)" if alt_lean < 0 else "flat")
        print(f"\n  ALT LEAN (dominance proxy)  ETH 24h {eth['chg24']:+.2f}% - BTC 24h {btc['chg24']:+.2f}% "
              f"= {alt_lean:+.2f}%  -> {lean}")

    if btc:
        lab, why = regime(btc, alt_lean, btc_tr)
        print(f"\n  REGIME: {lab}")
        print(f"    {why}")
        crowd = "longs pay shorts (crowded longs)" if btc["funding"] > 0 else \
                ("shorts pay longs (crowded shorts)" if btc["funding"] < 0 else "funding flat")
        print(f"    BTC funding {btc['funding']:+.4f}%/8h -> {crowd}.")

    print("\n  Note: true BTC.D dominance & total mcap are not on the Delta feed — use TradingView")
    print("  CRYPTOCAP:BTC.D / CRYPTOCAP:TOTAL for those. This is the on-exchange proxy.")
    print("  Read-only, public data only. Never trades. Not advice.\n")


if __name__ == "__main__":
    main()

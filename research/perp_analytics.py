"""
Candle-derived analytics shared by the perp scanner — the "structure" layer.

The base screen (futures_screen) ranks the whole universe from ONE cheap ticker call:
range position, 24h change, funding, OI growth, basis. That tells you a coin is
*stretched* — it cannot tell you whether that stretch is a trend-aligned pullback you
want to trade or a counter-trend knife you want to avoid. This module adds that read
from public candles: the higher-timeframe (Daily-led) trend, 4h reversal-underway,
ATR%, RSI, a plain-English SETUP label, and a trend-ALIGNMENT verdict that a pro desk
would gate on ("never fight the higher-timeframe trend").

It is read-only and uses only Delta's public /history/candles. Import `analyze()` to
get the facts for one symbol and `classify()` to turn ticker-facts + those candle-facts
into (setup, aligned, score-multiplier, state).
"""
from __future__ import annotations
import time

from .delta_api import DeltaPublicClient


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def ema(vals, n):
    if not vals:
        return None
    k = 2 / (n + 1)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes, n=14):
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


def atr(bars, n=14):
    trs = []
    for i in range(1, len(bars)):
        h, lo, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    if len(trs) < n:
        return None
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def _prep(bars):
    bars.sort(key=lambda r: r["time"])
    for r in bars:
        for k in ("open", "high", "low", "close"):
            r[k] = _f(r[k])
    return bars


def _trend(bars):
    closes = [b["close"] for b in bars]
    if len(closes) < 55:
        return None
    e20, e50 = ema(closes[-60:], 20), ema(closes[-60:], 50)
    px = closes[-1]
    if e20 > e50 and px >= e50:
        d = "UP"
    elif e20 < e50 and px <= e50:
        d = "DOWN"
    else:
        d = "FLAT"
    return {"dir": d, "ema20": e20, "ema50": e50, "price": px}


ARROW = {"UP": "↑", "DOWN": "↓", "FLAT": "→"}


def analyze(symbol, client=None):
    """Pull 4h + 1d candles and return the higher-timeframe structure facts.

    Returns {'ok': False} if the candle history is too short or the pull fails, so the
    caller can fall back to the ticker-only score without crashing.
    """
    client = client or DeltaPublicClient()
    now = int(time.time())
    try:
        b4 = _prep(client.candles(symbol, "4h", now - 30 * 86400, now))
        b1d = _prep(client.candles(symbol, "1d", now - 220 * 86400, now))
    except Exception:
        return {"ok": False}
    t4, t1d = _trend(b4), _trend(b1d)
    if not t4 or not t1d:
        return {"ok": False}
    closes4 = [b["close"] for b in b4]
    a4, a1d = atr(b4), atr(b1d)
    px = t4["price"]
    return {
        "ok": True,
        "htf": t1d["dir"],            # Daily-led higher-timeframe trend (the no-fade anchor)
        "t1d": t1d["dir"],
        "t4": t4["dir"],
        "px": px,
        "atr_pct_4h": (a4 / px * 100) if (a4 and px) else None,
        "atr_pct_1d": (a1d / px * 100) if (a1d and px) else None,
        "rsi_4h": rsi(closes4),
        "below_4h_ema20": px < t4["ema20"],   # short: reversal underway
        "above_4h_ema20": px > t4["ema20"],   # long: turning back up
    }


def classify(rng_pos, an, side):
    """Turn range-position + candle structure into a desk read.

    Returns {setup, aligned, mult, state}:
      setup  — plain label (trend-short / pullback-long / knife-long / fade-strength / ...)
      aligned— True (with HTF trend), False (fighting it), or None (unknown/no candles)
      mult   — score multiplier: 1.15 aligned, 0.70 counter-trend, 1.0 unknown
      state  — WATCH (aligned + at the level) / ARMED (aligned, approaching) / AVOID (counter) / —
    """
    if not an.get("ok"):
        return {"setup": "?", "aligned": None, "mult": 1.0, "state": "—"}
    htf = an["htf"]
    near_high = rng_pos >= 0.6
    near_low = rng_pos <= 0.4

    if side == "short":
        rolling_down = an["below_4h_ema20"]
        if htf == "DOWN":
            aligned = True
            setup = "trend-short" if rolling_down else "bounce-short"
        elif htf == "FLAT":
            aligned = near_high
            setup = "range-short" if near_high else "mid"
        else:  # HTF UP — shorting into an uptrend is the loophole
            aligned = False
            setup = "top-fade" if rolling_down else "fade-strength"
    else:  # long
        rolling_up = an["above_4h_ema20"]
        if htf == "UP":
            aligned = True
            setup = "trend-long" if rolling_up else "pullback-long"
        elif htf == "FLAT":
            aligned = near_low
            setup = "range-long" if near_low else "mid"
        else:  # HTF DOWN — buying a downtrend is catching a knife
            aligned = False
            setup = "bounce-long" if rolling_up else "knife-long"

    mult = 1.15 if aligned else (0.70 if aligned is False else 1.0)
    at_level = (side == "short" and near_high) or (side == "long" and near_low)
    if aligned is False:
        state = "AVOID"
    elif aligned and at_level:
        state = "WATCH"
    elif aligned:
        state = "ARMED"
    else:
        state = "—"
    return {"setup": setup, "aligned": aligned, "mult": mult, "state": state}


def underlying_atr_1h(symbol="BTCUSD", client=None, days=10):
    """ATR of an underlying on 1h candles (price units per hour), or None.

    Public-candle only — keeps the option scanner self-contained (no TradingView, no keys).
    Feeds the time-to-zone estimate so the premium projection can charge theta for the hours
    the underlying actually takes to travel into a watched level. `symbol` is the spot candle
    symbol (e.g. BTCUSD, ETHUSD — see config.UNDERLYINGS).
    """
    client = client or DeltaPublicClient()
    now = int(time.time())
    try:
        bars = _prep(client.candles(symbol, "1h", now - days * 86400, now))
    except Exception:
        return None
    return atr(bars) if bars else None


def btc_atr_1h(client=None, symbol="BTCUSD", days=10):
    """Back-compat BTC shim — unchanged behaviour. Prefer underlying_atr_1h(symbol)."""
    return underlying_atr_1h(symbol, client, days)


def hours_to_zone(spot, zone, atr_1h, k=0.5):
    """Estimated hours for spot to travel to `zone` at k x ATR(1h) of NET progress per hour.

    k < 1 because directional travel is choppier and slower than the full hourly range: BTC
    rarely moves a clean ATR toward one target every hour. k is exactly what the calibration
    loop (proj_log.calibrate) tunes from realized fills. Returns None if ATR is unknown, and
    the caller then falls back to an instantaneous (0h) projection.
    """
    if not atr_1h or atr_1h <= 0 or spot is None:
        return None
    return abs(zone - spot) / (atr_1h * k)

"""
Direction score — "which side do I sell: puts, calls, or neither?" — plus an honest backtest of it.
READ-ONLY research: public Delta candles in, statistics out. No orders, no keys.

The same rules drive the TradingView indicator (pine/options_short_ladder_auto.pine), so this module
is its specification AND its measured track record. v1 rules were fixed BEFORE testing; the only two
changes since were chosen on the DESIGN half (Aug-2024 → Sep-2025) and confirmed on the untouched TEST
half (Sep-2025 → Oct-2026) — see "Measured" below.

  vote +1 = bullish → sell PUTS (put side)      vote -1 = bearish → sell CALLS (call side)

  factor        what it reads                                                     weight
  weekly        weekly close above/below BOTH EMA20 and EMA50 (last closed week)     2
  daily         same on the daily (last closed day)                                  2
  h4            same on 4h                                                           1
  h1            same on 1h                                                           1
  structure     last confirmed 4h swing break (close through a 3/3 pivot high/low)   1
  vprofile      price above/below the 30-day volume-profile POC (1h bars)            1
  flow          24h candle-delta: sum(volume x (close-open)/(high-low)) sign         1
  location      4h Bollinger %B >= 0.9 -> call side, <= 0.1 -> put side   INFO ONLY (weight 0)

  score in [-9, +9];  PUT side if score >= +3 AND weekly >= 0 AND daily >= 0;
                      CALL side if score <= -3 AND weekly <= 0 AND daily <= 0;  else STAND ASIDE.

Measured (Delta BTCUSD 1h, 2024-08-05 → 2026-10-02, 18,913 hourly decisions):
  * Selling a 1-EM strike blind is OK ~87% of the time on EITHER side — distance does most of the work.
  * Chosen side OK 88.9% vs the wrong side 84.6% → the wrong side has ~1.4x the losers. Design half
    +3.5 pts, TEST half +5.1 pts (89.3% vs 84.2%). Signal fires ~55% of the time; else stand aside.
  * PUT signals are the robust part (+6 / +4 pts in both halves). CALL signals were WRONG in the
    bull-market design half (82.0% vs 92.8%) and right in the test half — treat CALL as lower confidence.
  * "Location" (fade a stretch to the top of the 4h Bollinger) LOST in both halves (−1.8 / −3.0 pts):
    selling calls because price is near the top of its range was the wrong side. Dropped from the score.
  * Not a price forecast: price moved the signalled way only ~50% of the time. It answers "which side
    is safer to sell", and only modestly. ~280 independent 48h windows, one asset, two regimes.

How "right" is scored (it is an option-SELLER's question, not a price forecast): at each 1h close,
place a strike one expected move away (EM = spot x 30d realised vol x sqrt(48h/1y), roughly a
0.16-delta 2-day option). The chosen side is RIGHT if that strike would have expired OTM 48h later
(and, stricter, if price never even touched it). Compared against: always-put, always-call, the
opposite side at the same moments, and a trend-only rule (weekly+daily agree).

CLI:  python -m research.direction --days 820            (fetches Delta 1h candles, caches to CSV)
"""
from __future__ import annotations

import argparse
import math
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .delta_api import DeltaPublicClient

WEIGHTS = {"weekly": 2, "daily": 2, "h4": 1, "h1": 1, "structure": 1, "vprofile": 1, "flow": 1}
INFO_ONLY = ("location",)   # measured as harmful to the score; still computed for display
THRESHOLD = 3
HORIZON_H = 48
EM_MULT = 1.0
VP_BARS = 720          # 30 days of 1h bars
VP_BINS = 100
FLOW_BARS = 24
PIVOT_LR = 3


# ------------------------------------------------------------------ data
def fetch_1h(symbol: str, start: int, end: int, cache: Path | None = None) -> pd.DataFrame:
    """Delta 1h candles [start, end] (epoch s), paged under the 4000-bar API cap, optionally cached."""
    if cache and cache.exists():
        df = pd.read_csv(cache)
        if df["time"].min() <= start + 3600 and df["time"].max() >= end - 2 * 3600:
            return _frame(df)
    client = DeltaPublicClient()
    rows: list[dict[str, Any]] = []
    step = 3900 * 3600
    s = start
    while s < end:
        e = min(s + step, end)
        rows += client.candles(symbol, "1h", s, e)
        s = e
        time.sleep(0.2)
    df = pd.DataFrame(rows)[["time", "open", "high", "low", "close", "volume"]]
    df = df.drop_duplicates("time").sort_values("time")
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(cache, index=False)
    return _frame(df)


def _frame(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["volume"] = df["volume"].fillna(0.0)
    df.index = pd.to_datetime(df["time"].astype("int64"), unit="s", utc=True)
    return df[["open", "high", "low", "close", "volume"]].dropna(subset=["close"])


# ------------------------------------------------------------------ pure rules (unit-tested)
def trend_vote(close: pd.Series) -> pd.Series:
    """+1 above BOTH EMA20 & EMA50, -1 below both, else 0 (EMA50 missing -> EMA20 alone)."""
    e20 = close.ewm(span=20, adjust=False).mean()
    e50 = close.ewm(span=50, adjust=False).mean()
    have50 = np.arange(len(close)) >= 49
    up = (close > e20) & ((close > e50) | ~have50)
    dn = (close < e20) & ((close < e50) | ~have50)
    return pd.Series(np.where(up, 1, np.where(dn, -1, 0)), index=close.index)


def structure_votes(high: np.ndarray, low: np.ndarray, close: np.ndarray, lr: int = PIVOT_LR) -> np.ndarray:
    """Last confirmed swing break per bar, known at that bar's close. A pivot at i is only usable
    from bar i+lr (confirmation), so nothing looks ahead."""
    n = len(close)
    out = np.zeros(n, dtype=int)
    last_hi = last_lo = np.nan
    state = 0
    for t in range(n):
        i = t - lr                                    # pivot candidate confirmed at bar t
        if i - lr >= 0:
            win_h = high[i - lr:i + lr + 1]
            win_l = low[i - lr:i + lr + 1]
            if high[i] == win_h.max():
                last_hi = high[i]
            if low[i] == win_l.min():
                last_lo = low[i]
        if not np.isnan(last_hi) and close[t] > last_hi:
            state = 1
        elif not np.isnan(last_lo) and close[t] < last_lo:
            state = -1
        out[t] = state
    return out


def poc(prices: np.ndarray, volumes: np.ndarray, bins: int = VP_BINS) -> float:
    """Volume-profile point of control: centre of the highest-volume price bin."""
    if len(prices) == 0 or volumes.sum() <= 0:
        return float("nan")
    hist, edges = np.histogram(prices, bins=bins, weights=volumes)
    k = int(np.argmax(hist))
    return float((edges[k] + edges[k + 1]) / 2)


def _v(x: Any) -> float:
    return 0.0 if (x is None or (isinstance(x, float) and math.isnan(x))) else float(x)


def combine(votes: dict[str, float]) -> tuple[float, str]:
    """Weighted score + side, with the never-fight-Weekly/Daily gate. Pure (unit-tested)."""
    score = sum(WEIGHTS[k] * _v(v) for k, v in votes.items() if k in WEIGHTS)
    w, d = _v(votes.get("weekly")), _v(votes.get("daily"))
    if score >= THRESHOLD and w >= 0 and d >= 0:
        return score, "PUT"
    if score <= -THRESHOLD and w <= 0 and d <= 0:
        return score, "CALL"
    return score, "STAND ASIDE"


# ------------------------------------------------------------------ feature build (causal)
def _htf_vote(h1: pd.DataFrame, rule: str, offset: pd.Timedelta) -> pd.Series:
    """Trend vote of the last CLOSED higher-TF bar, aligned to 1h decision times (1h bar close)."""
    agg = h1.resample(rule, label="left", closed="left").agg({"close": "last"}).dropna()
    vote = trend_vote(agg["close"])
    known = pd.Series(vote.values, index=agg.index + offset)       # usable once that bar has closed
    decide = h1.index + pd.Timedelta(hours=1)
    return known.reindex(known.index.union(decide)).ffill().reindex(decide).set_axis(h1.index)


def build(h1: pd.DataFrame) -> pd.DataFrame:
    f = pd.DataFrame(index=h1.index)
    f["weekly"] = _htf_vote(h1, "W-MON", pd.Timedelta(days=7))
    f["daily"] = _htf_vote(h1, "1D", pd.Timedelta(days=1))
    f["h4"] = _htf_vote(h1, "4h", pd.Timedelta(hours=4))
    f["h1"] = trend_vote(h1["close"])

    h4 = h1.resample("4h", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    st = pd.Series(structure_votes(h4["high"].values, h4["low"].values, h4["close"].values),
                   index=h4.index + pd.Timedelta(hours=4))
    mid = h4["close"].rolling(20).mean()
    sd = h4["close"].rolling(20).std(ddof=0)
    bb_u = pd.Series((mid + 2 * sd).values, index=h4.index + pd.Timedelta(hours=4))
    bb_l = pd.Series((mid - 2 * sd).values, index=h4.index + pd.Timedelta(hours=4))
    decide = h1.index + pd.Timedelta(hours=1)

    def asof(s: pd.Series) -> np.ndarray:
        return s.reindex(s.index.union(decide)).ffill().reindex(decide).values

    f["structure"] = asof(st)
    u, l = asof(bb_u), asof(bb_l)
    pb = (h1["close"].values - l) / (u - l)
    f["location"] = np.where(pb >= 0.9, -1, np.where(pb <= 0.1, 1, 0))
    f.loc[np.isnan(pb), "location"] = np.nan

    rng = (h1["high"] - h1["low"]).replace(0, np.nan)
    delta = (h1["volume"] * (h1["close"] - h1["open"]) / rng).fillna(0.0)
    flow = delta.rolling(FLOW_BARS).sum()
    f["flow"] = np.sign(flow)

    tp = ((h1["high"] + h1["low"] + h1["close"]) / 3).values
    vol = h1["volume"].values
    close = h1["close"].values
    vp = np.full(len(h1), np.nan)
    for t in range(VP_BARS - 1, len(h1)):
        p = poc(tp[t - VP_BARS + 1:t + 1], vol[t - VP_BARS + 1:t + 1])
        if not math.isnan(p):
            vp[t] = 1 if close[t] > p * 1.001 else -1 if close[t] < p * 0.999 else 0
    f["vprofile"] = vp

    w = pd.Series(WEIGHTS)
    f["score"] = f[list(WEIGHTS)].fillna(0).mul(w).sum(axis=1)
    f.loc[f[list(WEIGHTS)].isna().any(axis=1), "score"] = np.nan          # warm-up incomplete
    put_ok = (f["weekly"] >= 0) & (f["daily"] >= 0)
    call_ok = (f["weekly"] <= 0) & (f["daily"] <= 0)
    f["side"] = np.where((f["score"] >= THRESHOLD) & put_ok, "PUT",
                         np.where((f["score"] <= -THRESHOLD) & call_ok, "CALL", "STAND ASIDE"))
    f.loc[f["score"].isna(), "side"] = None
    return f


# ------------------------------------------------------------------ outcomes + evaluation
def outcomes(h1: pd.DataFrame, horizon: int = HORIZON_H, em_mult: float = EM_MULT) -> pd.DataFrame:
    c, hi, lo = h1["close"].values, h1["high"].values, h1["low"].values
    lr = np.log(h1["close"]).diff()
    sigma = (lr.rolling(720).std() * math.sqrt(8760)).values
    n = len(c)
    o = pd.DataFrame(index=h1.index, columns=["em", "put_exp", "call_exp", "put_path", "call_path", "ret"], dtype=float)
    for t in range(n - horizon):
        if np.isnan(sigma[t]):
            continue
        em = c[t] * sigma[t] * math.sqrt(horizon / 8760) * em_mult
        fut_c = c[t + horizon]
        fut_lo, fut_hi = lo[t + 1:t + horizon + 1].min(), hi[t + 1:t + horizon + 1].max()
        o.iloc[t] = [em, fut_c > c[t] - em, fut_c < c[t] + em, fut_lo > c[t] - em, fut_hi < c[t] + em, fut_c / c[t] - 1]
    return o.dropna()


def _rate(x: pd.Series) -> float:
    return float(x.mean()) if len(x) else float("nan")


def _binom_p(k: int, n: int, p0: float) -> float:
    """One-sided normal-approx p-value that the true rate exceeds p0."""
    if n == 0 or p0 <= 0 or p0 >= 1:
        return float("nan")
    z = (k - n * p0) / math.sqrt(n * p0 * (1 - p0))
    return 0.5 * math.erfc(z / math.sqrt(2))


def evaluate(f: pd.DataFrame, o: pd.DataFrame, horizon: int = HORIZON_H) -> dict[str, Any]:
    d = f.join(o, how="inner").dropna(subset=["score"])
    res: dict[str, Any] = {"bars": len(d), "start": str(d.index.min()), "end": str(d.index.max()),
                           "base_put_exp": _rate(d["put_exp"]), "base_call_exp": _rate(d["call_exp"]),
                           "base_put_path": _rate(d["put_path"]), "base_call_path": _rate(d["call_path"])}
    for side, own, other in (("PUT", "put", "call"), ("CALL", "call", "put")):
        s = d[d["side"] == side]
        n_ind = len(s) // horizon                                          # ~independent 48h windows
        own_rate = _rate(s[f"{own}_exp"])
        res[side] = {"bars": len(s), "share": len(s) / len(d), "indep_windows": n_ind,
                     "own_exp": own_rate, "own_path": _rate(s[f"{own}_path"]),
                     "opposite_exp": _rate(s[f"{other}_exp"]), "opposite_path": _rate(s[f"{other}_path"]),
                     "base_own_exp": res[f"base_{own}_exp"],
                     "p_vs_base": _binom_p(int(round(own_rate * n_ind)), n_ind, res[f"base_{own}_exp"]) if n_ind else float("nan"),
                     "dir_hit": _rate((s["ret"] > 0) if side == "PUT" else (s["ret"] < 0))}
    sa = d[d["side"] == "STAND ASIDE"]
    res["STAND ASIDE"] = {"bars": len(sa), "share": len(sa) / len(d),
                          "best_side_exp": max(_rate(sa["put_exp"]), _rate(sa["call_exp"])) if len(sa) else float("nan")}
    # trend-only comparison: weekly and daily agree
    tr = d[(d["weekly"] == d["daily"]) & (d["weekly"] != 0)]
    res["trend_only"] = {"bars": len(tr),
                         "own_exp": _rate(pd.concat([tr.loc[tr["weekly"] == 1, "put_exp"], tr.loc[tr["weekly"] == -1, "call_exp"]]))}
    # each factor alone: when it votes, how often is ITS side right vs the opposite side
    res["factors"] = {}
    for k in list(WEIGHTS) + list(INFO_ONLY):
        up, dn = d[d[k] == 1], d[d[k] == -1]
        right = pd.concat([up["put_exp"], dn["call_exp"]])
        wrong = pd.concat([up["call_exp"], dn["put_exp"]])
        res["factors"][k] = {"votes": len(right), "own_exp": _rate(right), "opposite_exp": _rate(wrong)}
    # stability: first half vs second half of the sample
    mid = d.index[len(d) // 2]
    for half, part in (("first_half", d[d.index < mid]), ("second_half", d[d.index >= mid])):
        put, call = part[part["side"] == "PUT"], part[part["side"] == "CALL"]
        res[half] = {"put_bars": len(put), "put_own": _rate(put["put_exp"]), "put_opp": _rate(put["call_exp"]),
                     "call_bars": len(call), "call_own": _rate(call["call_exp"]), "call_opp": _rate(call["put_exp"])}
    return res


def _pct(x: float) -> str:
    return "  n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{100 * x:5.1f}%"


def report(r: dict[str, Any]) -> None:
    print("=" * 96)
    print(f"  DIRECTION SCORE BACKTEST — Delta BTCUSD 1h, {r['start'][:10]} → {r['end'][:10]}  ({r['bars']:,} decisions)")
    print(f"  'right' = the chosen side's 1-EM strike (≈0.16δ, 48h) expires OTM. Path = never even touched.")
    print("=" * 96)
    print(f"  Baseline (sell blindly every hour):  puts OK {_pct(r['base_put_exp'])}   calls OK {_pct(r['base_call_exp'])}"
          f"   | never-touched: puts {_pct(r['base_put_path'])}  calls {_pct(r['base_call_path'])}")
    for side in ("PUT", "CALL"):
        s = r[side]
        print(f"\n  Signal {side:<4} — {_pct(s['share'])} of the time ({s['bars']:,} bars ≈ {s['indep_windows']} independent 48h windows)")
        print(f"    chosen side OK at expiry  {_pct(s['own_exp'])}   (opposite side would be {_pct(s['opposite_exp'])}; "
              f"blind {side.lower()} baseline {_pct(s['base_own_exp'])}; p vs baseline {s['p_vs_base']:.3f})")
        print(f"    chosen side never touched {_pct(s['own_path'])}   (opposite {_pct(s['opposite_path'])})   "
              f"| price moved the signalled way {_pct(s['dir_hit'])}")
    sa = r["STAND ASIDE"]
    print(f"\n  STAND ASIDE — {_pct(sa['share'])} of the time; the better side then was OK {_pct(sa['best_side_exp'])}")
    print(f"  Trend-only (weekly+daily agree) — chosen side OK {_pct(r['trend_only']['own_exp'])} on {r['trend_only']['bars']:,} bars")
    print("\n  Each factor alone (when it votes):            its side OK    opposite side OK    votes")
    for k, v in r["factors"].items():
        print(f"    {k:<12} (w{WEIGHTS.get(k, 0)})                         {_pct(v['own_exp'])}         {_pct(v['opposite_exp'])}      {v['votes']:,}")
    print("\n  Stability (chosen vs opposite side, OK at expiry):")
    for half in ("first_half", "second_half"):
        h = r[half]
        print(f"    {half:<12} PUT {h['put_bars']:>6,} bars: {_pct(h['put_own'])} vs {_pct(h['put_opp'])}   "
              f"CALL {h['call_bars']:>6,} bars: {_pct(h['call_own'])} vs {_pct(h['call_opp'])}")
    print("\n  Read-only research. A direction score is a disciplined lean, not a forecast; not advice.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest the put/call direction score on Delta BTC 1h candles (read-only).")
    ap.add_argument("--days", type=int, default=820, help="history to fetch (needs ~400 days of weekly-EMA warm-up)")
    ap.add_argument("--cache", type=Path, default=Path("research/data/processed/BTCUSD_1h.csv"))
    args = ap.parse_args()
    end = int(time.time()) // 3600 * 3600
    h1 = fetch_1h("BTCUSD", end - args.days * 86400, end, args.cache)
    f = build(h1)
    o = outcomes(h1)
    report(evaluate(f, o))


if __name__ == "__main__":
    main()

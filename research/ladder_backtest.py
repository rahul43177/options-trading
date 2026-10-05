"""
Backtest of the /options-entry MECHANICAL core (and the OPT-AUTO indicator that mirrors it).
READ-ONLY: public Delta 1h candles in, statistics out.

What can be replayed honestly: Delta keeps no historical option chains, so premiums/OI/spreads can't
be replayed. What CAN be replayed from candles is the part of the skill that decides WHERE the strike
goes and WHEN to act:
  side      research.direction (puts / calls / stand aside, never fights Weekly/Daily)
  zones     research.zones levels (1h EMA20/50/200, 1h & 4h Bollinger, prior-day pivots, 30d POC),
            clustered with the SAME cluster_levels(); NEAR + STRUCTURAL band per side
  strike    put: 200-grid strikes at or below (near-support arrival − 1000); call mirror.
              SCANNER = |delta| closest to 0.16 in [0.10, 0.22]
              BEST    = in-band strike past the structural shelf − 0.5×4h ATR nearest the target,
                        never closer than SCANNER (falls back to it)
  trigger   VALID ≈ near band touched in the last 6h AND the 1h close breaks the prior 3-bar high
            (≈ the indicator's 12×15m break), first VALID per 24h only

Outcome: the strike expires OTM 48h later (2-day option), and stricter: never touched.
Fair comparison: a strike's safety mostly comes from its DISTANCE, so every group is compared with
blind selling at the SAME distance (in expected moves) at all hours — the "distance-matched" rate.

Limits (stated in the report): delta uses 30-day realised vol as the IV proxy; the strike grid is an
even 200 (Delta's real far-OTM grid is irregular); no VAH/VAL; trigger approximated on 1h bars; no
premiums, so this measures how often the side/strike/timing was RIGHT, not P&L.

CLI:  python -m research.ladder_backtest
"""
from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from . import pricing
from .direction import HORIZON_H, VP_BARS, build as build_direction, fetch_1h, poc
from .zones import classic_pivots, cluster_levels

BUFFER = 1000.0
STEP = 200.0
ROUND = 100.0
DMIN, DMAX, DTARGET = 0.10, 0.22, 0.16
TOL = 0.0045


def _asof(series: pd.Series, decide: pd.DatetimeIndex) -> np.ndarray:
    return series.reindex(series.index.union(decide)).ffill().reindex(decide).values


def levels(h1: pd.DataFrame) -> pd.DataFrame:
    """Causal per-hour levels (known at that 1h bar's close)."""
    c = h1["close"]
    lv = pd.DataFrame(index=h1.index)
    lv["1h EMA20"] = c.ewm(span=20, adjust=False).mean()
    lv["1h EMA50"] = c.ewm(span=50, adjust=False).mean()
    lv["1h EMA200"] = c.ewm(span=200, adjust=False).mean()
    m, sd = c.rolling(20).mean(), c.rolling(20).std(ddof=0)
    lv["1h BB-up"], lv["1h BB-lo"] = m + 2 * sd, m - 2 * sd
    decide = h1.index + pd.Timedelta(hours=1)
    h4 = h1.resample("4h", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    m4, sd4 = h4["close"].rolling(20).mean(), h4["close"].rolling(20).std(ddof=0)
    shift4 = h4.index + pd.Timedelta(hours=4)
    lv["4h BB-up"] = _asof(pd.Series((m4 + 2 * sd4).values, index=shift4), decide)
    lv["4h BB-lo"] = _asof(pd.Series((m4 - 2 * sd4).values, index=shift4), decide)
    tr = pd.concat([h4["high"] - h4["low"], (h4["high"] - h4["close"].shift()).abs(),
                    (h4["low"] - h4["close"].shift()).abs()], axis=1).max(axis=1)
    lv["atr4h"] = _asof(pd.Series(tr.rolling(14).mean().values, index=shift4), decide)
    d1 = h1.resample("1D", label="left", closed="left").agg({"high": "max", "low": "min", "close": "last"}).dropna()
    piv = pd.DataFrame([classic_pivots(r.high, r.low, r.close) for r in d1.itertuples()], index=d1.index + pd.Timedelta(days=1))
    for k in ("R1", "R2", "R3", "S1", "S2", "S3"):
        lv["pivot " + k] = _asof(piv[k], decide)
    tp = ((h1["high"] + h1["low"] + h1["close"]) / 3).values
    vol = h1["volume"].values
    pocs = np.full(len(h1), np.nan)
    for t in range(VP_BARS - 1, len(h1)):
        pocs[t] = poc(tp[t - VP_BARS + 1:t + 1], vol[t - VP_BARS + 1:t + 1])
    lv["POC"] = pocs
    return lv


def _pick(is_put: bool, S: float, gate: float, shelf_edge: float, T: float, vol: float) -> tuple[float, float]:
    """(scanner strike, best strike) on the 200 grid; nan if none in the delta band.

    SCANNER = |delta| closest to the target inside the band (beyond the zone buffer).
    BEST    = of the in-band strikes past the structural shelf, the one closest to the target —
              and NEVER closer to price than the scanner pick (falls back to it). The first version
              took "the first in-band strike past the shelf", which when the shelf sat above the gate
              meant the HIGHEST-delta strike: 0.85 EM away and 85.7% OTM vs the scanner's 89.0%.
    """
    kind = "put" if is_put else "call"
    k0 = math.floor(gate / STEP) * STEP if is_put else math.ceil(gate / STEP) * STEP
    scan, err = math.nan, 9.0
    past: list[tuple[float, float]] = []
    for i in range(40):
        k = k0 - i * STEP if is_put else k0 + i * STEP
        ad = abs(pricing.delta(S, k, T, vol, kind))
        if DMIN <= ad <= DMAX:
            if abs(ad - DTARGET) < err:
                err, scan = abs(ad - DTARGET), k
            if math.isnan(shelf_edge) or ((k <= shelf_edge) if is_put else (k >= shelf_edge)):
                past.append((abs(ad - DTARGET), k))
        elif ad < DMIN:
            break
    if math.isnan(scan):
        return scan, scan
    best = min(past)[1] if past else scan
    return scan, (min(best, scan) if is_put else max(best, scan))


def run(h1: pd.DataFrame) -> dict[str, Any]:
    side = build_direction(h1)["side"]
    lv = levels(h1)
    names = [c for c in lv.columns if c != "atr4h"]
    c, hi, lo = h1["close"].values, h1["high"].values, h1["low"].values
    sigma = (np.log(h1["close"]).diff().rolling(720).std() * math.sqrt(8760)).values
    T = HORIZON_H / 8760
    n = len(h1)
    recs: list[dict[str, Any]] = []
    near_hist: dict[str, list[float]] = {"PUT": [math.nan] * n, "CALL": [math.nan] * n}
    last_valid = {"PUT": -10**9, "CALL": -10**9}
    for t in range(n - HORIZON_H):
        sd = side.iloc[t]
        if sd not in ("PUT", "CALL") or math.isnan(sigma[t]):
            continue
        row = lv.iloc[t]
        if row[names].isna().any() or math.isnan(row["atr4h"]):
            continue
        S = c[t]
        is_put = sd == "PUT"
        bands = cluster_levels(S, [(row[k], k) for k in names], "support" if is_put else "resistance", TOL)
        if not bands:
            continue
        near = bands[0]
        shelf = bands[1] if len(bands) > 1 else near
        if is_put:
            arrival = math.floor(near["hi"] / ROUND) * ROUND
            gate, shelf_edge = arrival - BUFFER, shelf["lo"] - 0.5 * row["atr4h"]
        else:
            arrival = math.ceil(near["lo"] / ROUND) * ROUND
            gate, shelf_edge = arrival + BUFFER, shelf["hi"] + 0.5 * row["atr4h"]
        scan, best = _pick(is_put, S, gate, shelf_edge, T, sigma[t])
        if math.isnan(scan):
            continue
        near_hist[sd][t] = near["hi"] if is_put else near["lo"]
        # VALID ≈ touched the near band in the last 6h and the close breaks the prior 3-bar extreme
        lb = range(max(0, t - 5), t + 1)
        if is_put:
            touched = any(not math.isnan(near_hist["PUT"][j]) and lo[j] <= near_hist["PUT"][j] for j in lb)
            broke = t >= 3 and S > hi[t - 3:t].max()
        else:
            touched = any(not math.isnan(near_hist["CALL"][j]) and hi[j] >= near_hist["CALL"][j] for j in lb)
            broke = t >= 3 and S < lo[t - 3:t].min()
        valid = touched and broke and t - last_valid[sd] >= 24
        if valid:
            last_valid[sd] = t
        em = S * sigma[t] * math.sqrt(T)
        fut_c = c[t + HORIZON_H]
        f_lo, f_hi = lo[t + 1:t + HORIZON_H + 1].min(), hi[t + 1:t + HORIZON_H + 1].max()
        for label, k in (("scanner", scan), ("best", best)):
            ok = fut_c > k if is_put else fut_c < k
            clean = f_lo > k if is_put else f_hi < k
            recs.append({"t": t, "side": sd, "pick": label, "valid": valid, "strike": k,
                         "dist_em": abs(S - k) / em, "ok": ok, "clean": clean})
    df = pd.DataFrame(recs)
    # distance-matched blind baseline: P(OTM at 48h) for a strike d EMs away, over ALL hours
    ems = c * sigma * math.sqrt(T)
    grid = np.arange(0.0, 4.01, 0.05)
    put_rate, call_rate = [], []
    valid_t = [t for t in range(n - HORIZON_H) if not math.isnan(sigma[t])]
    fc = np.array([c[t + HORIZON_H] for t in valid_t]); s0 = c[valid_t]; e0 = ems[valid_t]
    for d in grid:
        put_rate.append(float(np.mean(fc > s0 - d * e0)))
        call_rate.append(float(np.mean(fc < s0 + d * e0)))
    df["blind_same_dist"] = [np.interp(r.dist_em, grid, put_rate if r.side == "PUT" else call_rate) for r in df.itertuples()]
    return {"df": df, "start": str(h1.index[0])[:10], "end": str(h1.index[-1])[:10]}


def _independent(ts: pd.Series) -> int:
    """Trades whose 48h windows don't overlap (greedy) — the honest sample size."""
    count, last = 0, -10**9
    for t in sorted(ts.unique()):
        if t - last >= HORIZON_H:
            count, last = count + 1, t
    return count


def _line(name: str, g: pd.DataFrame) -> str:
    if g.empty:
        return f"  {name:<34} (no trades)"
    n_ind = _independent(g["t"])
    edge = 100 * (g["ok"].mean() - g["blind_same_dist"].mean())
    return (f"  {name:<34} n={len(g):>6,} (~{n_ind:>4} indep)  dist {g['dist_em'].mean():4.2f} EM   "
            f"OTM {100 * g['ok'].mean():5.1f}%  vs blind-same-distance {100 * g['blind_same_dist'].mean():5.1f}%  "
            f"edge {edge:+5.1f} pts   never-touched {100 * g['clean'].mean():5.1f}%")


def report(r: dict[str, Any]) -> None:
    df = r["df"]
    print("=" * 128)
    print(f"  /options-entry MECHANICAL CORE BACKTEST — Delta BTCUSD 1h, {r['start']} → {r['end']}, 48h (2-day) strikes")
    print("  side = research.direction · strike beyond near zone − 1000 buffer · 'edge' = vs blind selling at the SAME distance")
    print("=" * 128)
    for side in ("PUT", "CALL"):
        for pick in ("scanner", "best"):
            g = df[(df.side == side) & (df.pick == pick)]
            print(_line(f"{side} {pick} — every signalled hour", g))
            print(_line(f"{side} {pick} — only at VALID trigger", g[g.valid]))
        print()
    mid = df["t"].median()
    print("  Stability (BEST strike, every signalled hour, edge vs blind same distance):")
    for name, part in (("first half", df[df.t < mid]), ("second half", df[df.t >= mid])):
        for side in ("PUT", "CALL"):
            g = part[(part.side == side) & (part.pick == "best")]
            if len(g):
                print(f"    {name:<12} {side:<5} OTM {100 * g.ok.mean():5.1f}%  vs {100 * g.blind_same_dist.mean():5.1f}%  "
                      f"edge {100 * (g.ok.mean() - g.blind_same_dist.mean()):+5.1f} pts  (n={len(g):,})")
    print("\n  Limits: IV proxied by 30d realised vol; even 200 strike grid; no premiums/fees (hit-rate, not P&L);")
    print("  trigger approximated on 1h. Read-only research — not advice.")


def main() -> None:
    end = int(time.time()) // 3600 * 3600
    h1 = fetch_1h("BTCUSD", end - 820 * 86400, end, Path("research/data/processed/BTCUSD_1h.csv"))
    report(run(h1))


if __name__ == "__main__":
    main()

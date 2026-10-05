"""Empirical "when will price reach the zone?" model — the timing input behind rest@ / base / fast.

Why (measured 05-Oct-2026): the premium band is a Black-Scholes reprice at the zone, and the
reprice itself is accurate (with the ACTUAL arrival time plugged in, the median error vs the real
Delta mark was +3.8%; IV at the touch was ~flat: puts 0.97x, calls 1.02x). The error came from
the ARRIVAL TIME. The legacy rule `hours = distance / (0.5 x ATR1h)` assumes steady travel; on
23,808 Delta samples only 33% of arrivals came within it (median arrival at 1 ATR away: 4.5h,
not 2h; at 3 ATR: 17.5h, not 6h). A pure random walk gets P(arrive) right but still runs early
(real tape chops), so this module uses the DATA directly:

  * For distance m (in units of the same Wilder ATR(1h) the scanner uses) and a horizon H
    (hours left on the option), from 60+ days of Delta 5m/1h candles (BTC+ETH, up and down) it
    tabulates P(price touches the zone within H) and the 25th / 50th / 75th percentile of the
    arrival time GIVEN that it arrives within H.
  * research.entry_scan's band then prices: fast = 25th pct (+IV bump), base = median,
    floor (rest@) = 75th pct — so a resting SELL at rest@ is at-or-below the premium on ~75% of
    arrivals, and `valid` = that 75th-pct time.

Refit any time (it's read-only, public candles):  python -m research.arrival fit --days 60
Validate out-of-sample:                           python -m research.arrival validate --days 60
The table lives in research/data/arrival_table.json; if it is missing, callers fall back to the
legacy linear rule. Pure helpers (lookup/interp/quantiles) are unit-tested.
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
import math
import time
from pathlib import Path
from typing import Any

from .config import ROOT

TABLE_PATH = ROOT / "data" / "arrival_table.json"
M_GRID = [0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0]
H_GRID = [6.0, 12.0, 24.0, 36.0, 48.0, 72.0, 96.0]
QUANTS = (0.25, 0.5, 0.75)
ATR_N = 14
ATR_WINDOW_H = 240          # runtime underlying_atr_1h uses 10 days of 1h bars, Wilder RMA


# ----------------------------------------------------------------------------- pure helpers
def wilder_atr(bars: list[dict[str, float]], n: int = ATR_N) -> float | None:
    """Same definition as perp_analytics.atr (Wilder RMA of true range)."""
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


def quantile(xs: list[float], q: float) -> float:
    s = sorted(xs)
    if not s:
        return float("nan")
    pos = q * (len(s) - 1)
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def first_touch_hours(m5: list[dict[str, float]], start_idx: int, zone: float, up: bool,
                      horizon_h: float) -> float | None:
    """Hours from bar `start_idx` until a 5m bar's high (up) / low (down) touches `zone`."""
    t0 = m5[start_idx]["time"]
    end = t0 + horizon_h * 3600
    for j in range(start_idx, len(m5)):
        b = m5[j]
        if b["time"] > end:
            return None
        if (up and b["high"] >= zone) or (not up and b["low"] <= zone):
            return (b["time"] - t0) / 3600.0
    return None


def build_samples(m5: list[dict[str, float]], h1: list[dict[str, float]], step_h: int = 1,
                  max_h: float = max(H_GRID), t_min: int | None = None,
                  t_max: int | None = None) -> list[dict[str, Any]]:
    """One sample per (anchor hour, distance m, direction). Anchor ATR uses only PAST 1h bars."""
    t5 = [b["time"] for b in m5]
    out = []
    for i in range(ATR_WINDOW_H, len(h1)):
        t0 = h1[i]["time"]
        if t0 % (step_h * 3600):
            continue
        if (t_min and t0 < t_min) or (t_max and t0 >= t_max):
            continue
        a = wilder_atr(h1[i - ATR_WINDOW_H:i])
        k = bisect.bisect_left(t5, t0)
        if not a or k >= len(m5):
            continue
        spot = m5[k]["open"] if "open" in m5[k] else m5[k]["close"]
        avail = (m5[-1]["time"] - t0) / 3600.0
        for m in M_GRID:
            for up in (True, False):
                z = spot + (m * a if up else -m * a)
                hit = first_touch_hours(m5, k, z, up, max_h)
                out.append({"t0": t0, "m": m, "up": up, "hit": hit, "avail": avail})
    return out


def table_from_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    tab: dict[str, Any] = {}
    for H in H_GRID:
        row = {}
        for m in M_GRID:
            g = [s for s in samples if s["m"] == m and s["avail"] >= H]
            if len(g) < 30:
                continue
            hits = [s["hit"] for s in g if s["hit"] is not None and s["hit"] <= H]
            cell = {"n": len(g), "p_hit": len(hits) / len(g)}
            if len(hits) >= 10:
                for q in QUANTS:
                    cell[f"q{int(q * 100)}"] = round(quantile(hits, q), 3)
            row[str(m)] = cell
        tab[str(H)] = row
    return tab


def _interp(x: float, xs: list[float], ys: list[float]) -> float:
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    j = bisect.bisect_right(xs, x)
    x0, x1, y0, y1 = xs[j - 1], xs[j], ys[j - 1], ys[j]
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def _row_lookup(row: dict[str, Any], m: float) -> dict[str, float] | None:
    """Interpolate one H-row of the table at distance m (in ATRs)."""
    all_m = sorted(row, key=float)
    full = [k for k in all_m if all(f"q{int(q * 100)}" in row[k] for q in QUANTS)]
    if len(full) < 2:
        return None
    out = {"p_hit": _interp(m, [float(k) for k in all_m], [row[k]["p_hit"] for k in all_m])}
    lm = [math.log(float(k)) for k in full]
    for q in QUANTS:
        key = f"q{int(q * 100)}"
        ys = [math.log(max(row[k][key], 1e-3)) for k in full]
        if m < float(full[0]):
            # below the grid: shrink time with the local power law (never 0 -> no instant fills)
            slope = max((ys[1] - ys[0]) / (lm[1] - lm[0]), 1.0)
            val = math.exp(ys[0] + slope * (math.log(max(m, 1e-3)) - lm[0]))
        elif m > float(full[-1]):
            slope = (ys[-1] - ys[-2]) / (lm[-1] - lm[-2])
            val = math.exp(ys[-1] + slope * (math.log(m) - lm[-1]))
        else:
            val = math.exp(_interp(math.log(m), lm, ys))
        out[key] = val
    return out


def _key(x: float, d: dict[str, Any]) -> str:
    for k in d:
        if abs(float(k) - x) < 1e-9:
            return k
    raise KeyError(x)


def lookup(m: float, hours_left: float, table: dict[str, Any]) -> dict[str, float] | None:
    """Arrival quantiles (hours) + P(arrive within hours_left) for distance m ATRs. Pure.

    Interpolates linearly in H between table rows and in log-time / log-m within a row; every
    quantile is capped at hours_left (you can't arrive after expiry and still trade it).
    """
    if m is None or hours_left is None or hours_left <= 0 or not table:
        return None
    if m <= 0:
        return {"p_hit": 1.0, "q25": 0.0, "q50": 0.0, "q75": 0.0}
    tab = table.get("table", table)
    hs = sorted(float(h) for h in tab if tab[h])
    if not hs:
        return None
    lo = max([h for h in hs if h <= hours_left], default=hs[0])
    hi = min([h for h in hs if h >= hours_left], default=hs[-1])
    r_lo = _row_lookup(tab[_key(lo, tab)], m)
    r_hi = _row_lookup(tab[_key(hi, tab)], m)
    if r_lo is None or r_hi is None:
        return None
    w = 0.0 if hi == lo else (min(max(hours_left, lo), hi) - lo) / (hi - lo)
    out = {k: r_lo[k] + (r_hi[k] - r_lo[k]) * w for k in r_lo}
    if hours_left < hs[0]:                       # shorter than the grid: scale P(hit) down
        out["p_hit"] *= math.sqrt(hours_left / hs[0])
    for q in QUANTS:
        key = f"q{int(q * 100)}"
        out[key] = min(out[key], hours_left)
    out["p_hit"] = max(0.0, min(1.0, out["p_hit"]))
    return out


_CACHE: dict[str, Any] | None = None


def load_table(path: Path = TABLE_PATH) -> dict[str, Any] | None:
    global _CACHE
    if _CACHE is None:
        try:
            _CACHE = json.loads(Path(path).read_text())
        except Exception:
            _CACHE = {}
    return _CACHE or None


# ----------------------------------------------------------------------------- data + CLI
def _fetch(asset: str, days: int) -> tuple[list[dict], list[dict]]:
    from .delta_api import DeltaPublicClient
    from .config import UNDERLYINGS
    c = DeltaPublicClient()
    now = int(time.time())
    sym = UNDERLYINGS[asset]

    def pull(res: str, secs: int, d: int) -> list[dict]:
        out, s = {}, now - d * 86400
        while s < now:
            e = min(now, s + secs * 1900)
            for b in c.candles(sym, res, s, e):
                out[int(b["time"])] = {k: float(b[k]) for k in ("open", "high", "low", "close")} | {"time": int(b["time"])}
            s = e
        return [out[k] for k in sorted(out)]
    return pull("5m", 300, days), pull("1h", 3600, days + ATR_WINDOW_H // 24 + 1)


def _coverage(samples: list[dict[str, Any]], table: dict[str, Any]) -> dict[str, Any]:
    res: dict[str, Any] = {}
    for H in (24.0, 48.0, 72.0):
        g = [s for s in samples if s["avail"] >= H]
        hits = [s for s in g if s["hit"] is not None and s["hit"] <= H]
        cov = {}
        for q in QUANTS:
            key = f"q{int(q * 100)}"
            ok = [s["hit"] <= lookup(s["m"], H, table)[key] for s in hits]
            cov[key] = round(sum(ok) / len(ok), 3) if ok else None
        preds = [lookup(s["m"], H, table)["p_hit"] for s in g]
        res[str(H)] = {"n": len(g), "coverage": cov,
                       "p_hit_pred": round(sum(preds) / len(preds), 3) if preds else None,
                       "p_hit_actual": round(len(hits) / len(g), 3) if g else None,
                       "legacy_within_hz": round(sum(s["hit"] <= s["m"] / 0.5 for s in hits) / len(hits), 3) if hits else None}
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description="Empirical arrival-time table for the rest@ band (read-only).")
    ap.add_argument("cmd", choices=["fit", "validate", "show"])
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--assets", default="BTC,ETH")
    a = ap.parse_args()
    if a.cmd == "show":
        t = load_table()
        if not t:
            print("no arrival table yet — run: python -m research.arrival fit")
            return
        print(f"arrival table: {t.get('generated_at_utc')} · {t.get('days')}d · {t.get('assets')} · samples {t.get('n_samples')}")
        for H in ("24.0", "48.0", "72.0"):
            print(f"\n  H={H}h   m(ATR)  P(arrive)   q25    q50    q75 (hours, given it arrives)")
            for m, c in t["table"].get(H, {}).items():
                if "q50" in c:
                    print(f"          {float(m):5.2f}   {c['p_hit']:6.2f}   {c['q25']:5.1f}  {c['q50']:5.1f}  {c['q75']:5.1f}")
        return
    samples: list[dict[str, Any]] = []
    for asset in a.assets.split(","):
        m5, h1 = _fetch(asset.strip(), a.days)
        samples += build_samples(m5, h1)
    if a.cmd == "validate":
        mid = sorted(s["t0"] for s in samples)[len(samples) // 2]
        train = [s for s in samples if s["t0"] < mid]
        test = [s for s in samples if s["t0"] >= mid]
        t = {"table": table_from_samples(train)}
        print(json.dumps({"train_n": len(train), "test_n": len(test),
                          "out_of_sample": _coverage(test, t)}, indent=2))
        return
    tab = {"generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "days": a.days, "assets": a.assets, "n_samples": len(samples),
           "m_grid": M_GRID, "h_grid": H_GRID, "atr": "Wilder 14 on 1h, 240h window",
           "table": table_from_samples(samples)}
    TABLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    TABLE_PATH.write_text(json.dumps(tab, indent=1))
    print(f"wrote {TABLE_PATH} ({len(samples)} samples)")


if __name__ == "__main__":
    main()

"""rest@ accuracy backtest on REAL Delta option marks — READ-ONLY (public candles, no key).

Every watch_plan / entry_scan --log run appends its projection to research/data/proj_log.jsonl.
This replays each logged projection forward:

  1. Did the underlying actually touch the zone before expiry (5m highs/lows)?  -> P(arrive)
  2. At the first touch, what was the option's real Delta MARK (5m bar close / high)?
  3. Score the LEGACY band (as logged) and the ARRIVAL-model band (recomputed from the same logged
     inputs) against it: rest@ fill rate (target ~75%), base error, premium captured.

    python -m research.rest_backtest            # all logged projections with candle coverage
    python -m research.rest_backtest --days 14  # only the last 14 days

Run it weekly. If the arrival model's fill rate drifts far from 75%, refit the table
(python -m research.arrival fit) — the market's speed has changed. Not advice.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics as st
import time
from typing import Any

from . import arrival
from .delta_api import DeltaPublicClient
from .pricing import project_premium_arrival
from .proj_log import LOG


def _expiry_ts(sym: str) -> int:
    d = sym.split("-")[-1]
    return int(dt.datetime(2000 + int(d[4:6]), int(d[2:4]), int(d[0:2]), 12,
                           tzinfo=dt.timezone.utc).timestamp())


def first_touch(bars: list[tuple[int, dict]], zone: float, kind: str) -> tuple[int, dict] | None:
    """First 5m bar whose low (put zone, price falling) / high (call zone) reaches `zone`. Pure."""
    for t, b in bars:
        if (kind == "put" and b["low"] <= zone) or (kind == "call" and b["high"] >= zone):
            return t, b
    return None


def score(recs: list[dict[str, Any]], key: str) -> dict[str, Any]:
    """Fill rate / coverage / base error for one model over records with a touch. Pure."""
    hits = [r for r in recs if r.get("real") is not None]
    if not hits:
        return {"n": 0}
    fill = sum(r["real_hi"] >= r[key]["floor"] for r in hits) / len(hits)
    cov = sum(r["real"] >= r[key]["floor"] for r in hits) / len(hits)
    err = [(r[key]["base"] - r["real"]) / r["real"] * 100 for r in hits if r["real"] > 0]
    got = [r[key]["floor"] / r["real"] for r in hits if r["real"] >= r[key]["floor"] and r["real"] > 0]
    return {"n": len(hits), "fill_rate": fill, "close_above_rest": cov,
            "base_err_median_pct": st.median(err) if err else None,
            "rest_vs_real_when_filled": st.median(got) if got else None}


class _Candles:
    def __init__(self, days: int):
        self.c = DeltaPublicClient()
        self.now = int(time.time())
        self.start = self.now - days * 86400
        self.cache: dict[str, dict[int, dict]] = {}

    def get(self, symbol: str) -> dict[int, dict]:
        if symbol not in self.cache:
            out, s = {}, self.start
            while s < self.now:
                e = min(self.now, s + 300 * 1900)
                try:
                    for b in self.c.candles(symbol, "5m", s, e):
                        out[int(b["time"])] = {k: float(b[k]) for k in ("high", "low", "close")}
                except Exception:
                    pass
                s = e
            self.cache[symbol] = out
        return self.cache[symbol]


def run(days: int = 30) -> dict[str, Any]:
    rows = [json.loads(line) for line in LOG.read_text().splitlines() if '"projection"' in line]
    candles = _Candles(days + 3)
    table = arrival.load_table()
    seen, recs = set(), []
    for r in rows:
        if r["ts"] < candles.start:
            continue
        key = (r["symbol"], round(r["zone"]))
        if key in seen:                       # first projection per contract+zone (no double count)
            continue
        seen.add(key)
        asset = r["symbol"].split("-")[1]
        ex = _expiry_ts(r["symbol"])
        t0, end = int(r["ts"]), min(ex, candles.now)
        if end - t0 < 3600:
            continue
        under = sorted((t, b) for t, b in candles.get(asset + "USD").items() if t0 <= t <= end)
        rec: dict[str, Any] = {"symbol": r["symbol"], "kind": r["kind"],
                               "legacy": {"floor": r["floor"], "base": r["base"]},
                               "m": abs(r["zone"] - r["spot"]) / r["atr_1h"] if r.get("atr_1h") else None,
                               "censored_h": (end - t0) / 3600, "expired": ex <= candles.now}
        arr = arrival.lookup(rec["m"], r["hte"], table) if (table and rec["m"] is not None) else None
        if arr:
            rec["arrival"] = project_premium_arrival(r["strike"], r["kind"], r["iv"], r["hte"],
                                                     r["zone"], arr, r.get("iv_bump", 0.15))
        hit = first_touch(under, r["zone"], r["kind"])
        if hit:
            mk = candles.get("MARK:" + r["symbol"]).get(hit[0])
            if mk:
                rec.update(real=mk["close"], real_hi=mk["high"], hit_h=(hit[0] - t0) / 3600)
        rec["touched"] = hit is not None
        recs.append(rec)
    # P(arrive) is judged only on EXPIRED projections: an unexpired, untouched one isn't a "miss"
    # yet, and counting touched-but-unexpired ones alone would bias the touch rate upward.
    done = [r for r in recs if r["expired"]]
    res = {"projections": len(recs), "resolved": len(done),
           "touched": sum(r["touched"] for r in done),
           "p_arrive_pred": st.mean(r["arrival"]["p_hit"] for r in done if r.get("arrival"))
           if any(r.get("arrival") for r in done) else None,
           "legacy": score([r for r in recs if r.get("real") is not None], "legacy"),
           "arrival": score([r for r in recs if r.get("real") is not None and r.get("arrival")], "arrival")}
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description="rest@ accuracy on real Delta option marks (read-only).")
    ap.add_argument("--days", type=int, default=30)
    a = ap.parse_args()
    res = run(a.days)
    print("=" * 92)
    print("  REST@ ACCURACY — logged projections replayed against real Delta option marks")
    print("=" * 92)
    pa = res["p_arrive_pred"]
    print(f"  projections {res['projections']} · expired {res['resolved']} · zone touched before expiry "
          f"{res['touched']} ({res['touched'] / max(res['resolved'], 1):.0%})"
          + (f" · arrival model predicted {pa:.0%}" if pa is not None else ""))
    for name in ("legacy", "arrival"):
        s = res[name]
        if not s.get("n"):
            print(f"  {name:8s} no scored touches")
            continue
        print(f"  {name:8s} n={s['n']:3d}  rest@ filled {s['fill_rate']:.0%} (target ~75%)  ·  base error "
              f"{s['base_err_median_pct']:+.1f}% median  ·  rest@ = {s['rest_vs_real_when_filled']:.0%} of the real premium when filled")
    print("\n  If 'arrival' drifts far from ~75% filled, refit: python -m research.arrival fit --days 60\n")


if __name__ == "__main__":
    main()

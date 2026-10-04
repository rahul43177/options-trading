"""Forward CVD → next-return predictive probe — READ-ONLY, evidence-accumulating, honest-by-default.

Why this is forward-only (not a backtest):
  Signed cumulative volume delta (CVD) needs the taker side of every trade. Candles carry volume but
  NOT sign, and Delta's public `/trades` returns only ~50 recent trades with NO history. So CVD
  CANNOT be reconstructed retroactively — the only legitimate test of "does CVD predict the next
  move?" is to capture it forward over time and score it after the fact. This module does exactly
  that, matching the project's "accumulate evidence first, don't assume an edge" discipline.

Two subcommands:
  snapshot  — append one CVD + mid-price observation per asset to data/cvd_log.jsonl
              (run on a schedule, e.g. every 5-15 min, to build the forward sample).
  score     — pair each observation with the one >= horizon later (same asset), compute the forward
              return, and test whether CVD predicts it: directional hit-rate, Pearson correlation,
              and a permutation-null p-value. Returns COLLECTING until >= min_samples pairs exist.

Honest status: this MEASURES whether the CVD signal has forward edge; it does not assume one, and it
is deliberately NOT wired into research.engine / research.desk (CVD stays a CONFIRMATION-only read in
research.microstructure unless/until this probe shows a validated, out-of-sample edge). No fees,
single venue, one market regime — a significant result here is still a hypothesis, not a promise.

Read-only: no auth, no orders.  python -m research.cvd_probe snapshot   |   python -m research.cvd_probe score
"""
from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path
from typing import Any

from .config import ROOT, UNDERLYINGS
from .delta_api import DeltaPublicClient
from .microstructure import order_flow

LOG = ROOT / "data/cvd_log.jsonl"


def snapshot(assets: tuple[str, ...] = tuple(UNDERLYINGS),
             client: DeltaPublicClient | None = None,
             log_path: Path = LOG) -> list[dict[str, Any]]:
    """Capture one CVD + mid observation per asset and append to the forward log."""
    client = client or DeltaPublicClient()
    ts_us = int(time.time() * 1_000_000)
    rows: list[dict[str, Any]] = []
    for a in assets:
        sym = UNDERLYINGS[a]
        of = order_flow(sym, client=client)
        if not of or of.get("mid") is None:
            continue
        rows.append({
            "ts_us": ts_us, "asset": a, "symbol": sym, "mid": of["mid"],
            "cvd": of.get("cvd"), "cvd_net_pct": of.get("cvd_net_pct"),
            "signed_autocorr": of.get("signed_autocorr"),
            "trades_per_min": of.get("trades_per_min"), "flow_read": of.get("flow_read"),
        })
    if rows:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
    return rows


def load_log(log_path: Path = LOG) -> list[dict[str, Any]]:
    if not log_path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 3:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return 0.0
    return sxy / ((sxx * syy) ** 0.5)


def score_pairs(samples: list[dict[str, Any]], horizon_us: int,
                min_samples: int = 100, n_perm: int = 2000, seed: int = 0) -> dict[str, Any]:
    """Pair each sample with the earliest same-asset sample >= horizon later; test CVD→forward-return.

    Pure function (no network/IO) so it is unit-testable on synthetic tapes.
    """
    by: dict[str, list[dict[str, Any]]] = {}
    for s in samples:
        by.setdefault(s.get("asset", "?"), []).append(s)
    max_stale = horizon_us * 4  # don't pair across a long collection gap
    pairs: list[tuple[float, float]] = []
    for rows in by.values():
        rows = sorted(rows, key=lambda r: r.get("ts_us", 0))
        for i, r in enumerate(rows):
            x = r.get("cvd_net_pct")
            mid = r.get("mid")
            if x is None or not mid:
                continue
            fut = None
            for j in range(i + 1, len(rows)):
                dt = rows[j].get("ts_us", 0) - r.get("ts_us", 0)
                if dt >= horizon_us:
                    if dt <= max_stale and rows[j].get("mid"):
                        fut = rows[j]
                    break
            if not fut:
                continue
            ret = (fut["mid"] - mid) / mid
            pairs.append((float(x), float(ret)))
    n = len(pairs)
    if n < min_samples:
        return {"status": "COLLECTING", "n_pairs": n, "min_samples": min_samples,
                "note": (f"need >= {min_samples} forward-paired samples; have {n}. "
                         "Run `snapshot` on a schedule (e.g. every 5-15 min) to accumulate.")}
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    directional = [(x, y) for x, y in pairs if x != 0 and y != 0]
    hit = sum(1 for x, y in directional if (x > 0) == (y > 0)) / len(directional) if directional else 0.0
    r = _pearson(xs, ys)
    rng = random.Random(seed)
    ge = 0
    for _ in range(n_perm):
        shuffled = ys[:]
        rng.shuffle(shuffled)
        if abs(_pearson(xs, shuffled)) >= abs(r):
            ge += 1
    p = (ge + 1) / (n_perm + 1)
    edge = p < 0.05 and hit > 0.55
    return {
        "status": "EDGE_DETECTED" if edge else "NO_EDGE_YET",
        "n_pairs": n, "hit_rate": round(hit, 3), "pearson_r": round(r, 3), "perm_p": round(p, 4),
        "verdict": ("CVD shows a statistically-significant forward signal at this horizon — "
                    "still a hypothesis (one venue/regime, no fees); validate out-of-sample before trusting"
                    if edge else
                    "No significant forward edge from CVD at this horizon — keep it CONFIRMATION-only"),
    }


def score(horizon_min: float = 15.0, min_samples: int = 100, n_perm: int = 2000,
          log_path: Path = LOG) -> dict[str, Any]:
    samples = load_log(log_path)
    res = score_pairs(samples, int(horizon_min * 60 * 1_000_000), min_samples, n_perm)
    res["horizon_min"] = horizon_min
    res["total_observations"] = len(samples)
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description="Forward CVD -> next-return predictive probe (read-only).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("snapshot", help="append one CVD+mid observation per asset to the forward log")
    sp.add_argument("--assets", default=",".join(UNDERLYINGS))
    sc = sub.add_parser("score", help="score CVD->forward-return on the accumulated log")
    sc.add_argument("--horizon-min", type=float, default=15.0)
    sc.add_argument("--min-samples", type=int, default=100)
    sc.add_argument("--perm", type=int, default=2000)
    a = ap.parse_args()
    if a.cmd == "snapshot":
        assets = tuple(x.strip().upper() for x in a.assets.split(",") if x.strip())
        bad = set(assets) - set(UNDERLYINGS)
        if bad:
            ap.error(f"unsupported assets: {', '.join(sorted(bad))}")
        rows = snapshot(assets)
        print(f"logged {len(rows)} CVD observation(s): " +
              ", ".join(f"{r['asset']} mid={r['mid']} cvd_net={r['cvd_net_pct']}%" for r in rows))
    else:
        res = score(a.horizon_min, a.min_samples, a.perm)
        print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()

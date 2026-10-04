"""
Deterministic S/R zone builder — the rule-based "where are the levels" layer.

Why this exists (the bug it fixes): the options-entry workflow used to derive support/
resistance by hand from a TradingView read, then feed ONE support and ONE resistance to the
scanner/engine. Two problems followed. (1) It was non-deterministic — two runs of the same
market could pick different levels, so the engine's `◄ BEST` (which is only as good as the
zones fed in) was not reproducible. (2) A single zone choice could silently HIDE a valid
setup: anchor support to a deep structural shelf and the trend-aligned puts just below the
*nearer* support never get scanned, so the run wrongly reports "no setup".

This module addresses both. It computes, deterministically and from the SAME Delta public
candles the scanner already uses (one price source, no TradingView, no keys), two bands per
side:

  * NEAR       — the nearest dynamic cluster to spot (1h EMA20/50/200, 1h/4h Bollinger edge,
                 POC). This is the level price reaches first.
  * STRUCTURAL — the next cluster further out (classic daily pivots, the far Bollinger edge,
                 value-area edge). More cushion, less likely to be tagged.

The caller (the skill) scans BOTH bands per side and may only report "no setup" after both
come back empty — never off a single zone. TradingView stays the bias/eyes cross-check; the
precise levels come from here so the Delta-priced scanner, engine and desk all see one set of
zones.

Honest limits. Deterministic means "same candle snapshot in -> same zones out"; it is NOT
identical across runs, because the inputs are live (rolling 1h/4h candles incl. the forming
bar, a 30d volume profile, spot) and move between runs. Only the two nearest clusters per side
are kept, so a relevant third cluster, a failed volume-profile fetch, stale input, or a scanner
filter can still produce a false "no setup". This substantially REDUCES false negatives from
picking one zone by hand; it does not make them impossible.

Read-only: no auth, no orders.  python -m research.zones --asset BTC   (--json for machine use)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import time
from typing import Any

from .config import UNDERLYINGS
from .delta_api import DeltaPublicClient
from .microstructure import volume_profile
from .perp_analytics import _prep, ema

# Per-asset geometry. tol_frac = how close (as a fraction of spot) two levels must sit to join
# one band; round_to = tick the arrival level is rounded to for a clean --resistance/--support.
ASSET_GEOM = {
    "BTC": {"tol_frac": 0.0045, "round_to": 100.0},
    "ETH": {"tol_frac": 0.0045, "round_to": 10.0},
}
_DEFAULT_GEOM = {"tol_frac": 0.0045, "round_to": 1.0}


def classic_pivots(high: float, low: float, close: float) -> dict[str, float]:
    """Floor-trader pivots from one completed bar's H/L/C. Pure (unit-tested)."""
    p = (high + low + close) / 3.0
    rng = high - low
    return {
        "P": p,
        "R1": 2 * p - low, "S1": 2 * p - high,
        "R2": p + rng, "S2": p - rng,
        "R3": high + 2 * (p - low), "S3": low - 2 * (high - p),
    }


def bollinger(values: list[float], n: int = 20, k: float = 2.0) -> tuple[float, float, float] | None:
    """(lower, middle, upper) Bollinger band from the last n values. Pure (unit-tested)."""
    if len(values) < n:
        return None
    w = values[-n:]
    mid = sum(w) / n
    var = sum((v - mid) ** 2 for v in w) / n  # population stdev, matching most charting defaults
    sd = var ** 0.5
    return (mid - k * sd, mid, mid + k * sd)


def cluster_levels(spot: float, candidates: list[tuple[float, str]], side: str,
                   tol_frac: float, max_span_frac: float | None = None) -> list[dict[str, Any]]:
    """Greedy-cluster same-side levels into bands ordered nearest-to-spot first. Pure (unit-tested).

    `candidates` is (price, label). `side` is "resistance" (keep price>spot) or "support"
    (keep price<spot). A level joins the current band when it is within tol_frac*spot of the
    band's far edge AND the band's total width stays within max_span_frac*spot. The width cap
    defeats single-linkage CHAINING — without it a long run of near-adjacent levels drifts into
    one huge band and swallows the intermediate shelf that should be the STRUCTURAL zone
    (default max_span_frac = 2*tol_frac). Each band carries lo/hi, member labels, the distance-%
    of its nearest edge, and the price-arrival level (the edge price reaches first: resistance
    -> lo, support -> hi).
    """
    tol = tol_frac * spot
    max_span = (max_span_frac if max_span_frac is not None else 2.0 * tol_frac) * spot
    kept = [(p, lab) for p, lab in candidates
            if (p > spot if side == "resistance" else p < spot)]
    # nearest-to-spot first
    kept.sort(key=lambda r: abs(r[0] - spot))
    bands: list[dict[str, Any]] = []
    cur: list[tuple[float, str]] = []
    anchor = far = None  # anchor = nearest-to-spot member; far = edge growing away from spot
    for p, lab in kept:
        if not cur:
            cur = [(p, lab)]
            anchor = far = p
        elif abs(p - far) <= tol and abs(p - anchor) <= max_span:
            cur.append((p, lab))
            far = p
        else:
            bands.append(_band(cur, side, spot))
            cur = [(p, lab)]
            anchor = far = p
    if cur:
        bands.append(_band(cur, side, spot))
    return bands


def _band(members: list[tuple[float, str]], side: str, spot: float) -> dict[str, Any]:
    lo = min(p for p, _ in members)
    hi = max(p for p, _ in members)
    arrival = lo if side == "resistance" else hi            # edge price reaches first
    nearest_edge = lo if side == "resistance" else hi
    return {
        "lo": round(lo, 2), "hi": round(hi, 2),
        "arrival": round(arrival, 2),
        "dist_pct": round(abs(nearest_edge - spot) / spot * 100, 2),
        "sources": [lab for _, lab in sorted(members, key=lambda r: r[0])],
        "n_sources": len(members),
    }


def _round_arrival(x: float, step: float, side: str) -> float:
    """Round an arrival level to a clean tick AWAY from spot, never toward it. Pure (unit-tested).

    Resistance rounds UP (ceil) and support rounds DOWN (floor). Nearest-rounding could pull a
    level across spot (ETH spot 2,700.20, resistance 2,704.23 -> 2,700, i.e. below spot) and hand
    the call and put scans the same zone. Rounding away also keeps the engine's buffer gate
    conservative: the strike must clear the rounded level, which is never closer to spot.
    """
    if side == "resistance":
        return math.ceil(x / step) * step
    if side == "support":
        return math.floor(x / step) * step
    raise ValueError(f"side must be 'resistance' or 'support', got {side!r}")


def build_zones(asset: str, client: DeltaPublicClient | None = None,
                spot_override: float | None = None) -> dict[str, Any]:
    """Fetch Delta candles and assemble NEAR + STRUCTURAL support/resistance bands.

    Returns a dict with spot, the raw levels used, and per-side {"near", "structural"} bands
    (structural is None if only one cluster exists on that side). All levels are Delta-derived
    so they match the scanner's price source; pass spot_override to pin spot to the scanner's.
    """
    sym = UNDERLYINGS[asset]
    geom = ASSET_GEOM.get(asset, _DEFAULT_GEOM)
    client = client or DeltaPublicClient()
    now = int(time.time())
    b1h = _prep(client.candles(sym, "1h", now - 30 * 86400, now))
    b4h = _prep(client.candles(sym, "4h", now - 30 * 86400, now))
    b1d = _prep(client.candles(sym, "1d", now - 12 * 86400, now))
    closes1h = [b["close"] for b in b1h]
    spot = spot_override if spot_override is not None else closes1h[-1]

    e20, e50, e200 = ema(closes1h, 20), ema(closes1h, 50), ema(closes1h, 200)
    bb1h = bollinger(closes1h, 20)
    bb4h = bollinger([b["close"] for b in b4h], 20)
    # classic pivots from the last COMPLETED daily bar (the prior period, not today's partial)
    piv_bar = b1d[-2] if len(b1d) >= 2 else b1d[-1]
    piv = classic_pivots(piv_bar["high"], piv_bar["low"], piv_bar["close"])
    vp = volume_profile(sym, "1h", 30, client=client) or {}

    levels: list[tuple[float, str]] = []
    for v, lab in ((e20, "1h EMA20"), (e50, "1h EMA50"), (e200, "1h EMA200")):
        if v is not None:
            levels.append((v, lab))
    if bb1h:
        levels += [(bb1h[0], "1h BB-lower"), (bb1h[2], "1h BB-upper")]
    if bb4h:
        levels += [(bb4h[0], "4h BB-lower"), (bb4h[2], "4h BB-upper")]
    levels += [(piv["R1"], "pivot R1"), (piv["R2"], "pivot R2"), (piv["R3"], "pivot R3"),
               (piv["S1"], "pivot S1"), (piv["S2"], "pivot S2"), (piv["S3"], "pivot S3")]
    for key, lab in (("poc", "POC"), ("vah", "VAH"), ("val", "VAL")):
        if vp.get(key) is not None:
            levels.append((float(vp[key]), lab))

    out: dict[str, Any] = {
        "asset": asset, "symbol": sym,
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "spot": round(spot, 2),
        "spot_source": "scanner_override" if spot_override is not None else "last_1h_close",
        "buffer_hint": f"per-asset default (see config.DEFAULT_BUFFER[{asset!r}])",
    }
    for side in ("resistance", "support"):
        bands = cluster_levels(spot, levels, side, geom["tol_frac"])
        near = bands[0] if bands else None
        structural = bands[1] if len(bands) > 1 else None
        for band in (near, structural):
            if band:
                band["arrival_rounded"] = _round_arrival(band["arrival"], geom["round_to"], side)
        out[side] = {"near": near, "structural": structural}
    return out


def _fmt_band(tag: str, band: dict[str, Any] | None, flag: str) -> str:
    if not band:
        return f"    {tag:<11} (none found this side)"
    src = ", ".join(band["sources"])
    return (f"    {tag:<11} {band['lo']:,.0f}–{band['hi']:,.0f}  "
            f"(arrival {band['arrival_rounded']:,.0f}, {band['dist_pct']:.2f}% {flag})  "
            f"[{band['n_sources']}x: {src}]")


def main() -> None:
    ap = argparse.ArgumentParser(description="Deterministic near+structural S/R bands (read-only).")
    ap.add_argument("--asset", choices=sorted(UNDERLYINGS), default="BTC")
    ap.add_argument("--spot", type=float, default=None, help="pin spot to the scanner's (else last 1h close)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args()
    z = build_zones(a.asset, spot_override=a.spot)
    if a.json:
        print(json.dumps(z, indent=2))
        return
    print("=" * 92)
    print(f"  {a.asset} S/R ZONES — deterministic (Delta candles) | spot {z['spot']:,.0f} "
          f"({z['spot_source']}) | {z['generated_at_utc']}")
    print("  NEAR = reached first (scan this). STRUCTURAL = more cushion (scan this too). "
          "Never call 'no setup' off one zone.")
    print("=" * 92)
    print("  RESISTANCE (CALL-short watch — above spot):")
    print(_fmt_band("near", z["resistance"]["near"], "above"))
    print(_fmt_band("structural", z["resistance"]["structural"], "above"))
    print("  SUPPORT (PUT-short watch — below spot):")
    print(_fmt_band("near", z["support"]["near"], "below"))
    print(_fmt_band("structural", z["support"]["structural"], "below"))
    r_near = z["resistance"]["near"]
    s_near = z["support"]["near"]
    print("\n  Feed arrival levels to the scanner, e.g.:")
    if r_near and s_near:
        print(f"    python -m research.entry_scan --asset {a.asset} --side call "
              f"--zone {r_near['arrival_rounded']:,.0f}   (then --side put --zone {s_near['arrival_rounded']:,.0f})")
    print("  Scan the STRUCTURAL arrival too; report 'no setup' only if BOTH bands are empty.\n")


if __name__ == "__main__":
    main()

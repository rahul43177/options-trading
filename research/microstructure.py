"""Volume Profile + live order-flow read — READ-ONLY context/confirmation, NOT a predictive edge.

Why this exists, and its honest status (measured, not assumed):
  A causal rolling-VP touch-rejection test over 40-120 days of BTC/ETH/SOL/XRP candles found that
  price touching a volume-profile level (POC/VAH/VAL) does NOT reverse more often than it reverses
  at a RANDOM price level (best case BTC-1h p~0.18, not significant; most symbols no edge or worse).
  So volume profile is used here ONLY as *context* — where the most business was done (POC, a
  fair-value magnet), the value-area edges (VAH/VAL, range boundaries), high-volume shelves (HVN)
  and thin low-volume gaps (LVN, price travels fast) — to corroborate pivot/SMC zones, never as a
  standalone reversal trigger.

  Order flow (aggressor delta from the taker side of recent trades + resting-book imbalance/walls)
  is REAL but, on Delta's public API, only a thin live snapshot: /trades returns ~50 recent trades
  and /l2orderbook a single depth snapshot. There is no public trade history, so CVD/footprint
  CANNOT be backtested here. It is therefore a live CONFIRMATION tiebreaker — "at a level price has
  already reached, are aggressors actually flipping?" — never a predictor, and it must never
  override the higher-timeframe trend. Resting liquidity can be spoofed/pulled; weight it lightly.

  To ever VALIDATE order flow, capture /trades forward over time (a collector enhancement) and
  score signed cumulative delta against realized reversals — accumulate the evidence, don't assume.

Read-only: no auth, no orders.  python -m research.microstructure --symbol BTCUSD   (or --asset BTC)
"""
from __future__ import annotations

import argparse
import json
from typing import Any

from .config import UNDERLYINGS
from .delta_api import DeltaPublicClient


def _f(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _signed_flow_metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Order-flow QUALITY from the taker-classified tape — read-only, CONFIRMATION-only.

    Operationalises three results from the Malhotra (2018) microstructure phase, adapted to the
    thin public snapshot (so: a tiebreaker, never a predictor, never backtested, never a reason to
    fade the higher-timeframe trend):
      * Lee-Ready (1991) trade signing — Delta already tags the taker side, so each trade is a
        signed volume (+aggressive buy / -aggressive sell); the running sum is cumulative volume
        delta (CVD). We also split the window early-vs-late to see if the flow is BUILDING or
        EXHAUSTING.
      * Hasbrouck (1991) — net order flow carries information. We read whether it is
        one-sided-and-persistent (informative) vs two-sided (absorption/noise).
      * Dufour & Engle (2000) — faster trade arrival + persistent signed trades (lag-1 positive
        autocorrelation) imply more informed presence and REDUCED effective liquidity: a caution
        against resting a passive limit into the push.

    Pure function (no network) so it is unit-testable on a crafted tape.
    """
    seq: list[tuple[float, float]] = []
    for t in trades:
        sz = _f(t.get("size")) or 0.0
        ts = _f(t.get("timestamp"))
        if t.get("buyer_role") == "taker":
            s = sz
        elif t.get("seller_role") == "taker":
            s = -sz
        else:
            s = 0.0
        seq.append((ts if ts is not None else 0.0, s))
    seq.sort(key=lambda r: r[0])
    n = len(seq)
    if n == 0:
        return {"cvd": 0, "cvd_net_pct": 0.0, "cvd_early": 0, "cvd_late": 0,
                "signed_autocorr": 0.0, "trades_per_min": None, "intensity": "n/a",
                "flow_read": "no trades", "flow_note": "no tape to read"}
    cvd = sum(s for _, s in seq)
    half = n // 2
    early = sum(s for _, s in seq[:half])
    late = sum(s for _, s in seq[half:])
    # signed-trade lag-1 autocorrelation (persistence of the aggressor side)
    signs = [(1 if s > 0 else (-1 if s < 0 else 0)) for _, s in seq]
    nz = [x for x in signs if x != 0]
    autocorr = 0.0
    if len(nz) > 3:
        m = sum(nz) / len(nz)
        den = sum((x - m) ** 2 for x in nz)
        if den > 0:
            autocorr = sum((nz[i] - m) * (nz[i + 1] - m) for i in range(len(nz) - 1)) / den
    # trade intensity (Dufour-Engle): trades/min + is arrival speeding up (late dt < early dt)?
    ts_list = [ts for ts, _ in seq if ts]
    tpm = None
    dt_trend = None
    if len(ts_list) > 5:
        span_s = (ts_list[-1] - ts_list[0]) / 1e6
        if span_s > 0:
            tpm = len(ts_list) / (span_s / 60.0)
        dts = [(ts_list[i + 1] - ts_list[i]) / 1e6 for i in range(len(ts_list) - 1)]
        if len(dts) >= 6:
            h = len(dts) // 2
            a = sum(dts[:h]) / h
            b = sum(dts[h:]) / (len(dts) - h)
            if a > 0:
                dt_trend = (b - a) / a  # < 0  => arrivals speeding up
    tot = sum(abs(s) for _, s in seq) or 1.0
    net_pct = cvd / tot * 100.0
    side = "buyers" if cvd > 0 else ("sellers" if cvd < 0 else "balanced")
    early_side = "buyers" if early > 0 else ("sellers" if early < 0 else "balanced")
    # Net-flow % is the primary one-sidedness gate (robust even when a perfectly one-way tape makes
    # the sign-autocorrelation degenerate to 0); autocorrelation corroborates a merely-moderate net.
    one_sided = abs(net_pct) >= 45 and (abs(autocorr) >= 0.15 or abs(net_pct) >= 60)
    # Exhaustion: a real early directional push (>=30% of window volume) that reverses or dies late,
    # while the overall net is NOT strongly one-sided -> early reversal tell (names the fading side).
    real_early = abs(early) >= 0.30 * tot
    faded = (early > 0 and late <= 0.30 * early) or (early < 0 and late >= 0.30 * early)
    exhausting = (not one_sided) and real_early and faded and early_side != "balanced"
    if one_sided:
        flow_read = f"ONE-SIDED {side.upper()} PUSH (persistent)"
        flow_note = ("informed-looking one-way flow — don't rest a passive limit into it; "
                     "confirms continuation, never a reason to fade the HTF trend")
    elif exhausting:
        flow_read = f"FLOW FADING ({early_side} exhausting)"
        flow_note = (f"the early {early_side} push has cooled/reversed — early reversal tell "
                     "(confirm on the chart; never fade the HTF trend)")
    else:
        flow_read = "TWO-SIDED / absorption"
        flow_note = "no dominant informed push — consistent with a level being worked/held"
    intensity_tag = "steady"
    if dt_trend is not None:
        if dt_trend <= -0.25:
            intensity_tag = "RISING (trades speeding up — more informed presence, thinner effective liquidity)"
        elif dt_trend >= 0.25:
            intensity_tag = "FALLING (trades slowing — cooling off)"
    return {
        "cvd": round(cvd), "cvd_net_pct": round(net_pct, 1),
        "cvd_early": round(early), "cvd_late": round(late),
        "signed_autocorr": round(autocorr, 2),
        "trades_per_min": round(tpm, 1) if tpm is not None else None,
        "intensity": intensity_tag,
        "flow_read": flow_read, "flow_note": flow_note,
    }


def volume_profile(symbol: str, resolution: str = "1h", days: int = 30, bins: int = 100,
                   client: DeltaPublicClient | None = None) -> dict[str, Any] | None:
    """POC / value-area / HVN / LVN from trailing candles. Context levels only (see module docstring)."""
    import time
    client = client or DeltaPublicClient()
    now = int(time.time())
    try:
        bars = client.candles(symbol, resolution, now - days * 86400, now)
    except Exception:
        return None
    rows = []
    for b in bars:
        lo, hi, vol = _f(b.get("low")), _f(b.get("high")), _f(b.get("volume"))
        if None in (lo, hi, vol):
            continue
        rows.append((lo, hi, vol))
    if len(rows) < 50:
        return None
    lo = min(r[0] for r in rows)
    hi = max(r[1] for r in rows)
    if hi <= lo:
        return None
    step = (hi - lo) / bins
    vol = [0.0] * bins
    for blo, bhi, v in rows:
        a = max(0, int((blo - lo) / step))
        z = min(bins - 1, int((bhi - lo) / step))
        share = v / (z - a + 1)
        for k in range(a, z + 1):
            vol[k] += share
    poc_i = max(range(bins), key=lambda k: vol[k])
    total = sum(vol)
    target = total * 0.70
    lo_i = hi_i = poc_i
    acc = vol[poc_i]
    while acc < target and (lo_i > 0 or hi_i < bins - 1):
        down = vol[lo_i - 1] if lo_i > 0 else -1
        up = vol[hi_i + 1] if hi_i < bins - 1 else -1
        if up >= down:
            hi_i += 1; acc += vol[hi_i]
        else:
            lo_i -= 1; acc += vol[lo_i]
    price = lambda i: lo + (i + 0.5) * step
    hvn = [price(i) for i in sorted(range(bins), key=lambda k: vol[k], reverse=True)[:5]]
    # Low-volume nodes (thin gaps) with meaningful volume bins on both sides — price travels fast here.
    lvn = [price(i) for i in range(1, bins - 1)
           if vol[i] < 0.35 * vol[poc_i] and vol[i] < vol[i - 1] and vol[i] < vol[i + 1]]
    return {
        "symbol": symbol, "resolution": resolution, "days": days,
        "poc": round(price(poc_i), 2), "vah": round(price(hi_i), 2), "val": round(price(lo_i), 2),
        "hvn": [round(x, 2) for x in hvn], "lvn": [round(x, 2) for x in sorted(lvn)],
        "range_lo": round(lo, 2), "range_hi": round(hi, 2),
    }


def order_flow(symbol: str, book_levels: int = 25, wall_pct: float = 3.0,
               client: DeltaPublicClient | None = None) -> dict[str, Any] | None:
    """Live aggressor delta (taker side of recent trades) + resting-book imbalance and walls.

    Confirmation tiebreaker only — thin (~50 trades), snapshot, spoofable, not backtestable.
    """
    client = client or DeltaPublicClient()
    try:
        trades = client.get(f"/trades/{symbol}")["result"]
        ob = client.get(f"/l2orderbook/{symbol}")["result"]
    except Exception:
        return None
    if not trades or not ob.get("buy") or not ob.get("sell"):
        return None
    # taker is the aggressor: buyer=taker -> aggressive BUY; seller=taker -> aggressive SELL
    buy = sum(_f(t["size"]) or 0 for t in trades if t.get("buyer_role") == "taker")
    sell = sum(_f(t["size"]) or 0 for t in trades if t.get("seller_role") == "taker")
    tot = buy + sell
    delta = buy - sell
    delta_pct = (delta / tot * 100) if tot else 0.0
    best_bid = _f(ob["buy"][0]["price"]); best_ask = _f(ob["sell"][0]["price"])
    mid = (best_bid + best_ask) / 2 if (best_bid and best_ask) else None
    bid_sz = sum(_f(x["size"]) or 0 for x in ob["buy"][:book_levels])
    ask_sz = sum(_f(x["size"]) or 0 for x in ob["sell"][:book_levels])
    imb = (bid_sz - ask_sz) / (bid_sz + ask_sz) if (bid_sz + ask_sz) else 0.0
    # Largest resting wall within wall_pct of mid, each side (passive S/R — treat as soft).
    def wall(side_rows):
        best = None
        for x in side_rows:
            p, s = _f(x["price"]), _f(x["size"])
            if p is None or s is None or mid is None:
                continue
            if abs(p - mid) / mid * 100 > wall_pct:
                break
            if best is None or s > best[1]:
                best = (p, s)
        return best
    bid_wall = wall(ob["buy"]); ask_wall = wall(ob["sell"])
    rel_spread = round((best_ask - best_bid) / mid * 100, 3) if (best_bid and best_ask and mid) else None
    flow = _signed_flow_metrics(trades)  # Lee-Ready CVD + Hasbrouck/Dufour-Engle quality (confirmation-only)
    return {
        "symbol": symbol, "n_trades": len(trades),
        "aggr_buy": round(buy), "aggr_sell": round(sell),
        "delta": round(delta), "delta_pct": round(delta_pct, 1),
        "aggressor": "BUYERS" if delta > 0 else ("SELLERS" if delta < 0 else "balanced"),
        "mid": mid,
        "book_imbalance": round(imb, 2),
        "book_tag": "bid-heavy (passive support)" if imb > 0.15 else (
            "ask-heavy (passive supply)" if imb < -0.15 else "balanced"),
        "bid_wall": None if not bid_wall else {"price": round(bid_wall[0], 2), "size": round(bid_wall[1])},
        "ask_wall": None if not ask_wall else {"price": round(ask_wall[0], 2), "size": round(ask_wall[1])},
        "rel_spread_pct": rel_spread,
        **flow,
    }


def read(symbol: str, resolution: str = "1h", days: int = 30) -> dict[str, Any]:
    client = DeltaPublicClient()
    return {
        "symbol": symbol,
        "volume_profile": volume_profile(symbol, resolution, days, client=client),
        "order_flow": order_flow(symbol, client=client),
        "status": "CONTEXT_AND_CONFIRMATION_ONLY — not a validated predictive edge (see module docstring)",
    }


def _print_human(res: dict[str, Any]) -> None:
    vp, of = res["volume_profile"], res["order_flow"]
    print("=" * 92)
    print(f"  MICROSTRUCTURE — {res['symbol']}   (READ-ONLY context + live confirmation; NOT a predictive edge)")
    print("=" * 92)
    if vp:
        print(f"  VOLUME PROFILE  ({vp['resolution']}, {vp['days']}d)   range {vp['range_lo']:,.0f}-{vp['range_hi']:,.0f}")
        print(f"    POC (fair-value magnet)   {vp['poc']:,.2f}")
        print(f"    Value area  VAL {vp['val']:,.2f}  →  VAH {vp['vah']:,.2f}   (range boundaries)")
        print(f"    HVN shelves  {', '.join(f'{x:,.0f}' for x in vp['hvn'])}")
        if vp["lvn"]:
            print(f"    LVN thin gaps (price travels fast)  {', '.join(f'{x:,.0f}' for x in vp['lvn'][:6])}")
    else:
        print("  VOLUME PROFILE  unavailable (candle pull failed / too few bars)")
    print()
    if of:
        print(f"  LIVE ORDER FLOW  (last {of['n_trades']} trades + {25}-level book snapshot)")
        print(f"    Aggressor delta  buy {of['aggr_buy']:,} / sell {of['aggr_sell']:,}  "
              f"→  {of['delta']:+,} ({of['delta_pct']:+.0f}% of vol)  →  {of['aggressor']} aggressive")
        print(f"    Resting book imbalance  {of['book_imbalance']:+.2f}  ({of['book_tag']})")
        if of["bid_wall"]:
            print(f"    Bid wall (soft support)  {of['bid_wall']['size']:,} @ {of['bid_wall']['price']:,.2f}")
        if of["ask_wall"]:
            print(f"    Ask wall (soft supply)   {of['ask_wall']['size']:,} @ {of['ask_wall']['price']:,.2f}")
        # --- order-flow QUALITY (Lee-Ready CVD + Hasbrouck info-content + Dufour-Engle intensity) ---
        if "flow_read" in of:
            tpm = of.get("trades_per_min")
            tpm_s = f"{tpm:.1f}/min" if tpm is not None else "n/a"
            print(f"    CVD (signed vol)  {of['cvd']:+,} ({of['cvd_net_pct']:+.0f}% net)  "
                  f"early {of['cvd_early']:+,} → late {of['cvd_late']:+,}")
            print(f"    Flow quality  {of['flow_read']}   persistence(autocorr) {of['signed_autocorr']:+.2f}")
            print(f"    Trade intensity  {tpm_s} · {of['intensity']}")
            if of.get("rel_spread_pct") is not None:
                print(f"    Quote spread  {of['rel_spread_pct']:.3f}%  "
                      f"(wider = more order-processing/inventory/adverse-selection cost)")
            print(f"    → {of['flow_note']}")
    else:
        print("  LIVE ORDER FLOW  unavailable (/trades or /l2orderbook failed)")
    print()
    print("  Use VP to place/confirm ZONES, order flow to CONFIRM a reversal at a level price already")
    print("  reached. Flow quality = Lee-Ready signing + Hasbrouck info-content + Dufour-Engle intensity;")
    print("  ~50 trades + one snapshot (spoofable) — a tiebreaker, never a trigger, not backtested,")
    print("  and never a reason to fade the higher-timeframe trend. Read-only — you place every order.\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Read-only volume-profile + live order-flow read.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--symbol", help="perp/candle symbol, e.g. BTCUSD, ETHUSD, SOLUSD")
    g.add_argument("--asset", choices=sorted(UNDERLYINGS), help="maps to its spot symbol via config.UNDERLYINGS")
    ap.add_argument("--resolution", default="1h")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    symbol = a.symbol or UNDERLYINGS[a.asset]
    res = read(symbol, a.resolution, a.days)
    if a.json:
        print(json.dumps(res, indent=2, default=str))
    else:
        _print_human(res)


if __name__ == "__main__":
    main()

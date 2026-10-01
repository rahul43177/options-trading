"""
Read-only PERPETUAL FUTURES setup screener (Delta Exchange India) — BOTH sides.

Companion to the options entry_scan. Where entry_scan shows the option chain,
this sweeps the whole liquid perp universe in ONE public-API call and ranks the
alts whose price + microstructure line up for a reversal trade — the names worth
taking to TradingView for confirmation. It never trades and never needs a key: it
only reads public /tickers.

The score is a *screen*, not a signal. With --side short it answers "which liquid
alt perps are stretched into their highs with over-leveraged longs?"; with
--side long, "which are pressed into their lows with over-leveraged shorts (squeeze
fuel + long carry)?" You still have to confirm the reversal + the higher-timeframe
trend on the chart before entering either way.

Signals per coin (all from the live ticker, institutional tells):
  rngPos   position in the 24h range, (mark-lo)/(hi-lo). ~1 = jammed at the high
           (short tell); ~0 = pinned at the low (long tell).
  chg24    24h % change. Big + = extended rally (short); big - = extended dump (long).
  fund%    funding rate. + = longs pay shorts (crowded longs -> short carry);
           - = shorts pay longs (crowded shorts -> long carry + squeeze fuel).
  basis%   perp premium to spot, (mark-spot)/spot. Rich premium = leveraged longs
           (short); deep discount = leveraged shorts (long).
  OIΔ%     6h change in OI value / OI value. New OI into an extreme = trap fuel
           (works for both sides — longs trapped at a high, shorts at a low).
Hard liquidity gate (turnover + OI in USD) keeps illiquid books out — you can only
trade what you can exit.

    python -m research.futures_screen                        # short screen (default)
    python -m research.futures_screen --side long            # long screen
    python -m research.futures_screen --side long --max-rng-pos 0.4   # only near the low
    python -m research.futures_screen --top 20 --min-turnover 2e6
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os

from .delta_api import DeltaPublicClient
from . import perp_analytics as pa


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _clip01(x: float) -> float:
    return 0.0 if x < 0 else (1.0 if x > 1 else x)


# score weights. Kept explicit so the ranking is auditable. RS = 24h performance RELATIVE to
# BTC (cross-sectional momentum: long the leader / short the laggard) — the rel-value edge the
# desk-lens calls for but the raw absolute-chg24 term got backwards. Sum = 1.00.
W_RNG, W_CHG, W_FUND, W_OI, W_BASIS, W_RS = 0.30, 0.10, 0.18, 0.12, 0.10, 0.20


def load_perps():
    rows = []
    for t in DeltaPublicClient().get(
        "/tickers", {"contract_types": "perpetual_futures"}
    )["result"]:
        sym = str(t.get("symbol", ""))
        mark = _f(t.get("mark_price"))
        hi = _f(t.get("mark_high_24h"))
        lo = _f(t.get("mark_low_24h"))
        spot = _f(t.get("spot_price"))
        if not (mark and hi and lo and hi > lo):
            continue
        q = t.get("quotes", {}) or {}
        bid, ask = _f(q.get("best_bid")), _f(q.get("best_ask"))
        oi_usd = _f(t.get("oi_value_usd")) or 0.0
        oi_chg = _f(t.get("oi_change_usd_6h")) or 0.0
        rows.append({
            "symbol": sym,
            "coin": t.get("underlying_asset_symbol") or sym.replace("USD", ""),
            "top_tag": t.get("top_tag"),
            "mark": mark, "spot": spot, "hi": hi, "lo": lo,
            "chg24": _f(t.get("mark_change_24h")) or 0.0,
            "funding": _f(t.get("funding_rate")) or 0.0,
            "basis_pct": ((mark - spot) / spot * 100) if (spot and spot > 0) else 0.0,
            "rng_pos": _clip01((mark - lo) / (hi - lo)),
            "oi_usd": oi_usd,
            "oi_growth": (oi_chg / oi_usd) if oi_usd > 0 else 0.0,
            "turnover_usd": _f(t.get("turnover_usd")) or 0.0,
            "spread_pct": ((ask - bid) / ((ask + bid) / 2) * 100)
            if (bid and ask and ask > 0) else None,
            "leverage": _f(t.get("leverage")),
        })
    return rows


def score(r: dict, side: str = "short") -> float:
    rs = r.get("rs_btc", 0.0)                             # coin 24h% - BTC 24h% (vs-BTC strength)
    if side == "long":
        s = (
            W_RNG * (1.0 - r["rng_pos"])                 # near the 24h LOW = near support
            + W_CHG * _clip01(-r["chg24"] / 20.0)        # small mean-reversion tell (a pullback, not a crash)
            + W_RS * _clip01(rs / 6.0)                    # reward RELATIVE STRENGTH: holding up vs BTC = resilient, not a knife
            + W_FUND * _clip01(-r["funding"] / 0.05)     # -0.05%+/interval = shorts pay longs
            + W_OI * _clip01(r["oi_growth"] / 0.30)      # +30% OI in 6h into the low = squeeze fuel
            + W_BASIS * _clip01(-r["basis_pct"] / 0.10)  # 0.10% discount = perp sold below spot
        )
    else:
        s = (
            W_RNG * r["rng_pos"]
            + W_CHG * _clip01(r["chg24"] / 20.0)        # small mean-reversion tell (extended into the high)
            + W_RS * _clip01(-rs / 6.0)                  # reward RELATIVE WEAKNESS: lagging BTC = laggard rolling over, cleaner short
            + W_FUND * _clip01(r["funding"] / 0.05)     # 0.05%+/interval = very crowded
            + W_OI * _clip01(r["oi_growth"] / 0.30)     # +30% OI in 6h = heavy new positioning
            + W_BASIS * _clip01(r["basis_pct"] / 0.10)  # 0.10% premium = rich perp
        )
    return s * 100.0


def flags(r: dict, side: str = "short") -> str:
    out = []
    rs = r.get("rs_btc", 0.0)
    if side == "long":
        if r["rng_pos"] <= 0.15:
            out.append("atLow")
        if rs >= 1:
            out.append("RS+")       # holding up vs BTC — resilient leader, the long you want
        elif rs <= -5:
            out.append("knife?")    # far weaker than BTC — a breakdown, not a pullback
        if r["funding"] <= -0.03:
            out.append("🔥fund")   # shorts pay longs heavily — crowded shorts, long carry
        elif r["funding"] > 0:
            out.append("posFund")  # longs PAY here — carry works against a long
        if r["oi_growth"] >= 0.20:
            out.append("OI↑")
        if r["chg24"] <= -8:
            out.append("extended")  # extended dump
        if r["turnover_usd"] < 2_000_000:
            out.append("thin")
    else:
        if r["rng_pos"] >= 0.85:
            out.append("atHigh")
        if rs <= -1:
            out.append("RS-")        # lagging BTC — a laggard rolling over, the cleaner short
        elif rs >= 3:
            out.append("leadsMkt")   # leading the tape up — momentum risk to a short
        if r["funding"] >= 0.03:
            out.append("🔥fund")
        elif r["funding"] < 0:
            out.append("negFund")   # shorts PAY here — carry works against you
        if r["oi_growth"] >= 0.20:
            out.append("OI↑")
        if r["chg24"] >= 8:
            out.append("extended")
        if r["turnover_usd"] < 2_000_000:
            out.append("thin")
    return " ".join(out)


def regime_line(client) -> tuple[str, str]:
    """Best-effort BTC regime from the Delta feed (reuses macro_pulse). Never raises."""
    try:
        from . import macro_pulse as mp
        snap = mp.snapshot(client, ["BTC", "ETH"])
        btc, eth = snap.get("BTCUSD"), snap.get("ETHUSD")
        if not btc:
            return ("UNKNOWN", "regime unavailable")
        btc_tr = mp.trend(client, "BTCUSD")
        alt_lean = (eth["chg24"] - btc["chg24"]) if (eth and btc) else 0.0
        lab, why = mp.regime(btc, alt_lean, btc_tr)
        d1 = (btc_tr.get("1D") or {}).get("dir", "?")
        h4 = (btc_tr.get("4h") or {}).get("dir", "?")
        return (lab, f"BTC {btc['mark']:,.0f} 1D {d1}/4h {h4} · alt-lean {alt_lean:+.2f}% · {why}")
    except Exception:
        return ("UNKNOWN", "regime unavailable")


def log_run(side: str, regime: str, rows: list[dict]) -> str | None:
    """Append the enriched run to research/logs/futures_screen.jsonl for later
    evidence-based weight tuning. Never breaks the scan."""
    try:
        d = os.path.join(os.path.dirname(__file__), "logs")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "futures_screen.jsonl")
        rec = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "side": side, "regime": regime,
            "candidates": [
                {k: r.get(k) for k in (
                    "coin", "mark", "chg24", "rs_btc", "rng_pos", "funding", "basis_pct",
                    "oi_growth", "oi_usd", "turnover_usd", "score", "adj",
                    "htf", "t4", "setup", "aligned", "state", "atr_pct_1d", "rsi_4h")}
                for r in rows
            ],
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        return path
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--side", choices=["short", "long"], default="short",
                    help="which setup to rank for (default: short)")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--enrich", type=int, default=10,
                    help="candle-enrich & re-rank the top N by HTF trend/setup (0 or --fast to skip)")
    ap.add_argument("--fast", action="store_true",
                    help="ticker-only screen, no candle enrichment (old behaviour)")
    ap.add_argument("--no-log", action="store_true", help="don't append this run to the JSONL log")
    ap.add_argument("--no-regime", action="store_true", help="skip the BTC regime header")
    ap.add_argument("--min-turnover", type=float, default=1_000_000.0,
                    help="24h USD turnover floor (liquidity)")
    ap.add_argument("--min-oi-usd", type=float, default=250_000.0,
                    help="open-interest USD floor (liquidity)")
    ap.add_argument("--min-rng-pos", type=float, default=0.0,
                    help="only coins at least this far up their 24h range (0-1); shorts want a high floor")
    ap.add_argument("--max-rng-pos", type=float, default=1.0,
                    help="only coins at most this far up their 24h range (0-1); longs want a low ceiling")
    ap.add_argument("--include-tradfi", action="store_true",
                    help="also include tokenized metals/stocks (default: crypto alts only)")
    a = ap.parse_args()

    side = a.side
    now = dt.datetime.now(dt.timezone.utc)
    perps = load_perps()
    btc_chg = next((p["chg24"] for p in perps if str(p["symbol"]).upper() == "BTCUSD"), 0.0)
    for p in perps:
        p["rs_btc"] = p["chg24"] - btc_chg          # 24h performance relative to BTC (cross-sectional strength)
    universe = [
        r for r in perps
        if r["turnover_usd"] >= a.min_turnover
        and r["oi_usd"] >= a.min_oi_usd
        and a.min_rng_pos <= r["rng_pos"] <= a.max_rng_pos
        and (a.include_tradfi or r["top_tag"] == "crypto")
    ]
    for r in universe:
        r["score"] = score(r, side)
    universe.sort(key=lambda r: r["score"], reverse=True)

    client = DeltaPublicClient()
    shown = universe[:a.top]
    enrich_n = 0 if a.fast else min(a.enrich, len(shown), 12)
    enriched = shown[:enrich_n]
    for r in enriched:
        an = pa.analyze(r["symbol"], client)
        cl = pa.classify(r["rng_pos"], an, side)
        r["adj"] = r["score"] * cl["mult"]
        r["htf"] = an.get("htf") if an.get("ok") else "?"
        r["t4"] = an.get("t4") if an.get("ok") else "?"
        r["atr_pct_1d"] = an.get("atr_pct_1d")
        r["rsi_4h"] = an.get("rsi_4h")
        r["setup"], r["aligned"], r["state"] = cl["setup"], cl["aligned"], cl["state"]
    if enriched:
        enriched.sort(key=lambda r: r["adj"], reverse=True)

    reg_lab, reg_note = ("", "")
    if not a.no_regime:
        reg_lab, reg_note = regime_line(client)

    label = "LONG" if side == "long" else "SHORT"
    print("=" * 104)
    print(f"  DELTA INDIA — READ-ONLY PERP {label} SCREEN   (data only; confirm on TradingView, you place the order)")
    print(f"  {now:%Y-%m-%d %H:%M UTC}  |  {len(universe)} liquid perps "
          f"(turnover >= ${a.min_turnover:,.0f}, OI >= ${a.min_oi_usd:,.0f})  |  ranked by {side}-setup score")
    if reg_lab:
        print(f"  REGIME: {reg_lab}  |  {reg_note}")
    print("=" * 104)

    if enrich_n and enriched:
        hdr = (f"  {'#':>2} {'coin':<8} {'mark':>11} {'chg%':>6} {'rsB':>6} {'rng':>4} {'fund%':>6} "
               f"{'turn$':>7} {'base':>5} {'adj':>5}  {'D·4h':<5} {'setup':<13} {'state':<6}")
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        for i, r in enumerate(enriched, 1):
            tr = f"{pa.ARROW.get(r.get('htf'), '?')}{pa.ARROW.get(r.get('t4'), '?')}"
            print(f"  {i:>2} {r['coin']:<8} {r['mark']:>11,.4g} {r['chg24']:>6.1f} {r['rs_btc']:>6.1f} "
                  f"{r['rng_pos']:>4.2f} {r['funding']:>6.3f} {r['turnover_usd'] / 1e6:>6.1f}M "
                  f"{r['score']:>5.1f} {r['adj']:>5.1f}  {tr:<5} {str(r.get('setup')):<13} {str(r.get('state')):<6}")
        print(f"\n  adj = base × HTF-alignment (×1.15 with-trend, ×0.70 counter-trend). D·4h = Daily·4h trend (↑up ↓down →flat).")
        print("  rsB = coin 24h% − BTC 24h% (vs-BTC strength): shorts want rsB<0 (laggard); longs want rsB>0 (leader/resilient).")
        print("  setup: trend-/bounce-/range-/pullback- = with-trend or at a level; fade-strength/top-fade/knife-long = COUNTER-trend.")
        print("  state: WATCH (aligned + at the level) · ARMED (aligned, approaching) · AVOID (fighting the HTF trend). Not a trigger.")
        if not a.no_log:
            p = log_run(side, reg_lab, enriched)
            if p:
                print(f"  logged {len(enriched)} candidates -> {os.path.relpath(p)}")
        print()
    else:
        hdr = (f"  {'#':>2} {'coin':<9} {'mark':>12} {'chg24%':>7} {'rsB%':>6} {'rngPos':>6} "
               f"{'fund%':>6} {'basis%':>7} {'OIΔ6h%':>7} {'OI$':>8} {'turn$':>8} {'score':>5}  flags")
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        if not universe:
            print("   (no perps clear the liquidity gate — lower --min-turnover / --min-oi-usd)")
        for i, r in enumerate(shown, 1):
            print(f"  {i:>2} {r['coin']:<9} {r['mark']:>12,.4g} {r['chg24']:>7.2f} {r['rs_btc']:>6.1f} "
                  f"{r['rng_pos']:>6.2f} {r['funding']:>6.3f} {r['basis_pct']:>7.3f} "
                  f"{r['oi_growth'] * 100:>7.1f} {r['oi_usd'] / 1e6:>7.2f}M "
                  f"{r['turnover_usd'] / 1e6:>7.2f}M {r['score']:>5.1f}  {flags(r, side)}")
        print("\n  rsB% = coin 24h% − BTC 24h% (vs-BTC strength): shorts want rsB<0 (laggard); longs want rsB>0 (leader).")

    if side == "long":
        print("  rngPos ~0 = pressed at the 24h low (near support). fund% < 0 = shorts pay longs (long carry + squeeze fuel).")
        print("  Confirm a bullish reversal at support on TradingView, then futures_plan --side long. Read-only. Not advice.\n")
    else:
        print("  rngPos ~1 = pressed at the 24h high (near resistance). fund% > 0 = longs pay shorts (short carry).")
        print("  Confirm a bearish reversal at resistance on TradingView, then futures_plan. Read-only. Not advice.\n")


if __name__ == "__main__":
    main()

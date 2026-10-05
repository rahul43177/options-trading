"""
Read-only options entry scanner for the CALL/PUT short workflow.

Reuses the project's DeltaPublicClient (public market data only — no auth, no
orders). It SHOWS the live chain (premium, spread, greeks, liquidity, and a
rough premium projection if spot travels into a watched zone) so a human can
decide and place their own limit. It never trades.

    python -m research.entry_scan                              # both sides, OTM near spot
    python -m research.entry_scan --side call --zone 84530     # calls to short if BTC rises into a zone
    python -m research.entry_scan --side put  --zone 82726 --min-oi 20
    python -m research.entry_scan --dmin 0.10 --dmax 0.25 --buffer 1000 --max-hours 48
"""
from __future__ import annotations
import argparse, datetime as dt
from .delta_api import DeltaPublicClient
from .pricing import project_premium, project_premium_arrival, implied_vol
from . import arrival as _arrival
from .perp_analytics import underlying_atr_1h, hours_to_zone
from .config import UNDERLYINGS, DEFAULT_BUFFER
from . import proj_log


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def px(v, width=7):
    """Premium formatter: whole numbers for BTC-sized premiums, 2 decimals below 100 (ETH's
    single-digit premiums were printed as integers, hiding e.g. 4.5 vs 4.9)."""
    if v is None:
        return f"{'—':>{width}}"
    return f"{v:>{width}.0f}" if abs(v) >= 100 else f"{v:>{width}.2f}"


def valid_hours(band, slow=1.5):
    """How long the rest@ floor stays honest. Arrival model: the 75th-percentile arrival time
    (rest@ is priced there). Legacy model: slow x hours_to_zone. Past it, theta has eaten the
    premium — re-run the scan before resting a limit."""
    if not band:
        return None
    if band.get("valid_hours") is not None:
        return max(0.25, band["valid_hours"])
    return max(1.0, band.get("hours_to_zone", 0.0) * slow)


def reach_pct(band):
    """P(price reaches the zone before this expiry) from the arrival table, or None (legacy)."""
    p = (band or {}).get("p_hit")
    return None if p is None else p * 100


def _hours_left(o, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    return (o["expiry"] - now).total_seconds() / 3600 if o["expiry"] else None


def _iv_of(o):
    """The strike's own IV — mark_iv when present, else backed out of the mid."""
    iv = o.get("iv")
    if iv and iv > 0:
        return iv
    h = _hours_left(o)
    if o.get("mid") and h:
        kind = "call" if o["type"] == "CALL" else "put"
        return implied_vol(o["mid"], o["spot"], o["strike"], max(h, 0.25) / 8760, kind)
    return None


USE_ARRIVAL_MODEL = True        # flipped off by --legacy-band


def cap_at_ask(band, ask):
    """A resting SELL limit priced at/below the bid fills IMMEDIATELY — i.e. you'd be short
    before the zone, the exact mistake rest@ exists to prevent. When the late-arrival premium
    is below today's ask (theta over the wait outweighs the move to the zone), rest@ is lifted
    to the ask and flagged: waiting buys location/confirmation, not extra premium. Pure."""
    if band is None or not ask or band["floor"] >= ask:
        return band
    band = dict(band)
    band["floor_raw"] = band["floor"]
    band["floor"] = band["rest_here"] = ask
    band["capped"] = True
    return band


def band_for(o, zone, atr_1h, k=0.5, iv_bump=0.15):
    """Projected premium band for one option if BTC reaches `zone`. None if IV/expiry unknown.

    Shared by entry_scan and watch_plan so both speak the same numbers. Uses a Black-Scholes
    reprice at spot=zone, charging theta for the ATR-estimated hours to travel there (and a
    vega scenario for the ceiling) — not the old instantaneous delta+gamma snapshot.
    """
    h = _hours_left(o)
    iv = _iv_of(o)
    if h is None or iv is None:
        return None
    kind = "call" if o["type"] == "CALL" else "put"
    # Empirical arrival model (research.arrival, validated out-of-sample 05-Oct-2026: rest@ filled
    # on 75% of real zone hits vs 48% for the legacy linear rule). Falls back to legacy if the
    # table or ATR is missing, or when --legacy-band is passed.
    if USE_ARRIVAL_MODEL and atr_1h and o.get("spot"):
        table = _arrival.load_table()
        arr = _arrival.lookup(abs(zone - o["spot"]) / atr_1h, h, table) if table else None
        if arr:
            band = project_premium_arrival(o["strike"], kind, iv, h, zone, arr, iv_bump=iv_bump)
            band["dist_atr"] = abs(zone - o["spot"]) / atr_1h
            return cap_at_ask(band, o.get("ask"))
    hz = hours_to_zone(o["spot"], zone, atr_1h, k)
    hz = 0.0 if hz is None else hz          # ATR unknown -> instantaneous fallback
    band = project_premium(o["strike"], kind, iv, h, zone, hz, iv_bump=iv_bump)
    band["model"] = "legacy"
    return band


def _expiry(sym: str):
    try:
        return dt.datetime.strptime(sym.split("-")[-1], "%d%m%y").replace(
            hour=12, tzinfo=dt.timezone.utc)
    except Exception:
        return None


def load_options(asset="BTC"):
    rows = []
    for t in DeltaPublicClient().option_chain(underlying=asset):
        sym = str(t.get("symbol", ""))
        if not (sym.startswith(f"C-{asset}-") or sym.startswith(f"P-{asset}-")):
            continue
        q = t.get("quotes", {}) or {}
        g = t.get("greeks", {}) or {}
        bid, ask = _f(q.get("best_bid")), _f(q.get("best_ask"))
        rows.append({
            "symbol": sym,
            "type": "CALL" if t.get("contract_type") == "call_options" else "PUT",
            "strike": _f(t.get("strike_price")),
            "spot": _f(t.get("spot_price")),
            "bid": bid, "ask": ask,
            "mid": (bid + ask) / 2 if bid is not None and ask is not None else None,
            "spread": (ask - bid) if bid is not None and ask is not None else None,
            "delta": _f(g.get("delta")),
            "gamma": _f(g.get("gamma")) or 0.0,
            "theta": _f(g.get("theta")),
            "iv": _f(q.get("mark_iv")),
            "oi": _f(t.get("oi")) or 0.0,
            "expiry": _expiry(sym),
            "timestamp_us": int(t["timestamp"]) if t.get("timestamp") is not None else None,
            "bid_size": _f(q.get("bid_size")),
            "ask_size": _f(q.get("ask_size")),
            "contract_value": _f(t.get("contract_value")),
            "tick_size": _f(t.get("tick_size")),
            "turnover": _f(t.get("turnover")),
            "product_status": t.get("product_status") or t.get("state"),
        })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--side", choices=["call", "put", "both"], default="both")
    ap.add_argument("--zone", type=float, default=None,
                    help="BTC level you're watching; strike must clear it by --buffer")
    ap.add_argument("--buffer", type=float, default=None, help="min $ OTM past the zone (default: per-asset)")
    ap.add_argument("--dmin", type=float, default=0.10)
    ap.add_argument("--dmax", type=float, default=0.25)
    ap.add_argument("--max-hours", type=float, default=48.0)
    ap.add_argument("--min-oi", type=float, default=0.0, help="skip strikes with OI below this (liquidity)")
    ap.add_argument("--top", type=int, default=6)
    ap.add_argument("--asset", choices=sorted(UNDERLYINGS), default="BTC", help="underlying (default BTC)")
    ap.add_argument("--atr-k", type=float, default=0.5, help="net progress per hour = k x ATR(1h)")
    ap.add_argument("--iv-bump", type=float, default=0.15, help="IV expansion for the band ceiling (fast approach)")
    ap.add_argument("--log", action="store_true", help="append the top candidate's band to proj_log for calibration")
    ap.add_argument("--legacy-band", action="store_true", help="old linear arrival rule (dist / 0.5 ATR) instead of the arrival table")
    a = ap.parse_args()
    if a.buffer is None:
        a.buffer = DEFAULT_BUFFER[a.asset]
    global USE_ARRIVAL_MODEL
    USE_ARRIVAL_MODEL = not a.legacy_band

    now = dt.datetime.now(dt.timezone.utc)
    opts = load_options(a.asset)
    spot = next((o["spot"] for o in opts if o["spot"]), None)
    atr_1h = underlying_atr_1h(UNDERLYINGS[a.asset]) if a.zone is not None else None

    print("=" * 96)
    print("  DELTA INDIA — READ-ONLY ENTRY SCAN   (data only; you place your own limit order)")
    print(f"  {a.asset} spot {spot:,.1f}  |  {now:%Y-%m-%d %H:%M UTC}  |  |delta| {a.dmin}-{a.dmax}, "
          f"buffer ${a.buffer:,.0f}, <= {a.max_hours:.0f}h, min OI {a.min_oi:.0f}")
    if a.zone is not None:
        atr_txt = f"ATR(1h) {atr_1h:,.0f}" if atr_1h else "ATR unavailable (instantaneous proj)"
        model = ("arrival table (empirical, 75%-fill rest@)" if (not a.legacy_band and _arrival.load_table())
                 else "legacy linear arrival")
        print(f"  {atr_txt}  |  band = rest@ (late arrival) / base (median) / fast (early + IV +{a.iv_bump*100:.0f}%)  |  {model}")
    print("=" * 96)

    for side in (["CALL", "PUT"] if a.side == "both" else [a.side.upper()]):
        anchor = a.zone if a.zone is not None else spot
        rows = []
        for o in opts:
            if o["type"] != side or None in (o["bid"], o["ask"], o["delta"], o["strike"]):
                continue
            h = (o["expiry"] - now).total_seconds() / 3600 if o["expiry"] else None
            if h is None or not (0 < h <= a.max_hours):
                continue
            if side == "CALL" and o["strike"] < anchor + a.buffer:
                continue
            if side == "PUT" and o["strike"] > anchor - a.buffer:
                continue
            if not (a.dmin <= abs(o["delta"]) <= a.dmax) or o["oi"] < a.min_oi:
                continue
            o["dte_h"] = h
            o["dist"] = o["strike"] - spot if side == "CALL" else spot - o["strike"]
            o["spread_pct"] = (o["spread"] / o["mid"] * 100) if o["mid"] else 0.0
            if a.zone is not None:
                o["band"] = band_for(o, a.zone, atr_1h, a.atr_k, a.iv_bump)
            rows.append(o)
        rows.sort(key=lambda x: abs(abs(x["delta"]) - 0.16))

        title = f"SHORT {side}  (sell to open)"
        if a.zone is not None:
            title += f"   — watching zone {a.zone:,.0f}"
        print(f"\n  {title}")
        hdr = (f"  {'contract':<20} {'exp(h)':>6} {'bid':>7} {'ask':>7} {'mid':>7} "
               f"{'spr':>5} {'spr%':>6} {'|d|':>5} {'IV%':>5} {'OI':>8} {'dist$':>7}")
        if a.zone is not None:
            hdr += f" {'rest@':>7} {'base':>7} {'fast':>7} {'valid':>6} {'reach':>6}"
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        if not rows:
            print("   (no contracts match — widen --dmax, raise --max-hours, or lower --buffer/--min-oi)")
        for o in rows[:a.top]:
            line = (f"  {o['symbol']:<20} {o['dte_h']:>6.1f} {px(o['bid'])} {px(o['ask'])} "
                    f"{px(o['mid'])} {px(o['spread'], 5)} {o['spread_pct']:>5.1f}% "
                    f"{abs(o['delta']):>5.2f} {(o['iv'] or 0) * 100:>5.1f} {o['oi']:>8.1f} {o['dist']:>7.0f}")
            if a.zone is not None:
                b = o.get("band")
                vh = valid_hours(b)
                rp = reach_pct(b)
                fl = px(b['floor'], 6) + ("*" if b.get("capped") else " ")
                line += (f" {fl} {px(b['base'])} {px(b['ceiling'])} {('≤' + format(vh, '.0f') + 'h'):>6}"
                         f" {('—' if rp is None else format(rp, '.0f') + '%'):>6}"
                         if b else f" {'—':>7} {'—':>7} {'—':>7} {'—':>6} {'—':>6}")
            print(line)

        if a.log and a.zone is not None and rows and rows[0].get("band"):
            o = rows[0]
            proj_log.log_projection(o["symbol"], o["strike"], "call" if side == "CALL" else "put",
                                    spot, a.zone, _iv_of(o), o["dte_h"], o["band"],
                                    atr_1h, a.atr_k, a.iv_bump)

    print("\n  spr% = (ask-bid)/mid. High spr% or low OI = illiquid, hard to exit.")
    print("  rest@ = the premium to REST a maker SELL limit at (slow arrival, flat IV — fills even")
    print("  on a grind).  base = expected at the zone.  fast = if price spikes in with IV expanding.")
    print("  rest@ is priced at the LATE (75th-pct) arrival, so it fills on ~3 of 4 zone touches; base =")
    print("  median arrival. valid = that late-arrival time — not touched by then? re-run before resting.")
    print("  reach = chance price touches the zone before this expiry (empirical, 60d Delta candles).")
    print("  * = rest@ lifted to today's ask: on a late arrival the premium would be LOWER than now (theta >")
    print("  move), so waiting buys location/confirmation, not premium — and a lower limit would fill NOW.")
    print("  Zones > 2 ATR away are the least reliable (fewer fills) — re-run as price approaches.")
    print("  Fees: Delta charges min(0.01% of notional, 3.5% of premium) per side + 18% GST — on these")
    print("  strikes ~4.1% of premium each way, so a 50% take-profit keeps ~44% of the credit net.")
    print("  Don't market-short at the current ask before price reaches the zone. Not advice — you trade.\n")


if __name__ == "__main__":
    main()

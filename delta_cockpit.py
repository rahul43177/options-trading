#!/usr/bin/env python3
"""
Delta Exchange India — READ-ONLY options cockpit.

Reads PUBLIC market data only (no API key, no authentication, NEVER places or
modifies orders). Its only job is to SHOW you the option chain — premium, spread,
greeks, distance — so YOU can decide and place your own limit order on Delta.

Usage:
  python3 delta_cockpit.py                          # snapshot both sides, near-the-money OTM
  python3 delta_cockpit.py --side call --zone 84530 # calls to short if BTC rises into 84,530
  python3 delta_cockpit.py --side put  --zone 82726 # puts to short if BTC falls into 82,726
  python3 delta_cockpit.py --dmin 0.10 --dmax 0.25 --buffer 1000 --max-hours 48
"""
import urllib.request, json, argparse, datetime as dt

BASE = "https://api.india.delta.exchange"

def get(path):
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "delta-cockpit"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)

def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None

def parse_expiry(sym):
    try:
        d = sym.split("-")[-1]                       # e.g. 311025 -> 31 Oct 2025
        return dt.datetime.strptime(d, "%d%m%y").replace(hour=12, tzinfo=dt.timezone.utc)
    except Exception:
        return None

def load_btc_options():
    data = get("/v2/tickers?contract_types=call_options,put_options")
    out = []
    for t in data.get("result", []):
        sym = str(t.get("symbol", ""))
        if not (sym.startswith("C-BTC-") or sym.startswith("P-BTC-")):
            continue
        q = t.get("quotes", {}) or {}
        g = t.get("greeks", {}) or {}
        bid, ask = fnum(q.get("best_bid")), fnum(q.get("best_ask"))
        out.append({
            "symbol": sym,
            "type": "CALL" if t.get("contract_type") == "call_options" else "PUT",
            "strike": fnum(t.get("strike_price")),
            "spot": fnum(t.get("spot_price")),
            "bid": bid, "ask": ask,
            "mid": (bid + ask) / 2 if bid and ask else None,
            "spread": (ask - bid) if bid and ask else None,
            "delta": fnum(g.get("delta")),
            "gamma": fnum(g.get("gamma")),
            "theta": fnum(g.get("theta")),
            "iv": fnum(q.get("mark_iv")),
            "oi": fnum(t.get("oi")),
            "expiry": parse_expiry(sym),
        })
    return out

def hrs_to_expiry(o, now):
    return (o["expiry"] - now).total_seconds() / 3600 if o["expiry"] else None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--side", choices=["call", "put", "both"], default="both")
    ap.add_argument("--zone", type=float, default=None,
                    help="BTC level you're watching; strike must clear it by --buffer")
    ap.add_argument("--buffer", type=float, default=1000.0, help="min OTM distance from zone/spot ($)")
    ap.add_argument("--dmin", type=float, default=0.10, help="min |delta|")
    ap.add_argument("--dmax", type=float, default=0.25, help="max |delta|")
    ap.add_argument("--max-hours", type=float, default=48.0, help="max hours to expiry")
    ap.add_argument("--top", type=int, default=6, help="rows per side")
    a = ap.parse_args()

    now = dt.datetime.now(dt.timezone.utc)
    opts = load_btc_options()
    spot = next((o["spot"] for o in opts if o["spot"]), None)

    print("=" * 92)
    print("  DELTA INDIA — READ-ONLY OPTIONS COCKPIT   (data only; place your own limit order)")
    print(f"  BTC spot: {spot:,.1f}   |   {now:%Y-%m-%d %H:%M UTC}   |   filters: "
          f"|delta| {a.dmin}-{a.dmax}, buffer ${a.buffer:,.0f}, <= {a.max_hours:.0f}h")
    print("=" * 92)

    sides = ["CALL", "PUT"] if a.side == "both" else [a.side.upper()]
    for side in sides:
        anchor = a.zone if a.zone is not None else spot
        rows = []
        for o in opts:
            if o["type"] != side or None in (o["bid"], o["ask"], o["delta"], o["strike"]):
                continue
            h = hrs_to_expiry(o, now)
            if h is None or not (0 < h <= a.max_hours):
                continue
            # OTM + buffer relative to the anchor (zone if given, else spot)
            if side == "CALL" and o["strike"] < anchor + a.buffer:
                continue
            if side == "PUT" and o["strike"] > anchor - a.buffer:
                continue
            if not (a.dmin <= abs(o["delta"]) <= a.dmax):
                continue
            o["dte_h"] = h
            o["dist"] = o["strike"] - spot if side == "CALL" else spot - o["strike"]
            o["spread_pct"] = (o["spread"] / o["mid"] * 100) if o["mid"] else None
            # rough premium if spot travels to the zone (delta+gamma shock) — ESTIMATE ONLY
            if a.zone is not None:
                ds = a.zone - spot
                o["proj"] = o["ask"] + o["delta"] * ds + 0.5 * (o["gamma"] or 0) * ds * ds
            rows.append(o)
        rows.sort(key=lambda x: abs(abs(x["delta"]) - 0.16))

        title = f"SHORT {side}  (sell to open)"
        if a.zone is not None:
            title += f"   — watching zone {a.zone:,.0f}"
        print(f"\n  {title}")
        hdr = f"  {'strike':>8} {'exp(h)':>6} {'bid':>7} {'ask':>7} {'mid':>7} {'spr':>6} {'spr%':>6} {'|Δ|':>5} {'IV%':>5} {'OI':>7} {'dist$':>7}"
        if a.zone is not None:
            hdr += f" {'proj@zone':>9}"
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        if not rows:
            print("   (no contracts match — widen --dmax, raise --max-hours, or lower --buffer)")
        for o in rows[:a.top]:
            line = (f"  {o['strike']:>8.0f} {o['dte_h']:>6.1f} {o['bid']:>7.0f} {o['ask']:>7.0f} "
                    f"{o['mid']:>7.0f} {o['spread']:>6.0f} {o['spread_pct']:>5.1f}% "
                    f"{abs(o['delta']):>5.2f} {(o['iv'] or 0)*100:>5.1f} {o['oi'] or 0:>7.1f} {o['dist']:>7.0f}")
            if a.zone is not None:
                line += f" {o['proj']:>9.0f}"
            print(line)

    print("\n  spread% = (ask-bid)/mid. HIGH spread = you lose it if you cross. Rest a maker limit,")
    print("  don't hit the bid. proj@zone is a rough delta+gamma estimate, NOT a guaranteed price.")
    print("  This tool never trades. You place every order yourself. Not financial advice.\n")

if __name__ == "__main__":
    main()

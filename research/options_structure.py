"""Options-positioning CONTEXT — Max-Pain magnet, IV skew, put/call positioning (READ-ONLY).

Cherry-picked from the AstraQuant factor set (T1.5 "Options Volatility Surface" + Max-Pain), adapted to
the Delta India chain and this project's honesty discipline. It is CONTEXT only — a positioning read to
corroborate zones and judge whether put premium is rich — never a trigger, never wired into the
engine/desk score, never a reason to fade the higher-timeframe trend.

What it reads, per expiry:
  * MAX PAIN — the settlement strike that minimises total intrinsic payout to option holders
    (i.e. where the most open interest expires worthless). Into expiry there is a WEAK, well-documented
    but contested tendency for price to gravitate toward it (dealer hedging). Treat it like a pivot/POC:
    a magnet level, corroborating evidence, not a predictor. For a premium seller, where max-pain sits
    vs spot and vs your short strike is useful context (a put short is comfier when max-pain >= strike).
  * IV SKEW — 25-delta put IV minus 25-delta call IV. Positive (put-rich) = the market is paying up for
    downside protection (hedging/fear) -> fatter put premium to sell, but also priced downside risk; a
    steep skew is a caution, not a free lunch. Negative (call-rich) = upside-chasing/complacency.
  * ATM IV and PUT/CALL OI RATIO (PCR) — overall vol level and positioning balance.

Honest status: max-pain pin and skew are CONTEXT, not validated edges here (same discipline as
research.microstructure / research.desk). Read-only: no auth, no orders.

    python -m research.options_structure --asset BTC
    python -m research.options_structure --asset ETH --max-hours 240 --json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from typing import Any

from .config import UNDERLYINGS
from .entry_scan import load_options


def compute_max_pain(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Strike minimising total holder payout = Sum call_oi*max(S-K,0) + put_oi*max(K-S,0).

    Pure: rows need only {type, strike, oi}. Contract value cancels in the argmin, so OI (contracts)
    is sufficient. Returns None if there is no usable open interest.
    """
    strikes = sorted({r["strike"] for r in rows if r.get("strike") and (r.get("oi") or 0) > 0})
    if len(strikes) < 2:
        return None
    best_s = None
    best_pay = None
    curve = []
    for s in strikes:
        pay = 0.0
        for r in rows:
            k = r.get("strike")
            oi = r.get("oi") or 0.0
            if not k or oi <= 0:
                continue
            if r["type"] == "CALL":
                pay += oi * max(s - k, 0.0)
            else:
                pay += oi * max(k - s, 0.0)
        curve.append((s, pay))
        if best_pay is None or pay < best_pay:
            best_pay, best_s = pay, s
    return {"max_pain_strike": best_s, "payout_at_min": round(best_pay or 0.0),
            "n_strikes": len(strikes), "curve": curve}


def _nearest_by_delta(rows: list[dict[str, Any]], target_abs_delta: float) -> dict[str, Any] | None:
    cands = [r for r in rows if r.get("delta") is not None and r.get("iv")]
    if not cands:
        return None
    return min(cands, key=lambda r: abs(abs(r["delta"]) - target_abs_delta))


def compute_skew(rows: list[dict[str, Any]], spot: float) -> dict[str, Any]:
    """25-delta put-minus-call IV skew, ATM IV, and put/call OI ratio. Pure."""
    calls = [r for r in rows if r["type"] == "CALL"]
    puts = [r for r in rows if r["type"] == "PUT"]
    call25 = _nearest_by_delta(calls, 0.25)
    put25 = _nearest_by_delta(puts, 0.25)
    skew = None
    if call25 and put25:
        skew = put25["iv"] - call25["iv"]
    # ATM IV = IV of the strike nearest spot (avg of call+put legs when both present)
    atm_iv = None
    if spot:
        near = sorted({r["strike"] for r in rows if r.get("strike")},
                      key=lambda k: abs(k - spot))
        if near:
            atmk = near[0]
            ivs = [r["iv"] for r in rows if r.get("strike") == atmk and r.get("iv")]
            if ivs:
                atm_iv = sum(ivs) / len(ivs)
    call_oi = sum(r.get("oi") or 0.0 for r in calls)
    put_oi = sum(r.get("oi") or 0.0 for r in puts)
    pcr = (put_oi / call_oi) if call_oi > 0 else None
    return {
        "atm_iv": atm_iv,
        "call_iv_25d": call25["iv"] if call25 else None,
        "put_iv_25d": put25["iv"] if put25 else None,
        "skew_25d": skew,
        "call_oi": round(call_oi), "put_oi": round(put_oi),
        "pcr_oi": round(pcr, 2) if pcr is not None else None,
        "skew_tag": None if skew is None else (
            "PUT-RICH (downside hedging / fear — fatter put premium, but priced risk)" if skew >= 0.02 else
            "CALL-RICH (upside-chasing / complacency)" if skew <= -0.02 else
            "FLAT (balanced vol)"),
    }


def structure(asset: str = "BTC", max_hours: float = 240.0,
              min_total_oi: float = 50.0) -> dict[str, Any]:
    """Per-expiry Max-Pain + skew + PCR for the nearest liquid expiries within max_hours."""
    now = dt.datetime.now(dt.timezone.utc)
    rows = load_options(asset)
    spot = next((o["spot"] for o in rows if o.get("spot")), None)
    by_expiry: dict[Any, list[dict[str, Any]]] = {}
    for o in rows:
        exp = o.get("expiry")
        if not exp or o.get("strike") is None:
            continue
        h = (exp - now).total_seconds() / 3600
        if not (0 < h <= max_hours):
            continue
        by_expiry.setdefault(exp, []).append(o)
    out = []
    for exp in sorted(by_expiry):
        erows = by_expiry[exp]
        total_oi = sum(r.get("oi") or 0.0 for r in erows)
        if total_oi < min_total_oi:
            continue
        mp = compute_max_pain(erows)
        sk = compute_skew(erows, spot)
        h = (exp - now).total_seconds() / 3600
        rec = {
            "expiry": exp.strftime("%d-%b-%Y"), "hours": round(h, 1),
            "total_oi": round(total_oi),
            "max_pain": mp["max_pain_strike"] if mp else None,
            "max_pain_vs_spot_pct": (round((mp["max_pain_strike"] - spot) / spot * 100, 2)
                                     if (mp and spot) else None),
            **{k: v for k, v in sk.items() if k != "curve"},
        }
        out.append(rec)
    return {"asset": asset, "spot": spot, "generated_utc": now.strftime("%Y-%m-%d %H:%M UTC"),
            "expiries": out,
            "status": "CONTEXT_ONLY — max-pain pin & skew are positioning context, not a validated edge"}


def _print_human(res: dict[str, Any]) -> None:
    print("=" * 92)
    print(f"  OPTIONS POSITIONING — {res['asset']}   (READ-ONLY context: Max-Pain magnet + IV skew)")
    print(f"  spot {res['spot']:,.2f}  |  {res['generated_utc']}")
    print("=" * 92)
    if not res["expiries"]:
        print("  no liquid expiry within window (raise --max-hours or lower --min-oi)")
    for e in res["expiries"][:3]:
        print(f"\n  EXPIRY {e['expiry']}  (~{e['hours']:.0f}h)   total OI {e['total_oi']:,}")
        if e["max_pain"] is not None:
            arrow = "above spot (mild up-pull)" if e["max_pain_vs_spot_pct"] and e["max_pain_vs_spot_pct"] > 0 \
                else ("below spot (mild down-pull)" if e["max_pain_vs_spot_pct"] else "~at spot")
            print(f"    Max-Pain magnet  {e['max_pain']:,.0f}  ({e['max_pain_vs_spot_pct']:+.2f}% vs spot — {arrow})")
        atm = f"{e['atm_iv']*100:.1f}%" if e["atm_iv"] else "n/a"
        c25 = f"{e['call_iv_25d']*100:.1f}%" if e["call_iv_25d"] else "n/a"
        p25 = f"{e['put_iv_25d']*100:.1f}%" if e["put_iv_25d"] else "n/a"
        sk = f"{e['skew_25d']*100:+.1f} vol-pts" if e["skew_25d"] is not None else "n/a"
        print(f"    IV  ATM {atm} · 25d call {c25} / put {p25} · skew {sk}")
        if e.get("skew_tag"):
            print(f"       → {e['skew_tag']}")
        print(f"    Positioning  PCR(OI) {e['pcr_oi']}  (put OI {e['put_oi']:,} / call OI {e['call_oi']:,})")
    print("\n  Max-pain = a WEAK into-expiry magnet (treat like a pivot/POC, corroborating not predictive).")
    print("  Skew = where the chain prices risk / how rich put premium is. CONTEXT only — not a trigger,")
    print("  not wired into the engine/desk score, never a reason to fade the HTF trend. Read-only.\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Read-only options positioning: Max-Pain + IV skew + PCR.")
    ap.add_argument("--asset", choices=sorted(UNDERLYINGS), default="BTC")
    ap.add_argument("--max-hours", type=float, default=240.0, help="look at expiries within this window")
    ap.add_argument("--min-oi", type=float, default=50.0, help="skip expiries with total OI below this")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    res = structure(a.asset, a.max_hours, a.min_oi)
    if a.json:
        print(json.dumps(res, indent=2, default=str))
    else:
        _print_human(res)


if __name__ == "__main__":
    main()

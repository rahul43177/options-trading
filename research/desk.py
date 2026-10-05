"""Institutional 'last-brain' desk rating layer for the short-premium workflow — READ-ONLY.

The deterministic :mod:`research.engine` already does the right, disciplined thing: hard
executable-quote gates, a transparent multi-factor score, and exactly one global ``◄ BEST``.
It deliberately refuses to invent an edge (its ``iv_rv`` component stays 0 while a forward-RV
forecast is unverified — see ``ENGINE_AUDIT.md``).

This module does NOT replace or mutate that engine. It consumes the engine's *eligible* set
(the survivors of the hard gates, with their scores intact) and adds the extra read a
professional options desk would apply before committing capital, broken into the three things
a premium seller is actually paid to judge:

  RANGE   — will the strike stay OTM?  P(expire OTM) ~ 1-|delta|, plus the cushion measured in
            EXPECTED MOVES (dist / (spot*IV*sqrt(T))), discounted when the trade fights the
            higher-timeframe trend (the no-fade loophole).
  PREMIUM — is the reward worth the risk?  A modeled expected-value per unit of credit under a
            credit-multiple stop, the IV-versus-realized-vol REGIME (ATR-based RV proxy — an
            upward-biased estimate, NOT a vega forecast), and spread friction on the exit.
  DATE    — is the clock on your side?  DTE sweet-spot for theta capture, a hard gamma-danger
            cliff for sub-18h expiries, whether price can even reach the zone before expiry,
            and a weekend-span flag.

Every number here is a DISCIPLINED RANKING, not a validated edge: the stored evidence base is
still COLLECTING (see ``research.readiness``), the RV proxy is ATR-derived and biased high, the
EV model assumes delta ~ P(ITM) and a fixed-multiple stop; EV is net of Delta's fee rule
(research.fees) but margin/liquidation is NOT modeled (see research.positions). It never places, modifies, or cancels an order.

    python -m research.desk --context /tmp/ctx.json            # human desk table
    python -m research.desk --context /tmp/ctx.json --json     # machine-readable
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import UNDERLYINGS
from .entry_scan import load_options
from .perp_analytics import underlying_atr_1h, hours_to_zone
from . import engine as _engine
from . import fees as _fees
from . import arrival as _arrival

HOURS_PER_YEAR = 24 * 365

# Desk composite weights — a premium seller lives or dies on keeping the credit (RANGE),
# then on whether the reward justified the risk (PREMIUM); DATE is the tie-breaker.
W_RANGE, W_PREMIUM, W_DATE = 0.45, 0.35, 0.20

# A short option is stopped at this multiple of the credit received (standard desk discipline).
STOP_CREDIT_MULTIPLE = 2.0

# Sub-18h shorts are gamma/pin landmines — a hard desk reject, never a 'rest and forget'.
GAMMA_DANGER_HOURS = 18.0


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _rv_proxy_annual(atr_1h: float | None, spot: float | None) -> float | None:
    """Crude annualized realized vol from ATR(1h): (ATR/spot) * sqrt(hours/yr).

    ATR includes wicks/gaps, so this OVERSTATES close-to-close RV by roughly 1.2-1.5x. It is a
    regime read ("is option IV rich or cheap versus how much the thing is actually moving?"),
    never a forecast. We keep it out of the engine on purpose and label it everywhere.
    """
    if not atr_1h or not spot or spot <= 0:
        return None
    return (atr_1h / spot) * math.sqrt(HOURS_PER_YEAR)


def _expected_move(spot: float, iv: float | None, hours: float) -> float | None:
    """1-sigma expected move to expiry in price units: spot * IV * sqrt(T)."""
    if not iv or iv <= 0 or hours <= 0 or not spot:
        return None
    return spot * iv * math.sqrt(hours / HOURS_PER_YEAR)


def _grade_letter(x: float) -> str:
    if x >= 8.0:
        return "A"
    if x >= 6.5:
        return "B"
    if x >= 5.0:
        return "C"
    if x >= 3.5:
        return "D"
    return "F"


def _range_grade(abs_delta: float, cushion_sigma: float | None, aligned: bool) -> tuple[float, dict]:
    """0-10. Probability the credit survives, plus how many expected-moves of cushion."""
    p_otm = 1.0 - abs_delta
    # map p_otm in [0.75, 0.92] -> 0..6 (|delta| 0.25 -> 0.75; 0.08 -> 0.92)
    score_p = _clamp((p_otm - 0.75) / (0.92 - 0.75) * 6.0, 0, 6)
    # map cushion in [0.8, 2.0] expected-moves -> 0..4
    score_c = 0.0 if cushion_sigma is None else _clamp((cushion_sigma - 0.8) / (2.0 - 0.8) * 4.0, 0, 4)
    raw = score_p + score_c
    # No-fade discount: fighting the higher-timeframe trend is the loophole -> 0.6x (mirrors the
    # perp desk's 0.70 counter-trend multiplier; the engine already zeroes the trend component).
    grade = raw if aligned else raw * 0.6
    return round(grade, 2), {
        "p_expire_otm": round(p_otm, 3),
        "cushion_expected_moves": None if cushion_sigma is None else round(cushion_sigma, 2),
        "trend_aligned": aligned,
    }


# Typical take-profit used for the fee estimate on winners (the user's journal: median ~30-50% of
# the credit captured, closed well before expiry), so a win usually pays a closing fee too.
TYPICAL_TAKE_PROFIT = 0.5


def fee_drag_per_credit(abs_delta: float, fee_rate: float) -> float:
    """Expected fees per unit of credit (Delta rule, incl. GST). Pure (unit-tested).

    Opening fee = fee_rate x credit. Closing: a win buys back at ~(1-TYPICAL_TAKE_PROFIT) of the
    credit (prob ~1-|d|); a loss buys back at STOP_CREDIT_MULTIPLE x credit (prob ~|d|).
    """
    close = (1.0 - abs_delta) * (1.0 - TYPICAL_TAKE_PROFIT) + abs_delta * STOP_CREDIT_MULTIPLE
    return fee_rate * (1.0 + close)


def _premium_grade(abs_delta: float, iv: float | None, rv_proxy: float | None,
                   spread_pct: float, fee_rate: float = 0.0) -> tuple[float, dict]:
    """0-10. Modeled EV per credit NET of Delta fees, IV-vs-RV regime, and exit friction."""
    # EV per unit of credit under a credit-multiple stop: win keeps 1 credit (prob ~ 1-|d|),
    # loss gives back STOP_CREDIT_MULTIPLE credits (prob ~ |d|, delta as a P(ITM) proxy).
    ev_gross = (1.0 - abs_delta) - abs_delta * STOP_CREDIT_MULTIPLE
    drag = fee_drag_per_credit(abs_delta, fee_rate)
    ev_per_credit = ev_gross - drag                       # NET of fees (was ignored before)
    score_ev = _clamp(ev_per_credit / 0.70 * 5.0, 0, 5)  # EV 0.70 (|d|~0.10) -> full marks
    ratio = (iv / rv_proxy) if (iv and rv_proxy and rv_proxy > 0) else None
    # Only a RICH IV (vs realized) is a seller's vol edge. Cheap IV (ratio<~0.9) scores 0.
    score_ivrv = 0.0 if ratio is None else _clamp((ratio - 0.85) / (1.25 - 0.85) * 3.0, 0, 3)
    if spread_pct <= 3:
        score_fr = 2.0
    elif spread_pct <= 6:
        score_fr = 1.0
    elif spread_pct <= 10:
        score_fr = 0.5
    else:
        score_fr = 0.0
    grade = score_ev + score_ivrv + score_fr
    return round(grade, 2), {
        "ev_per_credit": round(ev_per_credit, 2),
        "ev_per_credit_gross": round(ev_gross, 2),
        "fee_drag_per_credit": round(drag, 3),
        "fee_rate_per_side": round(fee_rate, 4),
        "iv": None if iv is None else round(iv, 3),
        "rv_proxy_annual": None if rv_proxy is None else round(rv_proxy, 3),
        "iv_over_rv": None if ratio is None else round(ratio, 2),
        "stop_credit_multiple": STOP_CREDIT_MULTIPLE,
    }


def _date_grade(hours: float, reachable: bool | None, weekend_span: bool) -> tuple[float, dict, bool]:
    """0-10 plus a gamma-danger flag. Theta window vs. gamma risk vs. reachability."""
    gamma_danger = hours < GAMMA_DANGER_HOURS
    if gamma_danger:
        base = 1.0
    elif hours < 24:
        base = 3.0
    elif hours < 36:
        base = 6.0
    elif hours <= 72:
        base = 9.0
    elif hours <= 96:
        base = 7.0
    else:
        base = 5.0
    grade = base
    if reachable:
        grade += 1.0
    if weekend_span:
        grade -= 1.0
    grade = _clamp(grade, 0, 10)
    return round(grade, 2), {
        "dte_hours": round(hours, 1),
        "gamma_danger": gamma_danger,
        "zone_reachable_before_expiry": reachable,
        "weekend_span": weekend_span,
    }, gamma_danger


def max_units_for_risk(credit: float, equity: float, risk_pct: float, spot: float | None,
                       stop_multiple: float = STOP_CREDIT_MULTIPLE) -> float | None:
    """Underlying units you can short so a stop at stop_multiple x credit loses <= risk_pct of
    equity, fees included (open + stop buyback). Pure (unit-tested)."""
    if not credit or credit <= 0 or not equity or equity <= 0 or not risk_pct or risk_pct <= 0:
        return None
    loss_per_unit = credit * (stop_multiple - 1.0) + _fees.option_fee(credit, 1.0, spot) \
        + _fees.option_fee(credit * stop_multiple, 1.0, spot)
    return equity * risk_pct / 100.0 / loss_per_unit


def _rate(cand: dict[str, Any], row: dict[str, Any], setup: dict[str, Any],
          atr_1h: float | None, now: datetime, sizing: dict[str, Any] | None = None,
          held: dict[str, float] | None = None) -> dict[str, Any]:
    side = cand["side"]
    abs_delta = abs(cand["delta"])
    spot = row.get("spot")
    strike = cand["strike"]
    hours = cand["expiry_hours"]
    iv = cand.get("mark_iv")
    spread_pct = cand["spread_pct"]
    aligned = cand["score_components"]["trend"] > 0  # engine zeroes trend for counter-trend

    dist = (strike - spot) if (spot is not None and side == "CALL") else (
        (spot - strike) if spot is not None else None)
    em = _expected_move(spot, iv, hours) if spot is not None else None
    cushion_sigma = (dist / em) if (dist is not None and em and em > 0) else None
    rv_proxy = _rv_proxy_annual(atr_1h, spot)

    # Can price reach the watched zone before this option expires? (feasibility of the plan)
    # same fallback as the engine's gate: a single "zone" stands in for zone_low/zone_high
    zone = setup.get("zone_high" if side == "CALL" else "zone_low", setup.get("zone"))
    reachable = None
    p_arrive = None
    if zone is not None and spot is not None and atr_1h:
        table = _arrival.load_table()
        arr = _arrival.lookup(abs(float(zone) - spot) / atr_1h, hours, table) if table else None
        if arr:                                  # empirical: P(touch before expiry) >= 50%
            p_arrive = round(arr["p_hit"], 2)
            reachable = arr["p_hit"] >= 0.5
        else:                                    # legacy linear fallback
            hz = hours_to_zone(spot, float(zone), atr_1h)
            reachable = (hz is not None and hz < hours)

    expiry = row.get("expiry")
    weekend_span = bool(expiry and expiry.weekday() >= 5)  # Sat/Sun expiry = weekend-dominated window

    range_grade, range_detail = _range_grade(abs_delta, cushion_sigma, aligned)
    credit = cand.get("bid") or ((cand["bid"] + cand["ask"]) / 2 if cand.get("ask") else None)
    fee_rate = _fees.option_fee_rate(credit, spot) if credit else 0.0
    premium_grade, premium_detail = _premium_grade(abs_delta, iv, rv_proxy, spread_pct, fee_rate)
    date_grade, date_detail, gamma_danger = _date_grade(hours, reachable, weekend_span)
    date_detail["p_arrive"] = p_arrive

    composite = W_RANGE * range_grade + W_PREMIUM * premium_grade + W_DATE * date_grade

    # ---- verdict taxonomy -------------------------------------------------------------
    rejects: list[str] = []
    if gamma_danger:
        rejects.append(f"gamma danger: {hours:.0f}h to expiry (<{GAMMA_DANGER_HOURS:.0f}h)")
    if premium_detail["ev_per_credit"] <= 0:
        rejects.append(f"non-positive modeled EV ({premium_detail['ev_per_credit']}/credit at |d|{abs_delta:.2f})")
    if spread_pct > 10:
        rejects.append(f"exit friction: {spread_pct:.1f}% spread")
    if composite < 3.5:
        rejects.append(f"composite {composite:.1f}/10 (grade F)")

    flags: list[str] = []
    if not aligned:
        flags.append("counter-trend (fades higher-timeframe bias — smaller size)")
    if premium_detail["iv_over_rv"] is not None and premium_detail["iv_over_rv"] < 0.95:
        flags.append(f"IV cheap vs realized (IV/RV~{premium_detail['iv_over_rv']}) — no vol edge, theta-only")
    if weekend_span:
        flags.append("weekend-span expiry (thin liquidity + Monday gap risk)")
    if cand.get("oi", 0) < 25:
        flags.append(f"thin OI {cand.get('oi'):.0f} (marginal exit depth)")

    # ---- what you already hold (concentration) -----------------------------------------
    held = held or {}
    already = held.get(cand["symbol"])
    same_side = {s: q for s, q in held.items() if s != cand["symbol"]
                 and s.split("-")[1:2] == [cand["asset"]] and s[:1] == cand["symbol"][:1]}
    if already:
        flags.append(f"ALREADY HELD ({already:+g}) — adding doubles the same bet; size the add within your risk")
    elif same_side:
        flags.append("same asset+side already held (" + ", ".join(f"{s} {q:+g}" for s, q in same_side.items())
                     + ") — correlated: losses would stack")

    # ---- sizing: how much can you sell so a 2x-credit stop stays within risk -----------
    sizing_out = None
    if sizing and credit:
        units = max_units_for_risk(credit, sizing.get("equity"), sizing.get("risk_pct", 2.0), spot)
        cv = row.get("contract_value") or None
        if units is not None:
            sizing_out = {"equity": sizing.get("equity"), "risk_pct": sizing.get("risk_pct", 2.0),
                          "max_units": round(units, 4),
                          "max_lots": int(units / cv) if cv else None,
                          "loss_at_stop_per_unit": round(credit * (STOP_CREDIT_MULTIPLE - 1.0), 4)}

    return {
        "symbol": cand["symbol"], "asset": cand["asset"], "side": side,
        "strike": strike, "expiry_hours": round(hours, 1),
        "bid": cand["bid"], "ask": cand["ask"], "spread_pct": spread_pct,
        "abs_delta": round(abs_delta, 3), "oi": cand.get("oi"), "iv": iv,
        "dist": None if dist is None else round(dist, 0),
        "engine_score": cand["score"],
        "range": range_grade, "premium": premium_grade, "date": date_grade,
        "composite": round(composite, 2), "letter": _grade_letter(composite),
        "range_detail": range_detail, "premium_detail": premium_detail, "date_detail": date_detail,
        "reject_reasons": rejects, "flags": flags,
        "rejected": bool(rejects),
        "already_held": bool(already), "sizing": sizing_out,
    }


def _reconcile(engine_best: dict[str, Any], desk_best: dict[str, Any] | None) -> dict[str, Any]:
    """Compare the engine's and the desk's pick on BOTH contract and band. Pure (unit-tested).

    The same contract can be eligible under a NEAR and a STRUCTURAL band; those are different
    plans (different trigger zone, reachability, confirmation state), so a symbol-only match is
    not agreement. status: AGREE | SAME_CONTRACT_DIFFERENT_BAND | OVERRIDE | NO_DESK_BEST.
    """
    e_sym, e_sid = engine_best.get("symbol"), engine_best.get("setup_id")
    d_sym = desk_best["symbol"] if desk_best else None
    d_sid = desk_best["setup_id"] if desk_best else None
    if not desk_best:
        status = "NO_DESK_BEST"
    elif e_sym == d_sym and e_sid == d_sid:
        status = "AGREE"
    elif e_sym == d_sym:
        status = "SAME_CONTRACT_DIFFERENT_BAND"
    else:
        status = "OVERRIDE"
    return {
        "engine_best_symbol": e_sym, "engine_best_setup_id": e_sid,
        "desk_best_symbol": d_sym, "desk_best_setup_id": d_sid,
        "status": status, "agree": status == "AGREE",
    }


def run(context: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    assets = {str(s.get("asset", "")).upper() for s in context.get("setups", [])}
    chains = {a: load_options(a) for a in assets if a in UNDERLYINGS}
    atr = {a: underlying_atr_1h(UNDERLYINGS[a]) for a in chains}

    eng = _engine.analyze(context, chains=chains, now=now)
    eligible = [c for c in eng["candidates"] if c.get("eligible")]
    rows_by_symbol = {r["symbol"]: r for a in chains for r in chains[a]}
    # keyed by the engine's per-setup id, NOT (asset, side): a NEAR and a STRUCTURAL band on the
    # same side would otherwise collide and the later one would silently overwrite the first
    setups = context.get("setups", [])
    setup_by = dict(zip(_engine.setup_ids(setups), setups))

    # Optional context fields (additive): "positions": [{"symbol": ..., "size": -2}] (what you
    # already hold, Delta "Size" in underlying units) and "sizing": {"equity": 411, "risk_pct": 2}.
    held = {str(p.get("symbol")): float(p.get("size") or 0) for p in (context.get("positions") or [])
            if p.get("symbol")}
    sizing = context.get("sizing") if isinstance(context.get("sizing"), dict) else None

    rated: list[dict[str, Any]] = []
    for c in eligible:
        row = rows_by_symbol.get(c["symbol"], {})
        setup = setup_by.get(c["setup_id"], {})
        rated.append({**_rate(c, row, setup, atr.get(c["asset"]), now, sizing, held),
                      "setup_id": c["setup_id"], "band": c.get("band")})

    survivors = [r for r in rated if not r["rejected"]]
    survivors.sort(key=lambda r: (r["composite"], r["range"], r.get("oi") or 0), reverse=True)
    rated.sort(key=lambda r: (r["rejected"], -r["composite"]))

    best = survivors[0] if survivors else None
    # Is the best only conditionally arm-able (no confirmed VALID trigger yet)?
    conditional = False
    if best:
        s = setup_by.get(best["setup_id"], {})
        conditional = not (str(s.get("state", "")).upper() == "VALID" and bool(s.get("confirmation")))
        for r in rated:
            if r is best:
                r["verdict"] = "BEST IF TRIGGERED" if conditional else "BEST NOW"
            elif r["rejected"]:
                r["verdict"] = "REJECT"
            else:
                r["verdict"] = "ALT"
    else:
        for r in rated:
            r["verdict"] = "REJECT" if r["rejected"] else "ALT"

    # If the best is a contract you already hold, also surface the best one you DON'T hold, so the
    # read never silently recommends doubling an existing position.
    best_not_held = None
    if best and best.get("already_held"):
        best_not_held = next((r for r in survivors if not r.get("already_held")), None)

    reconciliation = _reconcile(eng.get("best_candidate") or {}, best)
    return {
        "mode": "READ_ONLY_RESEARCH_DESK_RATING",
        "generated_at_utc": now.isoformat(),
        "engine_decision": eng["decision"],
        "best": best,
        "best_is_conditional": conditional,
        "best_not_held": best_not_held,
        "reconciliation": reconciliation,
        "candidates": rated,
        "weights": {"range": W_RANGE, "premium": W_PREMIUM, "date": W_DATE},
        "caveats": [
            "READ-ONLY: no order is ever placed; you confirm and place every limit yourself.",
            "Grades are a disciplined RANKING, not a validated edge (evidence base is COLLECTING).",
            "RV proxy is ATR-based and biased HIGH; IV/RV is a regime read, not a vega forecast.",
            "EV/credit assumes delta~P(ITM) and a fixed credit-multiple stop; a model, not a promise.",
            "EV is NET of Delta fees (min(0.01% notional, 3.5% premium) + 18% GST); margin/liquidation is NOT modeled here — use research.positions for held shorts.",
        ],
    }


def _print_human(res: dict[str, Any]) -> None:
    print("=" * 108)
    print("  DESK RATING — institutional 'last brain' over the engine's eligible set   (READ-ONLY)")
    print(f"  {res['generated_at_utc'][:19]}Z   |   engine decision: {res['engine_decision']}"
          f"   |   weights R/P/D = {res['weights']['range']}/{res['weights']['premium']}/{res['weights']['date']}")
    print("=" * 108)
    hdr = (f"  {'verdict':<19}{'contract':<21}{'band':<12}{'exp(h)':>6}{'|d|':>5}{'OI':>7}{'spr%':>6}"
           f"{'RANGE':>7}{'PREM':>6}{'DATE':>6}{'COMPOSITE':>11}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for r in res["candidates"]:
        print(f"  {r['verdict']:<19}{r['symbol']:<21}{str(r.get('band') or r['setup_id'])[:11]:<12}{r['expiry_hours']:>6.0f}{r['abs_delta']:>5.2f}"
              f"{(r.get('oi') or 0):>7.0f}{r['spread_pct']:>6.1f}{r['range']:>7.1f}{r['premium']:>6.1f}"
              f"{r['date']:>6.1f}{r['composite']:>8.1f} {r['letter']:<2}")
        reason = "; ".join(r["reject_reasons"]) if r["rejected"] else "; ".join(r["flags"])
        if reason:
            print(f"      └─ {reason}")
    rec = res["reconciliation"]
    print()
    if res["best"]:
        b = res["best"]
        tag = "conditional (if triggered)" if res["best_is_conditional"] else "actionable now"
        print(f"  DESK BEST: {b['symbol']} [{b.get('band') or b['setup_id']}]  —  composite {b['composite']}/10 ({b['letter']}), {tag}")
        print(f"    RANGE {b['range']}: P(OTM)~{b['range_detail']['p_expire_otm']}, "
              f"cushion {b['range_detail']['cushion_expected_moves']} expected-moves, "
              f"{'trend-aligned' if b['range_detail']['trend_aligned'] else 'COUNTER-TREND'}")
        print(f"    PREM  {b['premium']}: EV {b['premium_detail']['ev_per_credit']}/credit net of fees "
              f"(gross {b['premium_detail']['ev_per_credit_gross']}, fees {b['premium_detail']['fee_drag_per_credit']}), "
              f"IV/RV {b['premium_detail']['iv_over_rv']}")
        print(f"    DATE  {b['date']}: {b['date_detail']['dte_hours']}h, "
              f"gamma_danger={b['date_detail']['gamma_danger']}, "
              f"reachable={b['date_detail']['zone_reachable_before_expiry']}"
              + (f" (P(arrive) {b['date_detail']['p_arrive']:.0%})" if b['date_detail'].get('p_arrive') is not None else ""))
        if b.get("sizing"):
            z = b["sizing"]
            print(f"    SIZE  max ~{z['max_units']:g} units"
                  + (f" ({z['max_lots']} lots)" if z.get("max_lots") is not None else "")
                  + f" so a 2x-credit stop loses <= {z['risk_pct']:g}% of {z['equity']:g} (fees incl.)")
        if b.get("already_held"):
            nb = res.get("best_not_held")
            print("    NOTE  you ALREADY HOLD this contract — adding doubles the bet. Best you don't hold: "
                  + (f"{nb['symbol']} [{nb.get('band') or nb['setup_id']}] ({nb['composite']}/10 {nb['letter']})" if nb else "none"))
    else:
        print("  DESK BEST: none — every eligible contract was rejected. Stand aside.")
    verdict = {
        "AGREE": "AGREE",
        "SAME_CONTRACT_DIFFERENT_BAND": "SAME CONTRACT, DIFFERENT BAND (different trigger zone — not agreement)",
        "OVERRIDE": "DESK OVERRIDES ENGINE (see verdicts/flags above)",
        "NO_DESK_BEST": "DESK HAS NO BEST (all rejected)",
    }[rec["status"]]
    print(f"  ENGINE BEST: {rec['engine_best_symbol']} [{rec['engine_best_setup_id']}]   |   {verdict}")
    print()
    for c in res["caveats"]:
        print(f"  * {c}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description="Read-only institutional desk rating over the engine's eligible set.")
    ap.add_argument("--context", type=Path, required=True, help="same structured TradingView/setup JSON the engine uses")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    context = json.loads(args.context.read_text(encoding="utf-8"))
    res = run(context)
    if args.json:
        print(json.dumps(res, indent=2, default=str))
    else:
        _print_human(res)


if __name__ == "__main__":
    main()

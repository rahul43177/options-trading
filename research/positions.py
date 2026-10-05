"""Open-position health check for SHORT options — READ-ONLY (public chain data, no key, no orders).

The scanner/desk pick NEW entries; nothing looked at what you already hold. On 05-Oct-2026 that
gap mattered twice: (1) isolated-margin short puts sat ~2% from liquidation, inside the support
cluster the thesis relied on, so a normal wick could force-close them before expiry; (2) the desk
rated the very contract already held as BEST NOW (adding = doubling one bet). This tool answers,
per held short:

  * live mark, P&L gross and NET of the closing fee (Delta fee rule, research.fees)
  * the UNDERLYING price at which your stop and Delta's liquidation trigger (Black-Scholes solve
    at the option's live IV) — and whether liquidation would fire BEFORE your stop
  * odds over the remaining life (GBM at the live IV, IV rising as price moves against you):
    P(stop hit), P(liquidated), P(expire in-the-money)
  * loss at the stop as % of equity, and net delta exposure per asset (concentration)

    python -m research.positions --equity 411 \
        --pos P-ETH-2600-071026:-2:6.0:stop=3.51:liq=19.34 \
        --pos P-ETH-2620-071026:-1.5:8.0:stop=4.75:liq=21.37

`size` is in UNDERLYING units, negative = short (Delta's "Size" column, e.g. -2 ETH, -0.15 BTC).
`liq` is the Est. Liq. Price Delta shows for the position (an option price). Odds are a model,
not a forecast. Not advice — you manage every order.
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import random
from typing import Any

from .pricing import price as bs_price, delta as bs_delta
from . import fees

HOURS_PER_YEAR = 24 * 365


def parse_pos(spec: str) -> dict[str, Any]:
    """'P-ETH-2600-071026:-2:6.0:stop=3.51:liq=19.34' -> dict. Pure (unit-tested)."""
    parts = spec.split(":")
    if len(parts) < 3:
        raise ValueError(f"--pos needs SYMBOL:SIZE:ENTRY[:stop=X][:liq=Y], got {spec!r}")
    out: dict[str, Any] = {"symbol": parts[0], "size": float(parts[1]), "entry": float(parts[2]),
                           "stop": None, "liq": None}
    for p in parts[3:]:
        k, _, v = p.partition("=")
        if k in ("stop", "liq") and v:
            out[k] = float(v)
    sym = out["symbol"]
    out["kind"] = "put" if sym.startswith("P-") else "call"
    out["asset"] = sym.split("-")[1]
    out["strike"] = float(sym.split("-")[2])
    out["expiry"] = dt.datetime.strptime(sym.split("-")[-1], "%d%m%y").replace(
        hour=12, tzinfo=dt.timezone.utc)
    return out


def spot_for_price(target: float, strike: float, hours: float, iv: float, kind: str,
                   spot_now: float) -> float | None:
    """Underlying level at which the option is worth `target` (bisection). Pure (unit-tested).

    For a put the price rises as spot falls, so we search below spot; for a call, above.
    Returns None if the target is unreachable inside a +-60% band.
    """
    t = max(hours, 0.25) / HOURS_PER_YEAR
    lo, hi = (spot_now * 0.4, spot_now) if kind == "put" else (spot_now, spot_now * 1.6)
    f = lambda s: bs_price(s, strike, t, iv, kind) - target
    if f(lo) * f(hi) > 0:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if f(lo) * f(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def barrier_odds(spot: float, strike: float, kind: str, hours: float, iv: float,
                 stop: float | None, liq: float | None, paths: int = 3000,
                 step_min: int = 15, seed: int = 7, adverse_iv_k: float = 3.0) -> dict[str, float]:
    """MC over the option's remaining life. IV rises by adverse_iv_k x the % adverse move
    (a mild skew/vol-up assumption — moves against a short seller usually come with higher IV).
    Returns P(stop touched), P(liq touched), P(ITM at expiry). Deterministic with `seed`.
    """
    rng = random.Random(seed)
    steps = max(1, int(hours * 60 / step_min))
    dt_y = (hours / HOURS_PER_YEAR) / steps
    hit_stop = hit_liq = itm = 0
    for _ in range(paths):
        s = spot
        st_hit = lq_hit = False
        for i in range(1, steps + 1):
            s *= math.exp(-0.5 * iv * iv * dt_y + iv * math.sqrt(dt_y) * rng.gauss(0, 1))
            if stop is None and liq is None:
                continue
            adverse = max(0.0, (spot - s) / spot) if kind == "put" else max(0.0, (s - spot) / spot)
            t_left = max(hours / HOURS_PER_YEAR - i * dt_y, 0.25 / HOURS_PER_YEAR)
            px = bs_price(s, strike, t_left, iv * (1 + adverse_iv_k * adverse), kind)
            if stop is not None and px >= stop:
                st_hit = True
            if liq is not None and px >= liq:
                lq_hit = True
            if st_hit and (liq is None or lq_hit):
                break
        hit_stop += st_hit
        hit_liq += lq_hit
        itm += (s < strike) if kind == "put" else (s > strike)
    return {"p_stop": hit_stop / paths, "p_liq": hit_liq / paths, "p_itm": itm / paths}


def assess(pos: dict[str, Any], row: dict[str, Any], equity: float | None,
           now: dt.datetime | None = None, paths: int = 3000) -> dict[str, Any]:
    """Pure-ish assessment of one held short given its live chain row (bid/ask/iv/spot)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    hours = (pos["expiry"] - now).total_seconds() / 3600
    spot = row["spot"]
    iv = row.get("iv") or 0.4
    bid, ask = row.get("bid"), row.get("ask")
    mark = (bid + ask) / 2 if bid is not None and ask is not None else bs_price(
        spot, pos["strike"], max(hours, 0.25) / HOURS_PER_YEAR, iv, pos["kind"])
    units = abs(pos["size"])
    short = pos["size"] < 0
    buyback = ask if ask is not None else mark                     # you close a short at the ask
    gross = (pos["entry"] - buyback) * units if short else (buyback - pos["entry"]) * units
    close_fee = fees.option_fee(buyback, units, spot)
    out: dict[str, Any] = {
        **{k: pos[k] for k in ("symbol", "size", "entry", "stop", "liq", "kind", "strike")},
        "hours_left": round(hours, 1), "spot": spot, "iv": iv, "mark": round(mark, 4),
        "pnl_gross_if_closed_at_ask": round(gross, 2),
        "pnl_net_if_closed_at_ask": round(gross - close_fee, 2),
        "delta_units": round(-bs_delta(spot, pos["strike"], max(hours, 0.25) / HOURS_PER_YEAR, iv,
                                       pos["kind"]) * units * (1 if short else -1), 4),
    }
    out["stop_spot"] = (spot_for_price(pos["stop"], pos["strike"], hours, iv, pos["kind"], spot)
                        if pos["stop"] else None)
    out["liq_spot"] = (spot_for_price(pos["liq"], pos["strike"], hours, iv, pos["kind"], spot)
                       if pos["liq"] else None)
    flags = []
    if short and pos["stop"] and pos["liq"] and pos["liq"] <= pos["stop"]:
        flags.append("LIQUIDATION fires BEFORE your stop — add margin / Auto Top-up or tighten the stop")
    if short and pos["stop"] is None:
        flags.append("no stop set — liquidation is your only exit")
    if short and pos["stop"] is not None and pos["stop"] < pos["entry"]:
        flags.append(f"stop is below entry -> locks in profit (~{(pos['entry']-pos['stop'])*units:.2f} before fees)")
    if equity and short and pos["stop"] is not None and pos["stop"] > pos["entry"]:
        loss = (pos["stop"] - pos["entry"]) * units + fees.option_fee(pos["stop"], units, spot)
        out["loss_at_stop"] = round(loss, 2)
        out["loss_at_stop_pct_equity"] = round(loss / equity * 100, 1)
        if loss / equity > 0.05:
            flags.append(f"loss at stop = {loss/equity*100:.1f}% of equity (> 5%) — size down")
    if out["stop_spot"] and out["liq_spot"] and short:
        gap = abs(out["stop_spot"] - out["liq_spot"]) / spot * 100
        out["stop_to_liq_gap_pct"] = round(gap, 2)
        if gap < 0.5:
            flags.append(f"stop and liquidation only {gap:.2f}% apart — a gap can skip the stop")
    if hours < 18:
        flags.append(f"{hours:.0f}h to expiry — gamma zone, small moves swing the premium hard")
    if short and (pos["stop"] or pos["liq"]):
        out.update(barrier_odds(spot, pos["strike"], pos["kind"], max(hours, 0.25), iv,
                                pos["stop"], pos["liq"], paths=paths))
    elif short:
        out.update(barrier_odds(spot, pos["strike"], pos["kind"], max(hours, 0.25), iv,
                                None, None, paths=paths))
    stop_guards = bool(short and pos["stop"] and pos["liq"] and pos["stop"] < pos["liq"])
    out["stop_guards_liq"] = stop_guards
    if not stop_guards and out.get("p_liq") is not None and out.get("p_itm") is not None and pos["liq"] and \
            out["p_liq"] > out["p_itm"] * 1.2:
        flags.append(f"liquidation ({out['p_liq']*100:.0f}%) is likelier than losing at expiry "
                     f"({out['p_itm']*100:.0f}%) — the margin, not the thesis, is the risk")
    out["flags"] = flags
    return out


def _fmt(x, nd=2):
    return "—" if x is None else f"{x:,.{nd}f}"


def main() -> None:
    from .entry_scan import load_options
    ap = argparse.ArgumentParser(description="Health check for held short options (read-only).")
    ap.add_argument("--pos", action="append", required=True,
                    help="SYMBOL:SIZE:ENTRY[:stop=X][:liq=Y]  e.g. P-ETH-2600-071026:-2:6.0:stop=3.51:liq=19.34")
    ap.add_argument("--equity", type=float, default=None, help="account equity in USD (for % of equity)")
    ap.add_argument("--paths", type=int, default=3000)
    a = ap.parse_args()
    poss = [parse_pos(p) for p in a.pos]
    chains = {asset: {r["symbol"]: r for r in load_options(asset)} for asset in {p["asset"] for p in poss}}
    now = dt.datetime.now(dt.timezone.utc)
    print("=" * 100)
    print(f"  OPEN-POSITION HEALTH CHECK   {now:%Y-%m-%d %H:%M UTC}   (read-only; odds = model, not forecast)")
    print("=" * 100)
    net_delta: dict[str, float] = {}
    for p in poss:
        row = chains[p["asset"]].get(p["symbol"])
        if not row:
            print(f"\n  {p['symbol']}: not found in the live chain (expired or mistyped)")
            continue
        r = assess(p, row, a.equity, now, a.paths)
        net_delta[p["asset"]] = net_delta.get(p["asset"], 0.0) + r["delta_units"]
        nd = 2 if p["asset"] == "ETH" else 0
        print(f"\n  {r['symbol']}  size {r['size']:+g} @ {r['entry']}  ·  {r['hours_left']}h left  ·  "
              f"spot {r['spot']:,.{nd}f}  ·  IV {r['iv']*100:.1f}%")
        print(f"    mark {r['mark']:.2f} (bid {_fmt(row.get('bid'))} / ask {_fmt(row.get('ask'))})  ·  "
              f"P&L if closed now {r['pnl_gross_if_closed_at_ask']:+.2f} gross / {r['pnl_net_if_closed_at_ask']:+.2f} net of fee")
        if r["stop"]:
            print(f"    stop {r['stop']} fires near {p['asset']} {_fmt(r['stop_spot'], nd)}"
                  + (f"  ·  loss at stop {r['loss_at_stop']:.2f} ({r['loss_at_stop_pct_equity']}% of equity)"
                     if r.get("loss_at_stop") is not None else ""))
        if r["liq"]:
            print(f"    liquidation {r['liq']} fires near {p['asset']} {_fmt(r['liq_spot'], nd)}"
                  + (f"  ·  stop↔liq gap {r['stop_to_liq_gap_pct']}%" if r.get("stop_to_liq_gap_pct") is not None else ""))
        liq_note = " (only if the stop is skipped by a gap)" if r.get("stop_guards_liq") else ""
        print(f"    odds to expiry: stop {_fmt((r.get('p_stop') or 0)*100,0)}% · liquidation "
              f"{_fmt((r.get('p_liq') or 0)*100,0)}%{liq_note} · ITM at expiry {_fmt(r.get('p_itm',0)*100,0)}%")
        for f in r["flags"]:
            print(f"    ⚠ {f}")
    if net_delta:
        print("\n  NET DELTA (underlying units; + = you gain if price rises): "
              + " · ".join(f"{k} {v:+.3f}" for k, v in net_delta.items()))
        print("  Adding a new trade on the same asset and side stacks this exposure — size the NEW trade "
              "so the combined loss at stops stays within your per-trade risk.")
    print("\n  Not advice — you manage and place every order.\n")


if __name__ == "__main__":
    main()

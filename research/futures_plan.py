"""
Read-only PERP short trade-card (Delta Exchange India).

Companion to the options watch_plan. Once TradingView has given you a coin's
resistance (entry) and support (take-profit), this pulls the live perp ticker and
lays out the whole short as a card: entry -> TP (support), a suggested invalidation
stop, the reward:risk, the funding carry you earn (or pay) holding it, and the
liquidity you must be able to exit into. It never trades and needs no key.

    python -m research.futures_plan --coin SOL --entry 121 --tp 112
    python -m research.futures_plan --symbol SOLUSD --entry 121 --tp 112 --stop 124
    python -m research.futures_plan --coin SOL --entry 121 --tp 112 --risk-pct 1 --equity 30000

Stop defaults to entry * (1 + --stop-pct) i.e. a fixed % above resistance if you
don't pass one. Reward:risk = (entry - tp) / (stop - entry). Funding is shown per
interval and as an approximate daily carry (assumes an 8h interval; override with
--funding-hours). Positive funding pays you while short.
"""
from __future__ import annotations
import argparse
import datetime as dt

from .delta_api import DeltaPublicClient


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fetch(symbol: str) -> dict | None:
    for t in DeltaPublicClient().get(
        "/tickers", {"contract_types": "perpetual_futures"}
    )["result"]:
        if str(t.get("symbol", "")).upper() == symbol.upper():
            return t
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--side", choices=["short", "long"], default="short",
                    help="short = sell resistance -> TP at support; long = buy support -> TP at resistance")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--symbol", help="Delta perp symbol, e.g. SOLUSD")
    g.add_argument("--coin", help="bare coin, e.g. SOL (maps to <COIN>USD)")
    ap.add_argument("--entry", type=float, required=True,
                    help="short: resistance you sell into | long: support you buy at")
    ap.add_argument("--tp", type=float, required=True,
                    help="short: take-profit near support | long: take-profit near resistance")
    ap.add_argument("--stop", type=float, default=None,
                    help="invalidation stop (default: entry -/+ --stop-pct depending on side)")
    ap.add_argument("--stop-pct", type=float, default=1.5,
                    help="default stop distance from entry, %% (above for short, below for long)")
    ap.add_argument("--risk-pct", type=float, default=None,
                    help="account %% to risk (for position sizing, informational)")
    ap.add_argument("--equity", type=float, default=None,
                    help="account equity in USD (for position sizing, informational)")
    ap.add_argument("--funding-hours", type=float, default=8.0,
                    help="funding interval in hours (Delta baseline ~8h)")
    ap.add_argument("--leverage", type=float, default=None,
                    help="isolated leverage to set (default: suggest a safe one from the stop)")
    a = ap.parse_args()

    symbol = a.symbol or f"{a.coin.upper()}USD"
    t = fetch(symbol)
    now = dt.datetime.now(dt.timezone.utc)
    if not t:
        print(f"  No live perp ticker for {symbol}. Check the symbol (e.g. SOLUSD, XRPUSD).")
        return

    mark = _f(t.get("mark_price")) or _f(t.get("close"))
    spot = _f(t.get("spot_price"))
    hi, lo = _f(t.get("mark_high_24h")), _f(t.get("mark_low_24h"))
    funding = _f(t.get("funding_rate")) or 0.0
    oi_usd = _f(t.get("oi_value_usd")) or 0.0
    turnover = _f(t.get("turnover_usd")) or 0.0
    contract_value = _f(t.get("contract_value")) or 1.0   # units of underlying per lot
    max_lev = _f(t.get("leverage")) or 100.0
    q = t.get("quotes", {}) or {}
    bid, ask = _f(q.get("best_bid")), _f(q.get("best_ask"))
    spread_pct = ((ask - bid) / ((ask + bid) / 2) * 100) if (bid and ask) else None
    basis_pct = ((mark - spot) / spot * 100) if (spot and spot > 0) else None

    is_long = a.side == "long"
    label = "LONG" if is_long else "SHORT"
    entry, tp = a.entry, a.tp
    if a.stop is not None:
        stop = a.stop
    else:
        stop = entry * (1 - a.stop_pct / 100) if is_long else entry * (1 + a.stop_pct / 100)

    if is_long:
        reward = tp - entry                  # long: profit if price rises to TP (resistance)
        risk = entry - stop                  # loss if price falls to stop (below support)
        to_entry_pct = (mark - entry) / mark * 100  # how far price must FALL to arm the long
    else:
        reward = entry - tp                  # short: profit if price falls to TP (support)
        risk = stop - entry                  # loss if price rises to stop (above resistance)
        to_entry_pct = (entry - mark) / mark * 100  # how far price must RISE to arm the short
    rr = (reward / risk) if risk > 0 else None
    reward_pct = reward / entry * 100
    risk_pct = risk / entry * 100

    # funding carry: positive funding pays the short and costs the long
    daily_funding = funding * (24.0 / a.funding_hours)
    carry_sign = -1.0 if is_long else 1.0    # + means credited to YOUR side

    print("=" * 92)
    print(f"  DELTA INDIA — READ-ONLY {label} CARD   {symbol}   (data only; you place the order)")
    print(f"  {now:%Y-%m-%d %H:%M UTC}   mark {mark:,.6g}   spot {spot:,.6g}"
          + (f"   basis {basis_pct:+.3f}%" if basis_pct is not None else ""))
    print("=" * 92)

    if is_long:
        arm = "ARMED — price already at/below entry" if mark <= entry \
            else f"WAIT — needs -{to_entry_pct:.2f}% (a dip) to reach entry"
    else:
        arm = "ARMED — price already at/above entry" if mark >= entry \
            else f"WAIT — needs +{to_entry_pct:.2f}% to reach entry"
    print(f"\n  {label} {symbol}   [{arm}]")
    if is_long:
        print(f"    Entry  (support)      {entry:,.6g}")
        print(f"    Stop   (invalidation) {stop:,.6g}   (-{risk_pct:.2f}% below entry)")
        print(f"    TP     (resistance)   {tp:,.6g}   (+{reward_pct:.2f}% above entry)")
    else:
        print(f"    Entry  (resistance)   {entry:,.6g}")
        print(f"    Stop   (invalidation) {stop:,.6g}   (+{risk_pct:.2f}% above entry)")
        print(f"    TP     (support)      {tp:,.6g}   (-{reward_pct:.2f}% below entry)")
    print(f"    Reward:Risk           {rr:.2f} : 1" if rr else "    Reward:Risk    n/a (bad stop)")
    print(f"    24h range             {lo:,.6g}  ..  {hi:,.6g}")
    # Reachability: is the TP within a sane expected move, or is the R:R a mirage? Use the 24h
    # range as a cheap daily-volatility proxy (no extra candle pull). A great R:R to a target
    # several days' range away rarely pays before the stop or the thesis decays.
    range24_pct = ((hi - lo) / mark * 100) if (hi and lo and mark) else None
    reward_ranges = (reward_pct / range24_pct) if (range24_pct and range24_pct > 0) else None
    if reward_ranges is not None:
        reach = ("well within a session" if reward_ranges <= 1.0
                 else "~a full session's range" if reward_ranges <= 1.5
                 else "a multi-session move" if reward_ranges <= 2.5
                 else "a LARGE move — target may be out of reach in one swing")
        print(f"    Reachability          TP is {reward_ranges:.1f}x the 24h range away  ({reach})")

    print("\n  MICROSTRUCTURE")
    if funding > 0:
        fund_note = "longs pay shorts (carry against you)" if is_long else "longs pay shorts (you earn carry)"
    elif funding < 0:
        fund_note = "shorts pay longs (you earn carry)" if is_long else "shorts pay longs (carry against you)"
    else:
        fund_note = "flat"
    print(f"    Funding      {funding:+.4f}% / {a.funding_hours:.0f}h   ~{daily_funding:+.3f}%/day   {fund_note}")
    print(f"    Open interest ${oi_usd:,.0f}")
    print(f"    24h turnover  ${turnover:,.0f}")
    if spread_pct is not None:
        liq = "OK" if spread_pct < 0.10 else ("wide" if spread_pct < 0.30 else "VERY WIDE — hard to exit")
        print(f"    Bid/ask spread {spread_pct:.3f}%   ({liq})")

    if a.risk_pct and a.equity and risk > 0:
        risk_usd = a.equity * a.risk_pct / 100
        notional = risk_usd / (risk_pct / 100)          # notional so a stop-out = risk_usd
        lot_usd = contract_value * entry                # $ value of one lot at entry
        lots = notional / lot_usd if lot_usd > 0 else 0
        lots_int = max(1, round(lots))
        actual_notional = lots_int * lot_usd
        actual_risk = actual_notional * (risk_pct / 100)
        # leverage: keep liquidation well BEYOND the stop. Isolated liq ~ (1/L) from entry;
        # require liq distance >= 2x the stop distance -> L <= 1/(2*stop_frac).
        safe_lev = max(1.0, 1.0 / (2 * (risk_pct / 100)))
        sugg_lev = min(max_lev, float(int(safe_lev)))
        lev = a.leverage if a.leverage is not None else sugg_lev
        margin = actual_notional / lev
        eff_lev = actual_notional / a.equity
        liq_pct = 100.0 / lev                            # rough isolated liq distance from entry
        daily_carry = actual_notional * daily_funding / 100 * carry_sign  # + = credited to YOUR side
        liq_dir = "below" if is_long else "above"
        liq_sign = "-" if is_long else "+"
        print("\n  POSITION, LEVERAGE & LOTS (fixed-fractional)")
        print(f"    Risk budget    {a.risk_pct:.2f}% of {a.equity:,.0f} = {risk_usd:,.2f} at the stop")
        print(f"    Contract       1 lot = {contract_value:g} {symbol.replace('USD','')}  (~{lot_usd:,.2f} at entry)")
        print(f"    Size           {lots_int} lots  ->  notional ~{actual_notional:,.0f}  "
              f"(stop-out loses ~{actual_risk:,.2f})")
        print(f"    Leverage       {lev:.0f}x isolated  (max {max_lev:.0f}x; suggest <= {sugg_lev:.0f}x so liq clears the stop)")
        print(f"    Margin         ~{margin:,.2f} locked  |  effective acct leverage {eff_lev:.2f}x")
        liq_flag = "OK — liq past stop" if liq_pct > risk_pct * 1.8 else "TOO HIGH — liq near stop, cut leverage"
        print(f"    Liquidation    ~{liq_sign}{liq_pct:.1f}% {liq_dir} entry vs stop {liq_sign}{risk_pct:.2f}%  [{liq_flag}]")
        print(f"    Funding carry  ~{daily_carry:+,.2f}/day on the position ({'credit' if daily_carry>=0 else 'debit'} to {a.side})")
        if lots > 0 and lots_int / lots > 1.5:
            print(f"    ! 1 lot already exceeds your risk budget — account too small for {a.risk_pct:.1f}% risk here")

    print("\n  FLAGS")
    fl = []
    if rr and rr < 2:
        fl.append(f"R:R {rr:.2f} below 2:1 — thin edge")
    if reward_ranges is not None and reward_ranges > 2.5:
        fl.append(f"TP is {reward_ranges:.1f}x the 24h range away — good R:R but the target may be out of reach in one swing")
    if is_long and funding > 0:
        fl.append("positive funding — you pay to hold the long")
    if (not is_long) and funding < 0:
        fl.append("negative funding — you pay to hold the short")
    if turnover < 2_000_000:
        fl.append("thin turnover — exit liquidity risk")
    if spread_pct is not None and spread_pct >= 0.30:
        fl.append("very wide spread")
    if is_long and mark > entry and to_entry_pct > 3:
        fl.append(f"entry is far below (-{to_entry_pct:.1f}%) — this is a resting plan, not a live entry")
    if (not is_long) and mark < entry and to_entry_pct > 3:
        fl.append(f"entry is far (+{to_entry_pct:.1f}%) — this is a resting plan, not a live entry")
    print("    " + ("; ".join(fl) if fl else "none — levels and liquidity look clean"))

    if is_long:
        print("\n  Long only WITH a confirmed bullish reversal at entry and the higher-timeframe trend.")
    else:
        print("\n  Short only WITH a confirmed bearish reversal at entry and the higher-timeframe trend.")
    print("  Lock profit early, trail the stop, hard max-loss before any add, never average a loser.")
    print("  Read-only. This tool never trades — you place every order yourself. Not advice.\n")


if __name__ == "__main__":
    main()

"""Trade journal from Delta India's own CSV exports — READ-ONLY, offline, no API key.

Export from Delta: Transaction Log -> "Order History" and "Asset History" (CSV). Then:

    python -m research.journal --orders ~/Downloads/Delta-TransactionLog-OrderHistory.csv \
                               --assets ~/Downloads/Delta-TransactionLog-AssetHistory.csv

What it answers (the things the scanner/desk can't see):
  * Are you actually making money NET of fees/GST, per contract and per category
    (short calls / short puts / perps)?
  * How much of the gross edge do fees eat (fee drag)?
  * How much of each option credit do you capture, and how long do you hold?
  * How big was each position versus the account (underlying notional / equity, and the loss
    a 2x-credit stop would have cost as % of equity) — i.e. was the size survivable?

Numbers come straight from Delta's "Realised P&L", "Trading Fees" (incl. GST) and the asset
ledger; nothing is modeled except the 2x-credit stop-loss estimate. Pure helpers are unit-tested.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import re
import statistics as st
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

OPTION_RE = re.compile(r"^([CP])-([A-Z]+)-(\d+(?:\.\d+)?)-(\d{6})$")

# Sizing thresholds used for the risk flags (a premium seller's survivability rules)
MAX_STOP_LOSS_PCT_EQUITY = 5.0     # a 2x-credit stop should cost <= 5% of equity
MAX_NOTIONAL_X_EQUITY = 20.0       # underlying notional / equity above this = very high leverage
STOP_CREDIT_MULTIPLE = 2.0


def _ts(s: str) -> dt.datetime:
    """Delta timestamps look like '2026-10-05 12:40:43.48+05:30 IST Asia/Kolkata'."""
    return dt.datetime.fromisoformat(s.split(" IST")[0].strip())


def _f(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def parse_option(symbol: str) -> dict[str, Any] | None:
    """'P-ETH-2600-071026' -> kind/asset/strike/expiry (12:00 UTC). None for non-options."""
    m = OPTION_RE.match(symbol or "")
    if not m:
        return None
    k, asset, strike, ddmmyy = m.groups()
    expiry = dt.datetime.strptime(ddmmyy, "%d%m%y").replace(hour=12, tzinfo=dt.timezone.utc)
    return {"kind": "call" if k == "C" else "put", "asset": asset, "strike": float(strike),
            "expiry": expiry}


def load_csv(path: str | Path) -> list[dict[str, str]]:
    with open(Path(path).expanduser(), newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def filled(orders: Iterable[dict[str, str]]) -> list[dict[str, Any]]:
    """Closed (filled) orders only, typed and time-sorted."""
    out = []
    for o in orders:
        if (o.get("Status") or "").strip() != "closed":
            continue
        out.append({
            "time": _ts(o["Time"]), "contract": o["Contract"], "side": o["Side"],
            "price": _f(o.get("Exec.Price")), "units": _f(o.get("Order Value")),
            "fee": _f(o.get("Trading Fees")), "cash": _f(o.get("Cashflow")),
            "realized": _f(o.get("Realised P&L")), "type": o.get("Order Type", ""),
        })
    out.sort(key=lambda r: r["time"])
    return out


def balance_series(assets: Iterable[dict[str, str]]) -> list[tuple[dt.datetime, float]]:
    s = [(_ts(a["Date"]), _f(a.get("Balance"))) for a in assets
         if (a.get("Asset Symbol") or "USD") == "USD" and a.get("Balance") not in (None, "")]
    s.sort()
    return s


def equity_at(series: list[tuple[dt.datetime, float]], t: dt.datetime) -> float | None:
    eq = None
    for tt, v in series:
        if tt <= t:
            eq = v
        else:
            break
    return eq if eq is not None else (series[0][1] if series else None)


def summarize_contract(contract: str, fills: list[dict[str, Any]],
                       bal: list[tuple[dt.datetime, float]]) -> dict[str, Any]:
    """One contract's round trip(s): realized, fees, net, and for options credit capture + size."""
    realized = sum(f["realized"] for f in fills)
    fees = sum(f["fee"] for f in fills)
    out: dict[str, Any] = {
        "contract": contract, "fills": len(fills), "realized": round(realized, 4),
        "fees": round(fees, 4), "net": round(realized - fees, 4),
        "first": fills[0]["time"], "last": fills[-1]["time"],
        "hold_hours": round((fills[-1]["time"] - fills[0]["time"]).total_seconds() / 3600, 2),
        "category": "perp",
    }
    opt = parse_option(contract)
    if not opt:
        return out
    opens_side = fills[0]["side"]                    # sell first = short premium
    opens = [f for f in fills if f["side"] == opens_side]
    closes = [f for f in fills if f["side"] != opens_side]
    units_open = sum(f["units"] for f in opens)
    units_close = sum(f["units"] for f in closes)
    avg_open = sum(f["price"] * f["units"] for f in opens) / units_open if units_open else 0.0
    avg_close = sum(f["price"] * f["units"] for f in closes) / units_close if units_close else 0.0
    short = opens_side == "sell"
    credit = avg_open * units_open
    capture = ((avg_open - avg_close) / avg_open * 100) if (short and avg_open) else None
    eq = equity_at(bal, fills[0]["time"])
    notional = units_open * opt["strike"]
    stop_loss = credit * (STOP_CREDIT_MULTIPLE - 1.0) if short else credit
    hrs_left = (opt["expiry"] - fills[0]["time"].astimezone(dt.timezone.utc)).total_seconds() / 3600
    out.update({
        "category": f"{'short' if short else 'long'} {opt['kind']}",
        "asset": opt["asset"], "strike": opt["strike"], "expiry": opt["expiry"],
        "units": round(units_open, 4), "avg_open": round(avg_open, 4), "avg_close": round(avg_close, 4),
        "credit": round(credit, 4), "capture_pct": None if capture is None else round(capture, 1),
        "hours_left_at_entry": round(hrs_left, 1),
        "fee_pct_of_gross": round(fees / realized * 100, 1) if realized > 0 else None,
        "equity_at_entry": eq,
        "notional_x_equity": round(notional / eq, 1) if eq else None,
        "stop_loss_pct_equity": round(stop_loss / eq * 100, 1) if eq else None,
        "still_open": abs(units_open - units_close) > 1e-9,
    })
    flags = []
    if out["stop_loss_pct_equity"] is not None and out["stop_loss_pct_equity"] > MAX_STOP_LOSS_PCT_EQUITY:
        flags.append(f"oversized: a {STOP_CREDIT_MULTIPLE:g}x-credit stop = {out['stop_loss_pct_equity']}% of equity")
    if out["notional_x_equity"] is not None and out["notional_x_equity"] > MAX_NOTIONAL_X_EQUITY:
        flags.append(f"{out['notional_x_equity']}x underlying notional vs equity")
    if short and opt["kind"] == "call":
        flags.append("short call — only with the trend or after a confirmed rejection (calls lost in the bull-regime backtest)")
    out["flags"] = flags
    return out


def build(orders: list[dict[str, str]], assets: list[dict[str, str]]) -> dict[str, Any]:
    fills = filled(orders)
    bal = balance_series(assets)
    by = defaultdict(list)
    for f in fills:
        by[f["contract"]].append(f)
    contracts = [summarize_contract(c, fs, bal) for c, fs in by.items()]
    contracts.sort(key=lambda r: r["first"])

    cats: dict[str, dict[str, Any]] = {}
    for r in contracts:
        c = cats.setdefault(r["category"], {"n": 0, "wins": 0, "realized": 0.0, "fees": 0.0, "net": 0.0})
        c["n"] += 1
        c["wins"] += r["net"] > 0
        c["realized"] += r["realized"]
        c["fees"] += r["fees"]
        c["net"] += r["net"]

    ledger = defaultdict(float)
    for a in assets:
        ledger[a.get("Transaction type", "")] += _f(a.get("Amount with GST"))
    deposits = ledger.get("deposit", 0.0)
    withdrawals = -ledger.get("withdrawal", 0.0)
    balance = bal[-1][1] if bal else None
    growth = (balance + withdrawals - deposits) if balance is not None else None

    opts = [r for r in contracts if r["category"].startswith("short")]
    caps = [r["capture_pct"] for r in opts if r.get("capture_pct") is not None]
    holds = [r["hold_hours"] for r in opts]
    gross = sum(r["realized"] for r in contracts)
    fees = sum(r["fees"] for r in contracts)
    return {
        "contracts": contracts, "categories": cats,
        "account": {"deposits": deposits, "withdrawals": withdrawals, "balance": balance,
                    "growth": growth, "growth_pct": (growth / deposits * 100) if deposits and growth is not None else None,
                    "funding": ledger.get("funding", 0.0),
                    "fee_credits_used": -ledger.get("trading_fee_credits_paid", 0.0)},
        "totals": {"realized": gross, "fees": fees, "net": gross - fees,
                   "fee_drag_pct": fees / gross * 100 if gross > 0 else None},
        "options": {"n": len(opts), "wins": sum(r["net"] > 0 for r in opts),
                    "median_capture_pct": st.median(caps) if caps else None,
                    "median_hold_h": st.median(holds) if holds else None,
                    "oversized": sum(any(f.startswith("oversized") for f in r["flags"]) for r in opts)},
    }


def _print(res: dict[str, Any]) -> None:
    acc, tot, op = res["account"], res["totals"], res["options"]
    print("=" * 112)
    print("  TRADE JOURNAL — from Delta's own CSV exports (read-only)")
    print("=" * 112)
    if acc["balance"] is not None:
        print(f"  Account: deposits {acc['deposits']:,.2f} · withdrawals {acc['withdrawals']:,.2f} · balance "
              f"{acc['balance']:,.2f} · growth {acc['growth']:+,.2f}"
              + (f" ({acc['growth_pct']:+.1f}%)" if acc["growth_pct"] is not None else "")
              + f" · funding {acc['funding']:+.2f} · fee credits used {acc['fee_credits_used']:.2f}")
    print(f"  Realized {tot['realized']:,.2f} − fees {tot['fees']:,.2f} = NET {tot['net']:,.2f}"
          + (f"   (fees ate {tot['fee_drag_pct']:.0f}% of gross)" if tot["fee_drag_pct"] is not None else ""))
    print(f"\n  {'category':<12}{'n':>4}{'wins':>6}{'realized':>11}{'fees':>9}{'net':>10}")
    for k, c in sorted(res["categories"].items()):
        print(f"  {k:<12}{c['n']:>4}{c['wins']:>6}{c['realized']:>11.2f}{c['fees']:>9.2f}{c['net']:>10.2f}")
    print(f"\n  {'option contract':<21}{'open':>9}{'close':>9}{'units':>7}{'capt%':>6}{'hold h':>7}{'h left':>7}"
          f"{'net':>8}{'fee%':>6}{'notl/eq':>8}{'stop%eq':>8}")
    for r in res["contracts"]:
        if not r["category"].startswith(("short", "long")):
            continue
        print(f"  {r['contract']:<21}{r['avg_open']:>9.2f}{r['avg_close']:>9.2f}{r['units']:>7.3f}"
              f"{(r['capture_pct'] if r['capture_pct'] is not None else 0):>6.0f}{r['hold_hours']:>7.1f}"
              f"{r['hours_left_at_entry']:>7.1f}{r['net']:>8.2f}{(r['fee_pct_of_gross'] or 0):>6.0f}"
              f"{(r['notional_x_equity'] or 0):>7.1f}x{(r['stop_loss_pct_equity'] or 0):>7.1f}%"
              + ("  NOT FLAT*" if r["still_open"] else ""))
    perps = [r for r in res["contracts"] if r["category"] == "perp"]
    if perps:
        losers = [r for r in perps if r["net"] < 0]
        print(f"\n  perps: {len(perps)} contracts, net {sum(r['net'] for r in perps):+.2f}; losers: "
              + (", ".join(f"{r['contract']} {r['net']:+.2f}" for r in losers) or "none"))
    print(f"\n  Options: {op['wins']}/{op['n']} net winners · median credit captured "
          f"{op['median_capture_pct']}% · median hold {op['median_hold_h']}h · "
          f"{op['oversized']} trade(s) where a {STOP_CREDIT_MULTIPLE:g}x-credit stop > {MAX_STOP_LOSS_PCT_EQUITY:g}% of equity")
    flagged = [r for r in res["contracts"] if r.get("flags")]
    if flagged:
        print("\n  FLAGS")
        for r in flagged:
            print(f"   {r['contract']:<21} " + " · ".join(r["flags"]))
    if any(r.get("still_open") for r in res["contracts"]):
        print("\n  * NOT FLAT = buys != sells in the export (a leftover leg expired/settled or is still open);")
        print("    capture% / avg close include that leg, so read those rows with care.")
    print("\n  Read-only. Realized/fees are Delta's own numbers; the stop-loss % is a 2x-credit estimate.\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Trade journal from Delta CSV exports (read-only).")
    ap.add_argument("--orders", default="~/Downloads/Delta-TransactionLog-OrderHistory.csv")
    ap.add_argument("--assets", default="~/Downloads/Delta-TransactionLog-AssetHistory.csv")
    a = ap.parse_args()
    _print(build(load_csv(a.orders), load_csv(a.assets)))


if __name__ == "__main__":
    main()

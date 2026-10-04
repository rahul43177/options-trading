"""Unified, read-only BTC/ETH options decision engine.

The engine combines structured TradingView context with live Delta public option
quotes.  It ranks conditional *paper* candidates and states what evidence is
missing.  It contains no authenticated API client and no order endpoint.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DEFAULT_BUFFER, ROOT, UNDERLYINGS
from .entry_scan import load_options
from .storage import connect


@dataclass(frozen=True)
class EngineConfig:
    delta_min: float = 0.10
    delta_max: float = 0.25
    delta_target: float = 0.16
    min_oi: float = 10.0
    max_spread_pct: float = 15.0
    max_hours: float = 96.0
    quote_max_age_seconds: float = 180.0
    context_max_age_minutes: float = 30.0


def _parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = float(value) / 1_000_000 if float(value) > 10_000_000_000 else float(value)
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _trend_score(side: str, setup: dict[str, Any]) -> tuple[float, str]:
    biases = [str(setup.get(key, "neutral")).lower() for key in ("weekly_bias", "daily_bias")]
    desired = "bearish" if side == "CALL" else "bullish"
    opposite = "bullish" if desired == "bearish" else "bearish"
    aligned = biases.count(desired)
    opposed = biases.count(opposite)
    score = 10.0 * aligned + 4.0 * (2 - aligned - opposed)
    if opposed == 2:
        score -= 10.0
    return max(0.0, score), f"{aligned}/2 higher timeframes support the {side.lower()}-short thesis"


def _state_score(setup: dict[str, Any]) -> tuple[float, str]:
    state = str(setup.get("state", "WAIT")).upper()
    confirmed = bool(setup.get("confirmation", False))
    if state == "VALID" and confirmed:
        return 20.0, "reversal trigger confirmed"
    if state == "ARMED":
        return (14.0 if confirmed else 9.0), "zone armed" + (" and confirmed" if confirmed else "; confirmation pending")
    return (5.0 if confirmed else 0.0), f"state={state}; confirmation={'yes' if confirmed else 'no'}"


def _liquidity_score(oi: float, spread_pct: float, bid_size: float | None, ask_size: float | None, min_oi: float) -> tuple[float, str]:
    oi_score = _clamp(oi / max(min_oi * 5, 1) * 10, 0, 10)
    if spread_pct <= 3:
        spread_score = 10.0
    elif spread_pct <= 5:
        spread_score = 8.0
    elif spread_pct <= 8:
        spread_score = 6.0
    elif spread_pct <= 12:
        spread_score = 3.0
    else:
        spread_score = 0.0
    depth_score = 0.0
    if bid_size is not None and ask_size is not None:
        depth_score = _clamp(math.log1p(max(0.0, min(bid_size, ask_size))) / math.log(101) * 5, 0, 5)
    return oi_score + spread_score + depth_score, f"OI {oi:.0f}, spread {spread_pct:.1f}%, displayed depth score {depth_score:.1f}/5"


def _vol_score(iv: float | None, rv: float | None) -> tuple[float, str, str | None]:
    if iv is None or rv is None or rv <= 0:
        return 0.0, "IV-versus-forward-RV edge unverified", "iv_rv_edge_unverified"
    ratio = iv / rv
    # A premium alone is not proof of edge; this is deliberately capped at 15/100.
    score = _clamp((ratio - 0.90) / 0.50 * 15, 0, 15)
    return score, f"mark IV / forward-RV forecast = {ratio:.2f}", None


def _fresh_account(context: dict[str, Any], now: datetime) -> tuple[bool, str | None]:
    account = context.get("account_state")
    if not isinstance(account, dict):
        return False, "account_margin_unverified"
    stamp = _parse_time(account.get("as_of_utc") or account.get("timestamp"))
    if stamp is None or (now - stamp).total_seconds() > 300:
        return False, "account_margin_stale"
    available = account.get("available_balance")
    try:
        available_ok = available is not None and float(available) > 0
    except (TypeError, ValueError):
        available_ok = False
    if not available_ok:
        return False, "account_available_balance_missing"
    margin_fields_present = account.get("margin_check_passed") is True or (
        account.get("initial_margin") is not None and account.get("maintenance_margin") is not None
    )
    if not margin_fields_present:
        return False, "account_margin_unverified"
    return True, None


def _context_age_gap(context: dict[str, Any], now: datetime, max_minutes: float) -> str | None:
    stamp = _parse_time(context.get("generated_at_utc"))
    if stamp is None:
        return "tradingview_context_timestamp_missing"
    age = (now - stamp).total_seconds()
    if age < -60:
        return "tradingview_context_timestamp_in_future"
    if age > max_minutes * 60:
        return "tradingview_context_stale"
    return None


def _candidate(option: dict[str, Any], setup: dict[str, Any], cfg: EngineConfig, now: datetime) -> dict[str, Any]:
    side = str(setup.get("side", "")).upper()
    strike, bid, ask, delta = (option.get(x) for x in ("strike", "bid", "ask", "delta"))
    failures: list[str] = []
    for name, value in (("strike", strike), ("bid", bid), ("ask", ask), ("delta", delta)):
        if value is None:
            failures.append(f"missing_{name}")
    if failures:
        return {"symbol": option.get("symbol"), "side": side, "eligible": False, "hard_failures": failures}

    expiry = option.get("expiry")
    hours = (expiry - now).total_seconds() / 3600 if expiry else None
    mid = (bid + ask) / 2
    spread_pct = ((ask - bid) / mid * 100) if mid > 0 else float("inf")
    zone_low = float(setup.get("zone_low", setup.get("zone", 0)))
    zone_high = float(setup.get("zone_high", setup.get("zone", 0)))
    buffer_value = float(setup.get("buffer", DEFAULT_BUFFER.get(str(setup.get("asset", "BTC")).upper(), 0)))
    quote_time = _parse_time(option.get("timestamp_us"))
    quote_age = (now - quote_time).total_seconds() if quote_time else None
    status = str(option.get("product_status") or "operational").lower()

    if bid <= 0 or ask <= 0:
        failures.append("non_executable_quote")
    if ask < bid:
        failures.append("crossed_quote")
    if hours is None or not (0 < hours <= cfg.max_hours):
        failures.append("expiry_outside_window")
    if option.get("oi", 0) < cfg.min_oi:
        failures.append("open_interest_below_floor")
    if spread_pct > cfg.max_spread_pct:
        failures.append("spread_too_wide")
    if not (cfg.delta_min <= abs(delta) <= cfg.delta_max):
        failures.append("delta_outside_band")
    if side == "CALL" and strike < zone_high + buffer_value:
        failures.append("strike_inside_resistance_buffer")
    if side == "PUT" and strike > zone_low - buffer_value:
        failures.append("strike_inside_support_buffer")
    if quote_age is None:
        failures.append("quote_timestamp_missing")
    elif quote_age < -30:
        failures.append("quote_timestamp_in_future")
    elif quote_age > cfg.quote_max_age_seconds:
        failures.append("quote_stale")
    if status not in {"operational", "live", "active", "open"}:
        failures.append("product_not_operational")

    if failures:
        return {
            "symbol": option.get("symbol"), "side": side, "eligible": False,
            "hard_failures": failures, "spread_pct": round(spread_pct, 3),
        }

    trend, trend_reason = _trend_score(side, setup)
    state, state_reason = _state_score(setup)
    liquidity, liquidity_reason = _liquidity_score(
        float(option.get("oi", 0)), spread_pct, option.get("bid_size"), option.get("ask_size"), cfg.min_oi
    )
    delta_score = _clamp(10 - abs(abs(delta) - cfg.delta_target) / 0.09 * 10, 0, 10)
    expiry_score = _clamp(10 - abs(hours - 48) / 48 * 10, 0, 10)
    rv = setup.get("realized_vol_forecast")
    vol, vol_reason, vol_gap = _vol_score(option.get("iv"), float(rv) if rv is not None else None)
    penalty = 0.0
    if bool(setup.get("squeeze", False)):
        penalty += 5.0
    if str(setup.get("event_risk", "unknown")).lower() == "high":
        penalty += 15.0
    if str(setup.get("macro_risk", "neutral")).lower() == "high":
        penalty += 10.0
    score = _clamp(trend + state + liquidity + delta_score + expiry_score + vol - penalty, 0, 100)
    gaps = [vol_gap] if vol_gap else []
    if str(setup.get("event_risk", "unknown")).lower() == "unknown":
        gaps.append("event_calendar_unverified")
    if str(setup.get("macro_risk", "unknown")).lower() == "unknown":
        gaps.append("macro_regime_unverified")
    return {
        "symbol": option["symbol"], "asset": str(setup.get("asset", "")).upper(), "side": side,
        "eligible": True, "score": round(score, 2), "strike": strike, "expiry_hours": round(hours, 2),
        "bid": bid, "ask": ask, "spread_pct": round(spread_pct, 3), "delta": delta,
        "mark_iv": option.get("iv"), "oi": option.get("oi"), "bid_size": option.get("bid_size"),
        "ask_size": option.get("ask_size"), "contract_value": option.get("contract_value"),
        "evidence_gaps": gaps,
        "score_components": {
            "trend": round(trend, 2), "trigger_state": round(state, 2),
            "liquidity": round(liquidity, 2), "delta_fit": round(delta_score, 2),
            "expiry_fit": round(expiry_score, 2), "iv_rv": round(vol, 2), "risk_penalty": round(-penalty, 2),
        },
        "reasons": [trend_reason, state_reason, liquidity_reason, vol_reason],
    }


def setup_ids(setups: list[dict[str, Any]]) -> list[str]:
    """One unique, deterministic id per setup, in input order.

    Several setups may share an asset/side (e.g. a NEAR and a STRUCTURAL PUT band), so asset/side
    alone cannot identify which zone a candidate came from. Uses the setup's own `id` if given,
    else ASSET-SIDE-<band> (band = "near"/"structural" when supplied) or ASSET-SIDE-<index>;
    collisions get a #<index> suffix.
    """
    out: list[str] = []
    seen: set[str] = set()
    for i, s in enumerate(setups):
        sid = str(s.get("id") or f"{str(s.get('asset', '')).upper()}-{str(s.get('side', '')).upper()}-{s.get('band') or i}")
        if sid in seen:
            sid = f"{sid}#{i}"
        seen.add(sid)
        out.append(sid)
    return out


def analyze(context: dict[str, Any], chains: dict[str, list[dict[str, Any]]] | None = None,
            config: EngineConfig = EngineConfig(), now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    context_gap = _context_age_gap(context, now, config.context_max_age_minutes)
    account_ok, account_gap = _fresh_account(context, now)
    results: list[dict[str, Any]] = []
    raw_chains = chains or {}
    live_cache: dict[str, list[dict[str, Any]]] = {}
    setups = context.get("setups", [])
    ids = setup_ids(setups)
    setup_by_id = dict(zip(ids, setups))
    for sid, setup in zip(ids, setups):
        asset = str(setup.get("asset", "")).upper()
        side = str(setup.get("side", "")).upper()
        band = setup.get("band")
        if asset not in UNDERLYINGS or side not in {"CALL", "PUT"}:
            results.append({"asset": asset, "side": side, "setup_id": sid, "band": band, "eligible": False, "hard_failures": ["invalid_setup"]})
            continue
        if setup.get("zone") is None and (setup.get("zone_low") is None or setup.get("zone_high") is None):
            results.append({"asset": asset, "side": side, "setup_id": sid, "band": band, "eligible": False, "hard_failures": ["setup_zone_missing"]})
            continue
        if chains is not None:
            options = raw_chains.get(asset)
        else:
            if asset not in live_cache:
                live_cache[asset] = load_options(asset)
            options = live_cache[asset]
        if not options:
            results.append({"asset": asset, "side": side, "setup_id": sid, "band": band, "eligible": False, "hard_failures": ["no_chain_rows"]})
            continue
        results.extend({**_candidate(o, setup, config, now), "setup_id": sid, "band": band}
                       for o in options if o.get("type") == side)

    eligible = [x for x in results if x.get("eligible")]
    eligible.sort(key=lambda x: (x["score"], x.get("oi") or 0, -x["spread_pct"]), reverse=True)
    global_gaps = [x for x in (context_gap, account_gap) if x]
    winner = eligible[0] if eligible else None
    for row in results:
        row["is_best"] = winner is not None and row is winner
        row["marker"] = "◄ BEST" if row["is_best"] else ("×" if not row.get("eligible") else "")
        if row.get("eligible"):
            row["evidence_gaps"] = sorted(set(row.get("evidence_gaps", []) + global_gaps))

    state = "NO_ELIGIBLE_CANDIDATE"
    if winner:
        setup = setup_by_id.get(winner["setup_id"], {})
        confirmed = bool(setup.get("confirmation")) and str(setup.get("state", "")).upper() == "VALID"
        if winner["evidence_gaps"]:
            state = "BEST_CONDITIONAL_MORE_DATA_REQUIRED"
        elif not confirmed:
            state = "WATCH_FOR_TRIGGER"
        else:
            state = "PAPER_CANDIDATE"

    return {
        "mode": "READ_ONLY_RESEARCH",
        "decision": state,
        "generated_at_utc": now.isoformat(),
        "best_candidate": winner,
        "candidates": sorted(results, key=lambda x: (not x.get("eligible", False), -(x.get("score") or 0))),
        "global_evidence_gaps": global_gaps,
        "account_state_usable_for_risk": account_ok,
        "method": "hard gates first; transparent multi-factor score second; one global best candidate; no order action",
        "config": asdict(config),
    }


def _load_chain_fixture(path: Path) -> dict[str, list[dict[str, Any]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "rows" in payload:
        payload = payload["rows"]
    if isinstance(payload, dict):
        return payload
    grouped: dict[str, list[dict[str, Any]]] = {}
    # Raw Delta rows are normalized to the same shape as entry_scan.load_options.
    for row in payload:
        symbol = str(row.get("symbol", ""))
        asset = symbol.split("-")[1] if symbol.count("-") >= 2 else ""
        if "type" in row:
            normalized = row
        else:
            quotes = row.get("quotes") or {}
            greeks = row.get("greeks") or {}
            bid = float(quotes["best_bid"]) if quotes.get("best_bid") is not None else None
            ask = float(quotes["best_ask"]) if quotes.get("best_ask") is not None else None
            expiry = None
            try:
                expiry = datetime.strptime(symbol.split("-")[-1], "%d%m%y").replace(hour=12, tzinfo=timezone.utc)
            except ValueError:
                pass
            normalized = {
                "symbol": symbol,
                "type": "CALL" if row.get("contract_type") == "call_options" else "PUT",
                "strike": float(row["strike_price"]) if row.get("strike_price") is not None else None,
                "spot": float(row["spot_price"]) if row.get("spot_price") is not None else None,
                "bid": bid, "ask": ask,
                "delta": float(greeks["delta"]) if greeks.get("delta") is not None else None,
                "iv": float(quotes["mark_iv"]) if quotes.get("mark_iv") is not None else None,
                "oi": float(row.get("oi") or 0), "expiry": expiry,
                "timestamp_us": row.get("timestamp"),
                "bid_size": float(quotes["bid_size"]) if quotes.get("bid_size") is not None else None,
                "ask_size": float(quotes["ask_size"]) if quotes.get("ask_size") is not None else None,
                "contract_value": float(row["contract_value"]) if row.get("contract_value") is not None else None,
                "product_status": row.get("product_status") or row.get("state"),
            }
        grouped.setdefault(asset, []).append(normalized)
    return grouped


def _print_human(result: dict[str, Any]) -> None:
    print(f"Decision: {result['decision']}  (read-only)")
    print(f"{'':8} {'contract':<24} {'band':<14} {'score':>6} {'bid':>8} {'ask':>8} {'spr%':>6} {'OI':>8} {'|d|':>5}")
    for row in [x for x in result["candidates"] if x.get("eligible")][:12]:
        print(f"{row['marker']:<8} {row['symbol']:<24} {str(row.get('band') or row.get('setup_id'))[:14]:<14} {row['score']:>6.1f} {row['bid']:>8.2f} {row['ask']:>8.2f} "
              f"{row['spread_pct']:>6.1f} {row['oi']:>8.0f} {abs(row['delta']):>5.2f}")
    if result["best_candidate"]:
        print("\n◄ BEST marks the highest-ranked contract after all hard gates; it is not an instruction to trade.")
        gaps = result["best_candidate"].get("evidence_gaps", [])
        if gaps:
            print("More data required: " + ", ".join(gaps))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", type=Path, required=True, help="structured TradingView/setup JSON")
    parser.add_argument("--chain-json", type=Path, help="optional deterministic fixture; live Delta is default")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--record", action="store_true", help="record the research decision in SQLite")
    args = parser.parse_args()
    context = json.loads(args.context.read_text(encoding="utf-8"))
    chains = _load_chain_fixture(args.chain_json) if args.chain_json else None
    result = analyze(context, chains=chains)
    if args.record:
        con = connect(ROOT / "data/live_option_chain/delta_live.sqlite")
        con.execute(
            "INSERT INTO strategy_decisions(ts_us,decision,details_json) VALUES (?,?,?)",
            (int(time.time() * 1_000_000), result["decision"], json.dumps(result, default=str)),
        )
        con.commit()
        con.close()
    print(json.dumps(result, indent=2, default=str) if args.json else "")
    if not args.json:
        _print_human(result)


if __name__ == "__main__":
    main()

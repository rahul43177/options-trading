"""
LADDER line validator — the bridge between /options-entry and the Options Ladder Pine indicator
(`pine/options_ladder.pine`). Read-only: it parses text and checks geometry, nothing else.

The skill prints one LADDER line per run; the user pastes it into the indicator. A typo there
(a band on the wrong side of spot, a put strike above its trigger zone) would silently draw a
wrong plan on the chart, so the skill runs this first. The grammar mirrors the Pine parser:

  KEY=value pairs separated by ';' (or newlines). Commas/spaces inside numbers are ignored.
  TS, STATUS (WAIT|ARMED|VALID), EXP            text
  NR, SR, NS, SS, TRIG                          band "lo-hi" or a single number
  BEST                                          "P83200@89" (side, strike, optional rest@)
  ALT                                           strikes separated by '|' (max 4)
  INV                                           levels separated by '|' (max 3)
  MP                                            number

CLI:  python -m research.ladder "<LADDER line>" --spot 85319
Exit code 0 = OK (warnings allowed), 1 = errors.
"""
from __future__ import annotations

import argparse
import sys
from typing import Any

BAND_KEYS = ("NR", "SR", "NS", "SS", "TRIG")
TEXT_KEYS = ("TS", "STATUS", "EXP")
KNOWN = set(BAND_KEYS) | set(TEXT_KEYS) | {"BEST", "ALT", "INV", "MP"}
STATUSES = {"WAIT", "ARMED", "VALID"}


def _num(s: str) -> float | None:
    try:
        return float(s.replace(" ", "").replace(",", ""))
    except ValueError:
        return None


def _band(v: str) -> tuple[float, float] | None:
    parts = [_num(p) for p in v.split("-")]
    if not parts or parts[0] is None:
        return None
    a = parts[0]
    b = parts[1] if len(parts) > 1 and parts[1] is not None else a
    return (min(a, b), max(a, b))


def _strike(v: str) -> dict[str, Any] | None:
    t = v.replace(" ", "").upper()
    if len(t) < 2 or t[0] not in "CP":
        return None
    body, _, rest = t[1:].partition("@")
    k = _num(body)
    return None if k is None else {"side": t[0], "strike": k, "rest": rest}


def parse(line: str) -> dict[str, Any]:
    """Parse a LADDER line exactly like the Pine indicator. Pure (unit-tested)."""
    out: dict[str, Any] = {"ALT": [], "INV": [], "unknown": [], "bad": []}
    for item in line.replace("\n", ";").split(";"):
        if "=" not in item:
            continue
        key, _, val = item.partition("=")
        key = key.replace(" ", "").upper()
        if key not in KNOWN:
            out["unknown"].append(key)
        elif key in TEXT_KEYS:
            out[key] = val.strip().upper() if key == "STATUS" else val.strip()
        elif key in BAND_KEYS:
            band = _band(val)
            if band:
                out[key] = band
            else:
                out["bad"].append(key)
        elif key == "MP":
            out["MP"] = _num(val)
        elif key == "BEST":
            best = _strike(val)
            if best:
                out["BEST"] = best
            else:
                out["bad"].append("BEST")
        elif key == "ALT":
            for s in val.split("|"):
                if s.strip():
                    st = _strike(s)
                    if st:
                        out["ALT"].append(st)
                    else:
                        out["bad"].append(f"ALT:{s.strip()}")
            out["ALT"] = out["ALT"][:4]
        elif key == "INV":
            out["INV"] = [x for x in (_num(s) for s in val.split("|") if s.strip()) if x is not None][:3]
    return out


def validate(line: str, spot: float) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for a LADDER line against live spot. Pure (unit-tested)."""
    p = parse(line)
    errors: list[str] = []
    warns: list[str] = []
    for k in p["unknown"]:
        errors.append(f"unknown key {k}")
    for k in p["bad"]:
        errors.append(f"unparseable value for {k}")
    for k in ("TS", "STATUS", "EXP"):
        if not p.get(k):
            errors.append(f"missing {k}")
    if p.get("STATUS") and p["STATUS"] not in STATUSES:
        errors.append(f"STATUS must be one of {sorted(STATUSES)}")

    for k in ("NR", "SR"):
        if p.get(k) and p[k][0] <= spot:
            errors.append(f"{k} {p[k][0]:,.0f} is not above spot {spot:,.0f}")
    for k in ("NS", "SS"):
        if p.get(k) and p[k][1] >= spot:
            errors.append(f"{k} {p[k][1]:,.0f} is not below spot {spot:,.0f}")
    if p.get("NR") and p.get("SR") and p["SR"][0] < p["NR"][0]:
        warns.append("structural resistance sits below near resistance")
    if p.get("NS") and p.get("SS") and p["SS"][1] > p["NS"][1]:
        warns.append("structural support sits above near support")

    best = p.get("BEST")
    for st in ([best] if best else []) + p["ALT"]:
        tag = f"{st['side']}{st['strike']:,.0f}"
        if st["side"] == "P" and st["strike"] >= spot:
            errors.append(f"{tag}: put strike is not below spot")
        if st["side"] == "C" and st["strike"] <= spot:
            errors.append(f"{tag}: call strike is not above spot")
    if best:
        trig = p.get("TRIG")
        if trig and best["side"] == "P" and best["strike"] >= trig[0]:
            errors.append("BEST put strike is not below the trigger zone")
        if trig and best["side"] == "C" and best["strike"] <= trig[1]:
            errors.append("BEST call strike is not above the trigger zone")
        hurts_put = best["side"] == "P"
        for lvl in p["INV"]:
            if hurts_put and lvl >= spot:
                errors.append(f"INV {lvl:,.0f} is above spot (a put is invalidated by a break DOWN)")
            if not hurts_put and lvl <= spot:
                errors.append(f"INV {lvl:,.0f} is below spot (a call is invalidated by a break UP)")
        if p["INV"] and hurts_put and p["INV"] != sorted(p["INV"], reverse=True):
            warns.append("INV should run soft → hard (descending for a put)")
        if p["INV"] and not hurts_put and p["INV"] != sorted(p["INV"]):
            warns.append("INV should run soft → hard (ascending for a call)")
        if best["rest"] == "":
            warns.append("BEST has no @rest premium")
    else:
        warns.append("no BEST — the ladder will draw zones only")
    return errors, warns


def main() -> None:
    ap = argparse.ArgumentParser(description="Validate an Options Ladder LADDER line (read-only).")
    ap.add_argument("line")
    ap.add_argument("--spot", type=float, required=True, help="live scanner spot")
    args = ap.parse_args()
    errors, warns = validate(args.line, args.spot)
    for w in warns:
        print(f"  warn: {w}")
    for e in errors:
        print(f"  ERROR: {e}")
    print("LADDER OK" if not errors else "LADDER INVALID — fix before pasting")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()

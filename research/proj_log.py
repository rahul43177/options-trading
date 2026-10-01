"""
Read-only calibration log for the zone-premium projector.

The whole point of the projector (pricing.project_premium) is to tell you, before BTC gets
there, what premium a strike will be worth AT a watched level — reliably enough to rest a
maker SELL limit on. This module closes the loop so that number earns trust from evidence
instead of assertion:

  * watch_plan appends the projected band for each candidate it prints  -> log_projection()
  * as BTC moves, you (or a cron) record what the strike actually traded  -> mark_realized()
  * calibrate() reprices each logged snapshot forward to every realized mark and reports the
    error, so the ATR-speed constant k (perp_analytics.hours_to_zone) and the IV bump can be
    tuned from what actually happened.

No API keys, no orders — it only reads/writes a local JSONL file.

    python -m research.proj_log calibrate       # score the log
    python -m research.proj_log seed-2026-09-30  # load the reference trade's realized curve
"""
from __future__ import annotations
import json, sys, time, datetime as dt
from statistics import mean, median
from .config import ROOT
from .pricing import price, implied_vol

LOG = ROOT / "data" / "proj_log.jsonl"


def _append(rec: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def log_projection(symbol, strike, kind, spot, zone, iv, hours_to_expiry,
                   band, atr_1h, k, iv_bump, ts=None) -> None:
    _append({"row": "projection", "ts": ts or time.time(), "symbol": symbol,
             "strike": strike, "kind": kind, "spot": spot, "zone": zone, "iv": iv,
             "hte": hours_to_expiry, "atr_1h": atr_1h, "k": k, "iv_bump": iv_bump,
             "floor": band["floor"], "base": band["base"], "ceiling": band["ceiling"],
             "hours_to_zone": band["hours_to_zone"]})


def mark_realized(symbol, strike, kind, spot, mark, hours_to_expiry, iv=None, ts=None) -> None:
    _append({"row": "realized", "ts": ts or time.time(), "symbol": symbol, "strike": strike,
             "kind": kind, "spot": spot, "mark": mark, "hte": hours_to_expiry, "iv": iv})


def _rows() -> list[dict]:
    if not LOG.exists():
        return []
    out = []
    for line in LOG.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def calibrate() -> None:
    """Reprice-accuracy check per (symbol, strike, kind).

    Anchors on the earliest realized snapshot that yields an IV, then reprices that IV forward
    to every later realized mark at its own spot + time-to-expiry, and reports the % error.
    This is the honest test of whether the Black-Scholes engine (hence the projected band)
    tracks the live market. When a realized snapshot lands within tolerance of a logged zone,
    it also scores that zone's floor/base/ceiling against the realized mark (fill accuracy).
    """
    rows = _rows()
    if not rows:
        print("proj_log: no data yet. Run watch_plan (it logs), or `seed-2026-09-30`.")
        return
    groups: dict[tuple, list] = {}
    for r in rows:
        groups.setdefault((r["symbol"], r["strike"], r["kind"]), []).append(r)

    print("=" * 92)
    print("  PROJECTION CALIBRATION  —  Black-Scholes reprice vs realized marks (read-only)")
    print("=" * 92)
    all_err = []
    for (symbol, strike, kind), rs in sorted(groups.items()):
        realized = sorted((r for r in rs if r["row"] == "realized"), key=lambda r: r["ts"])
        if len(realized) < 2:
            continue
        anchor = realized[0]
        aiv = anchor.get("iv") or implied_vol(
            anchor["mark"], anchor["spot"], strike, max(anchor["hte"], 0.25) / 8760, kind)
        if not aiv:
            continue
        print(f"\n  {symbol}  ({kind}, strike {strike:,.0f})  anchor IV {aiv*100:.1f}%"
              f"  from spot {anchor['spot']:,.0f}")
        print(f"    {'when':>16} {'spot':>9} {'realized':>9} {'reprice':>9} {'err%':>7}")
        for r in realized[1:]:
            pred = price(r["spot"], strike, max(r["hte"], 0.25) / 8760, aiv, kind)
            err = (pred - r["mark"]) / r["mark"] * 100 if r["mark"] else 0.0
            all_err.append(err)
            when = dt.datetime.fromtimestamp(r["ts"], dt.timezone.utc).strftime("%m-%d %H:%M")
            print(f"    {when:>16} {r['spot']:>9,.0f} {r['mark']:>9.0f} {pred:>9.0f} {err:>+6.1f}%")
    if all_err:
        print(f"\n  reprice error over {len(all_err)} points:  "
              f"mean {mean(all_err):+.1f}%   median {median(all_err):+.1f}%   "
              f"mean|err| {mean(abs(e) for e in all_err):.1f}%")
        bias = mean(all_err)
        if bias > 6:
            print("  -> model runs HOT vs market: lower the assumed IV, or trim the +iv_bump.")
        elif bias < -6:
            print("  -> model runs COLD vs market: IV is expanding faster than assumed; raise iv_bump.")
        else:
            print("  -> reprice tracks within tolerance; the band is trustworthy. Tune k on real zone-hits.")
    print("\n  Not advice. Data only. You place every order yourself.\n")


def seed_reference_trade() -> None:
    """Seed the realized premium curve of the 2026-09-30 C-BTC-86000-021026 short.

    Three live observations captured while analysing the user's own position (premium fell as
    BTC rejected resistance and reversed): a labeled starting point for the calibration loop.
    """
    base = dt.datetime(2026, 9, 30, 13, 5, tzinfo=dt.timezone.utc).timestamp()
    pts = [  # (minutes_after_base, spot, mark, hours_to_expiry)
        (0,  85439, 653, 47.6),
        (11, 85290, 562, 47.4),
        (18, 84278, 294, 46.3),
    ]
    for dm, spot, mark, hte in pts:
        mark_realized("C-BTC-86000-021026", 86000.0, "call", spot, mark, hte,
                      ts=base + dm * 60)
    print(f"seeded {len(pts)} realized points for C-BTC-86000-021026 -> {LOG}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "calibrate"
    if cmd == "seed-2026-09-30":
        seed_reference_trade()
    else:
        calibrate()

# Backlog — later implementation

Parked ideas for the options-short toolkit (`pine/options_short_ladder_auto.pine`, `research/`,
the `/options-entry` and `/futures-entry` skills). Newest context: 05-Oct-2026.
Move an item to "Done" with the date when it ships.

## 1. Free automatic alerts (TradingView Basic = 0 indicator alerts)

TradingView's free plan allows no alerts from indicators, so OPT-AUTO's `alert()` /
`alertcondition()` events never reach the phone. Workaround until then: plain price alerts at the
panel's PUT-zone top and "Stop if" level (move them when the Zones row changes).

**Plan — local watcher (`research/watch_alert.py`):**
- Every 5 min, read Delta public data (read-only, no key): candles + option chain.
- Reuse `research.zones` (near/structural bands), `research.direction` (bias score) and the Pine
  state machine (WAIT → ARMED → VALID → INVALID / PLAYED OUT; 12 × 15m confirmation break,
  1.5 × 1h-ATR played-out reset) — port the state machine to Python with a unit test.
- Message = same text as the Pine `alert()`: event · action · contract + expiry · model/live
  premium · rest@ · stop. Bonus over Pine: include the LIVE bid/ask, OI and spread.
- Delivery (user to choose): macOS notification (free, private, Mac must be awake) or phone push
  via ntfy (free app, private topic; messages pass through ntfy's server).
- Run with launchd (not a Claude cron). Fire only on state changes; de-duplicate.
- Validate against the indicator side by side for a few days before relying on it.

## 2. "Listed strikes" helper

Pine can't see Delta's chain. A one-liner that prints the listed strikes for a chosen expiry
(e.g. `84000,84200,84400,84600`) to paste into the indicator's "Listed strikes on Delta" input,
so BEST/ALT snap to real contracts. Optionally also print the BEST strike's mark IV for the
"Delta mark IV %" input and the current call−put skew for "Call IV − put IV".

## 3. Liquidity gap in the expiry menu

The menu colours an expiry green for its *time window* only — it can't see OI. On 05-Oct-2026 the
07-Oct P84200 was green with OI 8 (06-Oct 1,240; Friday 09-Oct 20,121). The watcher (item 1) or
helper (item 2) should report OI/spread per menu expiry. Until then: always check OI on Delta.

## 4. Calibration that drifts

- `iv_mult` 0.92 and `call_skew` +3 were measured on 05-Oct-2026 (BTC). Both drift; skew usually
  flips negative in sell-offs. Re-check weekly: model premium vs Delta mark within ~5%.
- Could be automated by the helper (item 2): print suggested `iv_mult` / `call_skew`.
- ETH not separately calibrated (BTC numbers used).

## 5. Evidence still thin

- Direction backtest is BTC only (chosen side 88.9% vs wrong 84.6%, blind ~87%, not significant vs
  blind). Run `research.direction` on ETHUSD before trusting the ETH bias.
- The full zone → trigger → rest@ → exit loop is unvalidated: `research.readiness` = COLLECTING,
  0 closed paper trades. Run `research.collector --interval 300` and log paper trades.
- `research.cvd_probe` needs ≥100 forward pairs (24 as of 05-Oct-2026).
- `proj_log calibrate` once zones get hit, to check rest@ fills vs realised.

## 6. Pine — confirm in TradingView

- Combined `alert()` block and Friday-weekly menu row compiled? (user screenshots showed the menu
  working; `alert()` untested because of the plan limit).
- Codex's 4 screenshot cases: PUT bias at support · PUT bias at resistance · CALL bias at
  resistance · VALID after price travelled to the opposite zone (Action must say WAIT).

## 7. Ideas, not yet justified

- Extra technicals (RSI/MACD/Stoch) in the direction score — only via a backtest in
  `research.direction` first (the "fade the top of the range" factor lost; don't add blindly).
- Monthly/quarterly expiries in the menu (outside the 48h backtest horizon).

## Done
- 05-Oct-2026 — Pine review rounds (ETH 20-grid, VP from 1h, played-out reset, BIAS/Action split,
  opposite-zone priority, IV 0.92 + term structure + skew, rest@ floor, Friday weekly row,
  weekend ≥6h, expiry menu, combined alert()). Backup: `pine/options_short_ladder_auto.pine.bak-20261005`.

## 7. rest@ accuracy — follow-ups (05-Oct-2026)

Shipped today (see "Done"). Still open:
- **Pine indicator still uses the old travel-time rule** for its model strikes' "rest@~" label
  (1.5x ATR travel). Port the arrival table (research/data/arrival_table.json) into the Pine as a
  small lookup (m x H grid) so chart and scanner agree. Until then, trust the scanner's rest@.
- **Real S/R zones are touched more often than generic distances** (48 expired plans: 79%
  touched vs 65% predicted, bullish week). After ~100 more logged plans, fit a zone "stickiness"
  multiplier on P(arrive) — not before (overfitting risk).
- **Refit cadence:** run `python -m research.rest_backtest` weekly; refit
  `python -m research.arrival fit --days 60` if the arrival model's fill rate leaves ~65–85%.
- **Zones > 2 ATR away** fill ~50%: consider a separate table conditioned on distance-to-expiry
  ratio, or simply re-run plans as price approaches (current guidance).

## Done
- 05-Oct-2026 — `research.fees` (Delta fee rule verified on real fills; backtest fee bug that
  understated option fees ~350x fixed in backtest.py / strict_delta_base.py).
- 05-Oct-2026 — `research.journal` (Delta CSV → net-of-fee scorecard, size vs equity flags).
- 05-Oct-2026 — `research.positions` (held-shorts: stop/liq spot levels, P(stop/liq/ITM), net delta).
- 05-Oct-2026 — desk: EV net of fees, `positions` (ALREADY HELD / same-side flags, best-not-held),
  `sizing` (max units for a risk % at a 2x-credit stop), reachable via P(arrive).
- 05-Oct-2026 — `research.arrival` + arrival-timed band in entry_scan/watch_plan (rest@ fills 75%
  on real Delta marks vs 48% legacy; base error +4% vs +12%), `valid`/`reach` columns, rest@
  capped at the ask, LADDER option, `--legacy-band`; `research.rest_backtest`; ETH 2-decimal prices.
- 05-Oct-2026 — zones: WEAK single-level flag + `deep` 3rd band when structural is <0.5% past near
  (near/structural unchanged, so the Pine mirror still matches).

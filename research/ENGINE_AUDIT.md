# Strategy and engine audit

## Finding

The original project had useful components, but its candidate choice was chiefly “closest to 0.15–0.16 delta after basic filters.” That can select a contract; it cannot establish that selling its volatility has positive expectancy or that it can be entered and exited at executable prices.

The `MORE DATA REQUIRED` verdict was therefore correct, but too vague. `research.readiness` now measures the missing evidence, while `research.engine` distinguishes a best **conditional** candidate from a validated trade.

## Gaps closed in code

- BTC and ETH are collected into the same timestamped snapshot.
- Stored contracts now include parsed expiry and indexed lookup paths.
- Zero quotes are no longer silently treated as missing mids; they fail an explicit executable-quote gate.
- Live chain objects retain quote timestamp, displayed sizes, contract value, turnover, tick size, and product status.
- Selection uses hard gates before a transparent multi-factor score.
- Exactly one row receives `◄ BEST`; `×` means a hard-gate failure.
- Prospective replay sells at a future best bid and buys back at a later best ask, preventing optimistic mark-to-mark evaluation.
- The evidence audit quantifies coverage instead of emitting an unexplained warning.

## Remaining strategy gaps

1. **Forward volatility forecast.** Mark IV must be compared with a point-in-time forecast of subsequent realised variance. `IV > recent RV` alone is not sufficient because the variance risk premium changes by regime and includes jump/tail compensation.
2. **Execution.** Top-of-book quotes do not reveal queue position, partial fills, cancellation latency, or market impact. Record L2 depth and paper-fill rules; do not assume every displayed bid fills.
3. **All-in economics.** Add exact Delta fees, India taxes/GST, settlement charges, and currency conversion to every replay. Gross results are not net results.
4. **Portfolio margin.** Delta portfolio margin depends on the whole portfolio and short-option margin floors. A candidate score cannot infer liquidation or safe size from a public ticker.
5. **Regime and events.** Segment results by trend, IV/RV regime, expiry bucket, time of day, and scheduled-event proximity. Avoid tuning a single threshold on pooled data.
6. **Selection bias.** The prior 75-configuration search needs walk-forward/out-of-sample evaluation and a multiple-testing correction such as Deflated Sharpe Ratio or Probability of Backtest Overfitting.
7. **Defined risk.** Compare naked shorts with vertical spreads using actual simultaneous multi-leg quotes and portfolio margin. Do not extrapolate a single-leg result to a spread.
8. **Calibration.** Score weights are policy priors, not learned truth. Freeze them before the forward sample; revise only on a declared training window, then retest on untouched data.

## Validation sequence

1. Run the five-minute BTC+ETH collector continuously; monitor cadence, missing assets, quote errors, and API failures.
2. Save every TradingView context before looking at the later outcome. Include zone, Weekly/Daily direction, 4h/1h state, 15m trigger, ATR, event risk, and a point-in-time RV forecast.
3. Record all candidates and rejections, including no-fill observations. Never keep only winners.
4. After 30 days, run the first diagnostic review. Continue to 90 days and at least 100 non-overlapping paper outcomes before a validation claim.
5. Evaluate calibration, expectancy after all costs, drawdown, tail loss, stability by regime, and sensitivity to nearby thresholds.
6. Keep a final untouched holdout. If it fails, the strategy remains research-only.

## Primary research and platform references

- Delta API documentation: https://docs.delta.exchange/
- Delta portfolio-margin guide: https://guides.delta.exchange/delta-exchange-india-user-guide/trading-guide/margin-explainer/portfolio-margin
- Delta options guide: https://guides.delta.exchange/delta-exchange-india-user-guide/derivatives-guide/options-guide
- Federal Reserve, implied and realised volatility risk premia: https://www.federalreserve.gov/econres/feds/dynamic-estimation-of-volatility-risk-premia-and-investor-risk-aversion-from-option-implied-and-realized-volatilities.htm
- NBER, variance risk premium: https://www.nber.org/papers/w18995
- Bailey and López de Prado, Deflated Sharpe Ratio: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551
- Bailey et al., Probability of Backtest Overfitting: https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf

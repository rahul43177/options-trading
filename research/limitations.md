# Limitations / kill criteria

## Blocking evidence gap

The public API did not yield timestamped historical option-chain snapshots. Therefore this project cannot yet answer whether Delta implied volatility exceeded subsequent realised volatility after live bid/ask costs. That is the primary research question.

## Explicitly not modelled

- Actual Delta isolated/cross/portfolio margin, liquidation price, and auto-top-up.
- Historical bid/ask, partial/multi-leg fills, stale quotes, exchange outages, settlement/pin mechanics, or actual fee/GST charged on a specific account.
- Defined-risk verticals, strangles, and iron condors; ranking them without actual joint fills/margin would be false precision.
- Out-of-sample/walk-forward option replay and live-paper outcomes (only one live observation cycle has occurred).

## Kill criteria for paper strategy selection

Do not enable a candidate if 30+ prospective 2–7 DTE observations show negative executable expectancy after bid/ask and fees, if any margin/risk guard is breached, if live fill assumptions are materially worse than estimates, or if a frozen candidate's live behaviour falls outside its predeclared confidence bounds. A more defensible threshold for a small edge will likely require hundreds of independent observations.

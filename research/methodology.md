# Methodology

1. Download raw Delta BTCUSD candles in non-overlapping API windows; retain every response and its SHA-256 metadata.
2. Validate timestamps and OHLC before any analysis. The raw source is not altered.
3. Run only a **Level 1** path-risk experiment: at each single daily entry point, calculate trailing (past-only) realised volatility, synthetic Black-Scholes premium and model-delta strike. Future BTC candles are used only after the entry timestamp.
4. Apply executable-direction assumptions (sell below model value, buy above it), a fee per side, take-profit choices, and account-loss guard.
5. Bootstrap/reorder the resulting model trade P&Ls for a sequence-risk illustration. This is not evidence about Delta's premium risk premium.
6. Collect the real option chain forward in time. Once the holding horizon has elapsed, a future version can perform a Level 3 replay using only snapshots available at each timestamp.

The current model includes naked directional short calls/puts only as a hypothesis stress test. Defined-risk spreads, strangles, and iron condors are intentionally not ranked before their real multi-leg fill and margin data are available.

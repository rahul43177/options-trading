# Executive Verdict

**MORE DATA REQUIRED.** The live Delta India public API was queried successfully and a replayable BTC dataset plus live option-chain collector were created. However, public historical option-chain snapshots were not available. The central question—whether Delta option premiums compensate for realised BTC movement after executable costs—cannot be proven from this run. No live orders were placed.

# Data Quality

**FACT:** Raw `BTCUSD` 5-minute candles came from `GET /v2/history/candles`: 210,270 raw / 210,241 unique, 2024-08-11 12:20 UTC through 2026-08-11 12:20 UTC. There were 29 duplicate boundary records, no cadence gaps, 393 zero-volume candles, and zero impossible OHLC bars. See `data_quality_report.md` and the sidecar request/checksum metadata.

**FACT:** Live `GET /v2/tickers?contract_types=call_options,put_options&underlying_asset_symbols=BTC` returned 385 current BTC option observations in the first collector cycle, including bid/ask, IV, Greeks, OI, volume and a spot value. The live raw payload is retained under `data/live_option_chain/`.

**UNKNOWN:** The API has no documented timestamp/start/end parameter for historical option-chain observations. No historical bid/ask/IV/Greek replay was obtained.

# Strategy Results

**MODEL:** 75 naked directional short-option configurations were tested only against real BTC paths, using synthetic Black-Scholes option values, trailing realised volatility as a proxy, one tick side cost, and an observed 0.01% option taker fee field. This is Level 1 evidence, not a Delta option P&L backtest.

The best in-sample model row was 7 DTE / 30Δ / 70% TP: 723 trades, 85.06% win rate, $0.0651 average P&L per 0.001 BTC contract, PF 1.117, $90.48 model drawdown, $18.20 worst model loss. The worst row, 5 DTE / 15Δ / 30% TP, had 95.71% wins but negative expectancy ($-0.0607) and PF 0.651. **INFERENCE:** high win rate does not establish an edge.

# Best Configuration

There is no validated configuration. The only *observation hypothesis* to collect prospectively is the in-sample model row: 7 DTE, approx. 30Δ, naked directional short, 70% TP, one daily scan, and a 5%-account model loss guard. It must not be paper-entered automatically or traded until real chain evidence, margin, and defined-risk comparison exist.

# Risk Analysis

**UNKNOWN:** Actual Delta liquidation distance, maintenance margin, top-up, and reserve consumption are account-/portfolio-specific and cannot be inferred from public candles. The model reserve estimate was zero because one 0.001 BTC contract is much smaller than the ₹15k model allocation; it must not be scaled linearly to 100×. Auto top-up is untested and may merely allow losses to compound.

# Regime Analysis

Not yet valid. Regime conclusions require real option observations: modelled price paths cannot establish whether a regime paid adequate premium.

# Robustness

The best result is parameter-sensitive and selected in-sample. Multiple high-win-rate configurations lost money in the same model. This fails the standard needed to call the strategy robust.

# Out-of-Sample

No option-chain OOS test is available. Any parameter selected from these 75 model alternatives is subject to selection bias.

# Monte Carlo

For the frozen best in-sample model configuration only, 10,000 bootstrap paths gave mean ending $400.48 from a $352.94 model account, 5th percentile $307.75, 1st percentile $265.34, P(≥25% drawdown) 5.78%, P(≥50%) 0.03%, P(ruin) 0%. **MODEL LIMITATION:** these figures omit real IV, fill, liquidity, margin and jump risks and are not investable probabilities.

# ₹30,000 Account

**UNKNOWN:** ₹15k trading margin, ₹15k reserve and 100× leverage cannot be converted to option contracts without authenticated Delta margin/risk information. The synthetic test used one 0.001 BTC contract only. There is no safe position-size conclusion.

# Income Feasibility

₹500/day to ₹5,000/day targets are not estimable responsibly. The model P&Ls are per tiny contract and do not validate premium or scalable margin. No income target is supported.

# Recommended Next Experiment

Run `python -m research.collect_live` continuously. When sufficient overlapping 2–7 DTE snapshot paths have accumulated, replay actual best bid on short entry and ask on close, preserve staleness/quote-width filters, compare IV with realised volatility, get authenticated margin documentation/observations, and paper trade a frozen defined-risk candidate only.

# Final Decision

**MORE DATA REQUIRED.**

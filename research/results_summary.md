# Results summary — Level 1 only

Run: `backtests/runs/2026-08-11_run_122636/summary.json`.

- 210,241 unique real Delta BTCUSD 5-minute candles; 2024-08-11 12:20 UTC to 2026-08-11 12:20 UTC.
- 75 model configurations: 1/2/3/5/7 DTE × 10/15/20/25/30 delta × 30/50/70% take-profit.
- 723 entry dates/configuration (one daily UTC entry); 54,225 independent model trade paths. The aggregate is **not** a portfolio and is not used for a performance claim.
- Best in-sample *model* configuration: naked directional short, 7 DTE / 30Δ / 70% TP: 85.06% win rate, $0.0651 mean P&L per 0.001-BTC contract, PF 1.117, $90.48 maximum model drawdown, $18.20 worst model trade, one account-loss guard exit.
- Worst: 5 DTE / 15Δ / 30% TP: 95.71% win rate but **-$0.0607** expected P&L and PF 0.651. This is direct evidence that high win rate is not enough.
- For the best *in-sample model configuration only*, 10,000 bootstrap paths produced 5.78% probability of a ≥25% model account drawdown and 0.03% probability of ≥50% drawdown. These values omit actual Delta margin, IV shocks, and fills, so they must not be used as real risk estimates.

There is no out-of-sample selection/test split yet: doing one after selecting from this same set would still not prove an option premium edge. No historical Delta option observations exist in this run.

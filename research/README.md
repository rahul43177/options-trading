# Delta India BTC + ETH options research engine

This is deliberately a **research and paper-only** system. `LIVE_TRADING_ENABLED = False`; no module contains an order-placement call or credentials.

## What “MORE DATA REQUIRED” means

It is an evidence verdict, not “no candidate exists.” The old database contains only two BTC chain snapshots about one second apart, no ETH snapshots, no closed paper trades, and three realised projection observations. That is enough to test plumbing, not enough to estimate an executable edge through different volatility regimes.

Run the audit at any time:

```bash
python -m research.readiness
```

The minimum gates are explicit: BTC and ETH coverage, at least 80% of scheduled five-minute snapshots over 30 days as a baseline (90-day target), at least 100 non-overlapping closed paper outcomes, at least 50 realised zone observations, and no more than 1% invalid executable quotes. Passing them means **validation-ready**, not approved for live trading. A few snapshots separated by a month do not count as a month of coverage.

## One-engine workflow

1. TradingView MCP supplies structured Weekly → 15m trend, zone, state, and reversal-confirmation context. It is the chart-analysis input, not an execution system.
2. Python applies hard evidence gates and an explainable score.
3. Delta public REST supplies live quote, IV/Greeks, OI, spread, displayed size, status, and contract value. Future snapshots measure executable bid-to-ask outcomes.
4. A separate read-only account adapter may provide a *sanitised* account-state object. API secrets never enter this repository or the analysis prompt.
5. The engine emits one global best conditional candidate marked `◄ BEST`, along with every unresolved evidence gap. It never places an order.

```bash
# Build the forward evidence base (BTC and ETH, every five minutes by default)
python -m research.collector --once
python -m research.collector --interval 300

# Rank a current TradingView setup against live Delta quotes
cp research/context.example.json /tmp/context.json   # replace values and timestamp
python -m research.engine --context /tmp/context.json

# Persist a paper decision, then replay it only after future snapshots exist
python -m research.engine --context /tmp/context.json --record
python -m research.prospective_replay

# Historical research retained from the original project
python -m research.download_historical --days 730
python -m research.data_quality
python -m research.run_backtest
python -m research.real_delta_replay
```

The score is deliberately not a black-box “institutional” label. Hard gates reject stale/non-executable quotes, weak liquidity, unsuitable delta/expiry, and strikes inside the zone buffer before scoring. The score then exposes trend, trigger state, liquidity/depth, delta fit, expiry fit, IV-versus-forward-RV evidence, and event/regime penalties separately.

Evidence labels remain strict: live chains are **LIVE OBSERVATION**; BTC-path tests are **REAL DELTA HISTORICAL BTC + SYNTHETIC/MODELED OPTIONS**; Delta's expired option history is mark-only and cannot prove historical executable fills.

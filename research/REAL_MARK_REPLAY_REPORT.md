# Real Delta India historical mark-price replay

Run folder: `backtests/runs/2026-08-11_real_mark_replay_124349/`.

## What was simulated

**FACT:** Historical BTCUSD 5-minute candles and historical `MARK:<expired BTC option symbol>` 5-minute candles came from Delta India’s public `GET /v2/history/candles` endpoint. Expired contract metadata came from `GET /v2/products?states=expired&contract_types=call_options,put_options&expiry=YYYY-MM-DD`.

**Fixed strategy:** At 12:00 UTC, if BTC had moved at least $500 in the previous hour, sell one BTC option (call after an up move, put after a down move), choose an OTM strike nearest $1,000 from spot and a 36–60 hour expiry, then cover at the earlier of two days or settlement. This is exactly one fixed 0.001 BTC contract—there is no leverage/margin scaling.

**MODEL:** Starting balance ₹20,000; USD/INR conversion ₹85; 0.01% fee each side, from the observed option taker-commission field. Balance is updated after every completed trade. No trade is created when a historical mark candle is unavailable.

## Result

| Metric | Value |
|---|---:|
| Qualifying/recorded signals | 64 |
| Completed mark-to-mark trades | 17 |
| Explicitly skipped (missing historical mark) | 47 |
| Wins / losses | 14 / 3 |
| Win rate | 82.35% |
| Starting balance | ₹20,000.00 |
| Final balance | ₹20,059.40 |
| Net result | **₹59.40** |

## Interpretation

**INFERENCE:** A single fixed 0.001 BTC contract produced a tiny positive mark-to-mark result over the available completed sample. This is not evidence that the strategy is profitable or scalable: the sample is only 17 completed trades, excludes historical bid/ask spread and liquidation/margin, and skips 47 signals because Delta did not return the necessary historical mark series.

## Audit files

- `trade_log.csv`: one row per completed or skipped signal, including entry/exit timestamp, symbol, marks, fee, P&L, balance before and after.
- `trade_log.json`: immutable structured equivalent.
- `contracts_by_expiry.json`: precise contract universe queried at each expiry date.
- `summary.json`: headline metrics and limitations.

The first row begins 2024-08-12 and the last evaluated row is 2026-07-06. Data gaps have not been imputed.

# Verified public API specification (checked 2026-08-11 UTC)

Base URL: `https://api.india.delta.exchange/v2`.

| Need | Verified endpoint | Result used |
|---|---|---|
| Instruments/contracts | `GET /products` | Contract type, strike, expiry/settlement time, contract value, tick size, initial/maintenance fields, fee fields |
| Live option chain | `GET /tickers?contract_types=call_options,put_options&underlying_asset_symbols=BTC` | bid/ask and sizes, mark, IV, Greeks, OI, volume, spot |
| BTC history | `GET /history/candles?symbol=BTCUSD&resolution=5m&start=...&end=...` | OHLCV, Unix-second timestamps |
| Recent product trades | `GET /trades/{symbol}` | recent only, not used as historical chain data |
| Expired contracts | `GET /products?states=expired` | contract metadata/settlement fields, cursor-paginated |

## Critical historical-data finding

**FACT:** The documented/public option-chain request accepts contract type, underlying and `expiry_date`; it does **not** accept an observation timestamp, start, or end parameter. Live ticker payloads contain current data only. We therefore found no public endpoint that returns historical option-chain snapshots (bid/ask/IV/Greeks/OI) by historical timestamp.

**INFERENCE:** Historical BTC candle data and expired product metadata cannot reconstruct what the live option market showed at a past time. Every option-price result from the initial backtest must be labelled **SYNTHETIC/MODELED**, not a Delta option backtest.

## Operational facts used

- The historical-candle endpoint returned a maximum of about 4,000 5-minute records per request in live testing; downloader requests non-overlapping time blocks and preserves request metadata/checksum.
- The observed live BTC option product payload reported `contract_value: 0.001`, `tick_size: 0.1`, and `taker_commission_rate: 0.0001` for sampled instruments. The code reads raw values rather than assuming they are universal.
- The live collector polls the documented REST ticker endpoint every five minutes by default. It is intentionally sufficient for an auditable baseline; WebSocket integration is a future enhancement, not silently claimed.

## Scope boundary

Public endpoints are used only. Actual account-specific margin, liquidation distance, auto-top-up state, and portfolio-margin outcomes require authenticated account/risk endpoints and are **UNKNOWN** in this research run. No credentials are read and no orders are submitted.

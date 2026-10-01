# Data quality report

- Source: `/Users/rahulmishra/Desktop/Personal/options-trading/research/data/raw/delta_btc/BTCUSD_5m_730d_20260811T122212Z.json`
- Total raw candles: 210270
- Unique candles: 210241
- Date range UTC: 2024-08-11T12:20:00+00:00 to 2026-08-11T12:20:00+00:00
- Duplicate timestamps: 29
- Missing/non-5-minute intervals: 0
- Zero-volume candles: 393
- Impossible OHLC relationships: 0

## Cleaning decisions

No observations were deleted. Exact timestamp duplicates are deterministically deduplicated only in the analysis view (last raw occurrence wins); raw JSON remains unchanged. Timestamps are Unix seconds and are converted only to UTC.

## First 100 gaps

```json
[]
```

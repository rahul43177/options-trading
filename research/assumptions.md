# Assumptions and non-claims

## FACT

BTCUSD candles, one current BTC option-chain snapshot per collector invocation, and option product metadata are real public Delta data.

## MODEL

- Black-Scholes option value, strikes selected from model delta, and a constant realised-volatility estimate are synthetic.
- USD/INR is set to 85 solely to express the ₹30,000 account thought experiment; it is not a fetched FX rate.
- One tick each side and 0.01% fee each side are a conservative/transparent initial execution model. They do not replace actual historical bid/ask fills.
- Reserve usage in `backtest.py` is a simple account-loss proxy, **not** Delta's margin engine.

## UNKNOWN

Historical Delta option bid/ask, IV, individual account margin, liquidation price, auto-top-up consumption, and portfolio offsets are not established by public historical data. They cannot be used to validate profitability until collected prospectively or obtained from a reliable historical source.

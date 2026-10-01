# Risk specification

The risk engine is logically independent: an account-loss guard exits a model trade at 5% of the ₹30,000 account (converted using the stated model FX). Take profit closes at 30%, 50%, or 70% model premium capture; time exit occurs at the selected 1/2/3/5/7-day horizon.

This is a research guard only. It does not claim to know Delta liquidation price or account margin. Production paper trading remains `NO_TRADE_MORE_DATA_REQUIRED` until actual option-history evidence supports a configuration. A stale-data/API-failure condition must block new entries; the collector logs errors and never emits an order.

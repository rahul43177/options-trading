"""Central, explicit research settings.  Live trading is deliberately impossible here."""
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
API_BASE = "https://api.india.delta.exchange/v2"
LIVE_TRADING_ENABLED = False

# Underlyings the read-only scanner supports: option-symbol prefix -> spot candle symbol
# (the spot symbol feeds ATR/hours-to-zone). BTC is the default everywhere; ETH is additive.
UNDERLYINGS = {"BTC": "BTCUSD", "ETH": "ETHUSD"}

# Default OTM buffer ($ past the zone a strike must clear) per asset — scaled to price so
# ETH's tightly-spaced strikes aren't filtered out by a BTC-sized $1,000. Overridable via --buffer.
DEFAULT_BUFFER = {"BTC": 1000.0, "ETH": 40.0}

@dataclass(frozen=True)
class BacktestConfig:
    initial_inr: float = 30_000.0
    usd_inr: float = 85.0  # MODEL: configurable conversion, not a Delta price feed
    candle_minutes: int = 5
    lookback_days: int = 7
    entry_hour_utc: int = 12
    dtes: tuple[int, ...] = (1, 2, 3, 5, 7)
    deltas: tuple[float, ...] = (0.10, 0.15, 0.20, 0.25, 0.30)
    take_profits: tuple[float, ...] = (0.30, 0.50, 0.70)
    fee_rate: float = 0.0001  # observed live BTC option product taker_commission_rate
    slippage_ticks: int = 1
    tick_size: float = 0.1
    contracts: int = 1
    seed: int = 20260811

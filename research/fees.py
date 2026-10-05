"""Delta Exchange India fee model — the real rule, verified against live fills.

Options (per side, per fill):
    fee = min(taker_commission_rate x underlying notional,  premium_commission_rate x premium)
with taker_commission_rate = 0.0001 (0.01%) and premium_commission_rate = 0.035 (3.5%), both read
from the product endpoint (`taker_commission_rate`, `product_specs.premium_commission_rate`).
For the far-OTM strikes this toolkit sells, the 3.5%-of-premium CAP almost always binds, so the
effective fee is ~3.5% of premium per side. GST (18%) is charged on top of every fee.

Verified on the user's 05-Oct-2026 fills (Delta asset history, fee incl. GST):
    P-ETH-2620-071026 buy 1.5 ETH @ 5.00, ETH ~2,728  -> 0.30975   (cap binds: 0.035*7.5*1.18)
    P-ETH-2600-071026 buy 2.0 ETH @ 3.70              -> 0.30562   (cap binds: 0.035*7.4*1.18)
    P-ETH-2620-051026 sell 4.43 ETH @ 9.99, ETH ~2,668 -> 1.3950 (notional binds: 0.0001*2668*4.43*1.18)
Fills showing a LOWER fee than this model were part-paid by Delta trading-fee credits (TFC).
Perps (verified on ETHUSD/BTCUSD/BNBUSD/TAOUSD fills): 0.05% taker, 0.02% maker, + GST.

The older research code charged `0.0001 x premium` (the notional rate applied to premium), which
understated option fees roughly 350x. Use this module instead. Pure functions, unit-tested.
"""
from __future__ import annotations

NOTIONAL_RATE = 0.0001       # taker/maker commission on underlying notional
PREMIUM_CAP = 0.035          # cap: fee never exceeds 3.5% of the premium traded
GST = 0.18                   # Indian GST on exchange fees
PERP_TAKER_RATE = 0.0005     # perps: 0.05% of notional on market (taker) fills
PERP_MAKER_RATE = 0.0002     # perps: 0.02% of notional on resting limit (maker) fills


def option_fee(premium: float, qty_underlying: float, spot: float | None = None,
               notional_rate: float = NOTIONAL_RATE, premium_cap: float = PREMIUM_CAP,
               gst: float = GST) -> float:
    """Fee in quote currency (USD) for ONE fill of `qty_underlying` (e.g. 1.5 ETH, 0.15 BTC).

    `premium` is the option price per 1 unit of underlying (what the chain quotes). If `spot` is
    unknown the premium cap is used alone — an upper bound for OTM strikes, so never optimistic.
    """
    if premium <= 0 or qty_underlying <= 0:
        return 0.0
    cap_fee = premium_cap * premium * qty_underlying
    if spot and spot > 0:
        base = min(notional_rate * spot * qty_underlying, cap_fee)
    else:
        base = cap_fee
    return base * (1.0 + gst)


def option_fee_rate(premium: float, spot: float | None = None) -> float:
    """Fee as a FRACTION of premium for one side (incl. GST). ~0.0413 when the cap binds."""
    if premium <= 0:
        return 0.0
    return option_fee(premium, 1.0, spot) / premium


def round_trip_fee(entry: float, exit_price: float, qty_underlying: float,
                   spot: float | None = None) -> float:
    """Open + close fees. An option left to expire worthless pays no closing trade fee."""
    return option_fee(entry, qty_underlying, spot) + option_fee(exit_price, qty_underlying, spot)


def net_short_pnl(entry: float, exit_price: float, qty_underlying: float,
                  spot: float | None = None) -> float:
    """Net USD P&L of a short option: credit - debit - both fees."""
    gross = (entry - exit_price) * qty_underlying
    return gross - round_trip_fee(entry, exit_price, qty_underlying, spot)


def perp_fee(price: float, qty_underlying: float, rate: float = PERP_TAKER_RATE,
             gst: float = GST) -> float:
    """Perp fee for one fill: rate x notional, plus GST."""
    if price <= 0 or qty_underlying <= 0:
        return 0.0
    return rate * price * qty_underlying * (1.0 + gst)

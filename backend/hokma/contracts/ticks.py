"""The minimum price step of a contract at a price level (SPEC-020: N-03).

A future or a share has one tick for its whole price range, the one of its sheet. An option of the equity market has two: 0.01 below
3 USD and 0.05 from 3 USD up. The simulated broker (`hokma.broker.sim_engine`) and the backtest (`hokma.backtest.engine`) read the grid
from this one function, so an order is rounded the same way in both. Rounding itself is `hokma.exec.money.round_to_tick`.
"""
from __future__ import annotations

from decimal import Decimal

OPTION_BREAK = Decimal("3")
OPTION_TICK_BELOW = Decimal("0.01")
OPTION_TICK_FROM = Decimal("0.05")
VARIABLE_TICK_ASSETS = ("option",)


def tick_scheme(asset_type: str | None, price: Decimal, tick_size: Decimal | None = None) -> Decimal | None:
    """The tick at `price`: the two level scheme for an option, the `tick_size` of the sheet for anything else (None when it has none)."""
    if asset_type in VARIABLE_TICK_ASSETS:
        if not isinstance(price, Decimal) or not price.is_finite() or price < 0:
            raise ValueError("a price is a Decimal of zero or more")
        return OPTION_TICK_BELOW if price < OPTION_BREAK else OPTION_TICK_FROM
    return tick_size

"""What a broker reports. Balance is built by every broker; Order, Fill and Position by the brokers that take orders
(Schwab). None of them carries an account number or a hash: an outside broker's account shows only as `account_ref`
("A1") and `mask` ("••••1234")."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True, slots=True)
class Capabilities:
    accounts: bool = True
    reset: bool = True
    orders: bool = False
    positions: bool = False


@dataclass(frozen=True, slots=True)
class Balance:
    """`cash` and `equity` are None when the broker cannot work them out: the screen says so, it never shows a made-up 0.
    `buying_power` is only given by brokers that report it."""
    cash: float | None
    equity: float | None
    currency: str
    as_of: str
    buying_power: float | None = None


@dataclass(frozen=True, slots=True)
class Order:
    """An order as the broker reports it. `symbol`, `side` and `qty` are those of the first leg; `legs` has them all
    ({symbol, asset_type, instruction, position_effect, quantity}). `data` is the broker's own object, already masked."""
    account_id: int
    symbol: str
    side: str            # the broker's instruction of the first leg ("BUY", "SELL_TO_CLOSE"...) or "buy" | "sell"
    qty: float
    type: str = "market"
    limit_price: float | None = None
    order_id: str | None = None
    status: str | None = None
    duration: str | None = None
    stop_price: float | None = None
    filled_qty: float | None = None
    remaining_qty: float | None = None
    entered_at: str | None = None
    cancelable: bool | None = None
    editable: bool | None = None
    account_ref: str | None = None
    mask: str | None = None
    legs: tuple[dict[str, Any], ...] = ()
    data: dict[str, Any] | None = None
    multiplier: Decimal | None = None       # of the contract sheet (futures 20, options 100...); None while there is no sheet
    contract_id: str | None = None


@dataclass(frozen=True, slots=True)
class Fill:
    account_id: int
    symbol: str
    side: str
    qty: float
    price: float
    commission: float
    at: str
    order_id: str | None = None
    account_ref: str | None = None
    mask: str | None = None
    multiplier: Decimal | None = None       # of the contract sheet; the cash value of a fill is qty * price * multiplier
    contract_id: str | None = None


@dataclass(frozen=True, slots=True)
class Position:
    """`qty` is long minus short. `market_value` is the broker's, signed as the broker gives it. `multiplier` is that of the contract sheet (an option 100, a future
    its own) and None while the position has no sheet: a position with no sheet is not valued (AH-23). `qty` is signed. `data` is the broker's own object, already masked."""
    account_id: int
    symbol: str
    qty: float
    avg_price: float
    asset_type: str | None = None
    long_qty: float | None = None
    short_qty: float | None = None
    market_value: float | None = None
    day_pl: float | None = None
    day_pl_pct: float | None = None
    open_pl: float | None = None
    multiplier: float | None = None
    put_call: str | None = None
    underlying: str | None = None
    account_ref: str | None = None
    mask: str | None = None
    data: dict[str, Any] | None = None
    contract_id: str | None = None

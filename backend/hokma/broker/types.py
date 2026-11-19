"""What a broker reports. Order, Fill and Position are declared for class 9; nothing in class 7 builds them."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Capabilities:
    accounts: bool = True
    reset: bool = True
    orders: bool = False
    positions: bool = False


@dataclass(frozen=True, slots=True)
class Balance:
    """`cash` and `equity` are None when the broker cannot work them out: the screen says so, it never shows a made-up 0."""
    cash: float | None
    equity: float | None
    currency: str
    as_of: str


@dataclass(frozen=True, slots=True)
class Order:
    account_id: int
    symbol: str
    side: str            # "buy" | "sell"
    qty: float
    type: str = "market"
    limit_price: float | None = None


@dataclass(frozen=True, slots=True)
class Fill:
    account_id: int
    symbol: str
    side: str
    qty: float
    price: float
    commission: float
    at: str


@dataclass(frozen=True, slots=True)
class Position:
    account_id: int
    symbol: str
    qty: float
    avg_price: float

"""Money and quantities as Decimal from the proposal to the broker (SPEC-018: OA-56).

A float never carries a price, a quantity or an amount inside `hokma.exec`: `dec` is the only way in, it reads an int, a Decimal or
the text of a number (a float of a JSON answer goes through its shortest text, never through binary arithmetic), and `text` is the only way out.
Rounding to the tick of the contract sheet, the value of a quantity with the multiplier, and the `reduce_only` clip are here.
"""
from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

ZERO = Decimal(0)
MAX_ABS = Decimal("1e15")


class MoneyError(ValueError):
    """The value is not a finite number inside the limits."""


def dec(value: Any, what: str = "value") -> Decimal:
    if isinstance(value, bool) or value is None:
        raise MoneyError(f"{what} must be a number")
    if isinstance(value, Decimal):
        out = value
    elif isinstance(value, int):
        out = Decimal(value)
    elif isinstance(value, float):
        out = Decimal(repr(value))
    elif isinstance(value, str):
        try:
            out = Decimal(value.strip())
        except InvalidOperation as exc:
            raise MoneyError(f"{what} is not a number") from exc
    else:
        raise MoneyError(f"{what} must be a number")
    if not out.is_finite() or abs(out) > MAX_ABS:
        raise MoneyError(f"{what} must be a finite number below 1e15")
    return out


def dec_or_none(value: Any, what: str = "value") -> Decimal | None:
    return None if value is None else dec(value, what)


def text(value: Decimal | None) -> str | None:
    """The stable text of a Decimal: no exponent, no trailing zeros (`1.50` and `1.5` are the same text)."""
    if value is None:
        return None
    out = format(value.normalize(), "f")
    return "0" if out in ("-0", "") else out


def round_to_tick(price: Decimal, tick: Decimal | None, *, direction: str = "nearest") -> Decimal:
    """`price` on the grid of `tick`. `direction`: nearest (half up), down or up."""
    if tick is None or tick <= 0:
        return price
    steps = price / tick
    mode = {"nearest": ROUND_HALF_UP, "down": ROUND_FLOOR, "up": ROUND_CEILING}[direction]
    return steps.to_integral_value(rounding=mode) * tick


def value_of(qty: Decimal, price: Decimal, multiplier: Decimal) -> Decimal:
    """The cash value of a quantity: qty * price * multiplier, always positive."""
    return abs(qty * price * multiplier)


def clip_reduce_only(side: str, qty: Decimal, position_qty: Decimal) -> tuple[Decimal, bool]:
    """A `reduce_only` order never opens the opposite position: a sell is at most the long, a buy at most the short.
    Returns (quantity, clipped)."""
    room = position_qty if side == "sell" else -position_qty
    room = max(room, ZERO)
    if qty > room:
        return room, True
    return qty, False

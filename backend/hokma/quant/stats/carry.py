"""Carry of a future from the term structure: the traded contract against the next one (SPEC-020: P-05).

The carry is the price gap between the two contracts per year of distance between their expirations, in price units: positive when the traded
contract is dearer than the next one (backwardation), negative in contango. As a share of the traded price it is the yearly yield of holding the
traded contract and rolling down. Years are calendar days over 365. Without the next contract there is no carry: the result is None with a warning.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.stats import _common as c
from hokma.quant.types import QuantResult

_PIECE = ("P-05",)
DAYS_PER_YEAR = 365.0


def _expiry(label: str, value: Any) -> date:
    if isinstance(value, bool) or not isinstance(value, (str, date)):
        raise BadInput(f"{label} must be a date text such as 2026-12-21, got {type(value).__name__}")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise c.Domain(f"{label} must be a date such as 2026-12-21") from exc


@registry.register("stats.futures_carry", module="stats",
                   summary="Annualised carry of a future from its term structure: the traded contract (price, expiry) against the next contract (next_price, next_expiry). carry_annualized is (price - next_price) over the years between the two expirations, in price units; carry_pct_annualized divides it by the traded price. Positive is backwardation. Without the next contract the value is None with a warning.",
                   inputs={"price": {"type": "float", "required": True}, "expiry": {"type": "str", "required": True},
                           "next_price": {"type": "float", "required": False, "default": None}, "next_expiry": {"type": "str", "required": False, "default": None}},
                   piece_ids=_PIECE)
def futures_carry(price: float, expiry: Any, next_price: float | None = None, next_expiry: Any = None) -> QuantResult:
    name = "stats.futures_carry"
    params = {"price": price if isinstance(price, (int, float)) else None, "expiry": str(expiry), "next_price": next_price, "next_expiry": None if next_expiry is None else str(next_expiry)}

    def compute() -> Any:
        traded = c.number("price", price, low=0.0)
        near = _expiry("expiry", expiry)
        if traded <= 0.0:
            raise c.Domain("price must be positive")
        if next_price is None or next_expiry is None:
            raise c.Domain("no second expiry: carry needs the next contract (next_price and next_expiry)")
        other = c.number("next_price", next_price, low=0.0)
        far = _expiry("next_expiry", next_expiry)
        if other <= 0.0:
            raise c.Domain("next_price must be positive")
        days = (far - near).days
        if days <= 0:
            raise c.Domain("next_expiry must be after expiry")
        years = days / DAYS_PER_YEAR
        carry = (traded - other) / years
        return {"carry_annualized": carry, "carry_pct_annualized": carry / traded, "spread": traded - other, "years_between": years, "days_between": days,
                "structure": "backwardation" if traded > other else "contango" if traded < other else "flat"}

    return c.run(name, unit="price_per_year", method="term_structure_carry", params=params, piece_ids=_PIECE, compute=compute)

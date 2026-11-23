"""Valuation that fails loudly (SPEC-018: AH-23). A position with no market price is an error `unpriced` that names the symbol: it is
never worth zero. The snapshot of the account marks it, and nothing downstream treats the missing value as a number."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal

from hokma.backoffice.cost_basis import CostPosition
from hokma.exec.money import ZERO


class Unpriced(LookupError):
    """The position has no market price. `symbol` is the contract id."""

    code = "unpriced"

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        super().__init__(f"unpriced: {symbol} has no market price")


@dataclass(frozen=True)
class ValuedPosition:
    contract_id: str
    qty: Decimal
    avg_price: Decimal
    price: Decimal
    multiplier: Decimal
    market_value: Decimal        # signed: a short is negative
    unrealized: Decimal


def value(position: CostPosition, price: Decimal | None) -> ValuedPosition:
    if price is None:
        raise Unpriced(position.contract_id)
    mv = position.qty * price * position.multiplier
    return ValuedPosition(position.contract_id, position.qty, position.avg_price, price, position.multiplier, mv,
                          (price - position.avg_price) * position.qty * position.multiplier)


def value_all(positions: Iterable[CostPosition], prices: Mapping[str, Decimal]) -> tuple[list[ValuedPosition], dict[str, str]]:
    """The valued positions and, for every one with no price, its error (the others are still valued)."""
    out: list[ValuedPosition] = []
    errors: dict[str, str] = {}
    for p in positions:
        if p.qty == 0:
            continue
        try:
            out.append(value(p, prices.get(p.contract_id)))
        except Unpriced as exc:
            errors[exc.symbol] = exc.code
    return out, errors


def total(values: Iterable[ValuedPosition]) -> Decimal:
    return sum((v.market_value for v in values), ZERO)

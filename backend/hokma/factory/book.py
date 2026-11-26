"""The dollar neutral book of a long and short strategy and its check at every rebalance (SPEC-020: AH-31).

The book of a rebalance is built from the views of the names: the long side shares half of the gross amount among the names with a positive
view in proportion to the view, and the short side shares the other half among the names with a negative view. Each side is shared in whole
cents with the exact allocation of `hokma.exec.algos.allocate` (the pure function that slices orders), so the two sides add up to the same
amount to the cent and the net is zero. A side with no name cannot be balanced: the book is empty and the rebalance is reported, never filled.

`neutral_gaps` is the check a backtest runs at each rebalance of the grid: the dates where the net of the book is not zero (inside `tolerance`
dollars) or where there is no book.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal

from hokma.quant.errors import BadInput

CENT = Decimal("0.01")
ZERO = Decimal(0)


def dollar_neutral_book(views: Mapping[str, float], gross_usd: Decimal) -> dict[str, Decimal]:
    """Signed dollars per name: positive views long, negative views short, half the gross amount on each side. Names with no view are left out."""
    from hokma.exec.algos import AlgoError, allocate      # the pure part of the slicing: the module that sends orders is never imported

    gross = Decimal(gross_usd)
    if not gross.is_finite() or gross <= 0:
        raise BadInput("gross_usd is an amount above zero")
    longs = {n: Decimal(repr(float(v))) for n, v in sorted(views.items()) if v is not None and v > 0}
    shorts = {n: Decimal(repr(-float(v))) for n, v in sorted(views.items()) if v is not None and v < 0}
    if not longs or not shorts:
        return {}
    cents = (gross / 2 / CENT).to_integral_value()
    try:
        long_part, short_part = allocate(cents, tuple(longs.values())), allocate(cents, tuple(shorts.values()))
    except AlgoError as exc:
        raise BadInput(str(exc)) from exc
    return {**{n: q * CENT for n, q in zip(longs, long_part)}, **{n: -q * CENT for n, q in zip(shorts, short_part)}}


def net_usd(book: Mapping[str, Decimal]) -> Decimal:
    """The sum of the signed dollars: zero for a neutral book."""
    return sum(book.values(), ZERO)


def neutral_gaps(books: Mapping[date, Mapping[str, Decimal]], tolerance: Decimal = ZERO) -> tuple[date, ...]:
    """The rebalance dates (in order) whose book is empty or whose net is further from zero than `tolerance` dollars."""
    return tuple(d for d, book in sorted(books.items()) if not book or abs(net_usd(book)) > tolerance)

"""Fundamental rank alpha (SPEC-020: AH-31). Long the best ranked names and short the worst, from percentile ranks."""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from hokma.desks.core.signal import Criterion, Signal, SignalContext
from hokma.factory.alphas._base import fact, view
from hokma.factory.params import Param, ParamSchema

BOOKS = ("dollar_neutral", "long_only")
RANKS = ("value_rank", "quality_rank", "momentum_rank")


@dataclass(frozen=True)
class FundamentalRank:
    """composite = weighted mean of the three percentile ranks (0 is the worst name of the universe, 1 the best);
    value = 2 * (composite - 0.5), in [-1, +1].

    `book` is `dollar_neutral` when the sleeve is long and short (the portfolio module balances the two sides in dollars) or
    `long_only`, where a composite below the middle is a flat view. The ranks come from the fundamentals and price history.
    """

    weight_value: float = 0.4
    weight_quality: float = 0.4
    weight_momentum: float = 0.2
    book: str = "dollar_neutral"

    SCHEMA: ClassVar[ParamSchema] = ParamSchema((
        Param("weight_value", "float", 0.4, 0.0, 1.0), Param("weight_quality", "float", 0.4, 0.0, 1.0),
        Param("weight_momentum", "float", 0.2, 0.0, 1.0), Param("book", "choice", "dollar_neutral", choices=BOOKS)))
    FACTS: ClassVar[tuple[str, ...]] = RANKS

    def __post_init__(self) -> None:
        if self.weight_value + self.weight_quality + self.weight_momentum <= 0:
            raise ValueError("at least one weight must be above zero")
        if self.book not in BOOKS:
            raise ValueError(f"book is one of {', '.join(BOOKS)}")

    @property
    def lookback(self) -> int:
        return 1

    def signal(self, ctx: SignalContext) -> Signal:
        ranks = {n: fact(ctx, n) for n in RANKS}
        gaps = tuple(n for n, v in ranks.items() if v is None)
        if gaps or any(not 0.0 <= float(v[-1]) <= 1.0 for v in ranks.values()):      # type: ignore[index]
            return ctx.abstain("The three percentile ranks are needed and each is between zero and one. No view is given without them.",
                               gaps or RANKS)
        weights = (self.weight_value, self.weight_quality, self.weight_momentum)
        levels = [float(ranks[n][-1]) for n in RANKS]                                  # type: ignore[index]
        composite = sum(w * x for w, x in zip(weights, levels)) / sum(weights)
        value = 2.0 * (composite - 0.5)
        if self.book == "long_only":
            value = max(0.0, value)
        side = "above" if composite > 0.5 else "below" if composite < 0.5 else "at"
        criteria = tuple(Criterion(n, x, 0.5, x > 0.5, "fundamentals.rank") for n, x in zip(RANKS, levels))
        return view(ctx, "alpha.fundamental_rank", value, criteria + (Criterion("composite_rank", composite, 0.5, composite > 0.5, "alpha.fundamental_rank"),),
                    f"The weighted rank of the name is {side} the middle of the universe. The view grows with the distance from the middle.")

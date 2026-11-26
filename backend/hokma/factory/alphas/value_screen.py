"""Value screen alpha (SPEC-020: AH-30). A long only view from the share of value criteria a name meets."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar

from hokma.desks.core.signal import Criterion, Signal, SignalContext
from hokma.factory.alphas._base import fact, view
from hokma.factory.params import Param, ParamSchema

ASSET_CLASSES = ("equity", "etf", "future")
# The criteria that exist for each asset class. A future has no statements: only its own price speaks for value.
CRITERIA_BY_ASSET_CLASS: Mapping[str, tuple[str, ...]] = {
    "equity": ("earnings_yield", "fcf_yield", "book_to_price"),
    "etf": ("earnings_yield", "book_to_price"),
    "future": ("price_to_average",),
}


@dataclass(frozen=True)
class ValueScreen:
    """value = criteria met / criteria of the asset class, in [0, 1]. Never short: no criterion met is a flat view.

    Equity reads earnings_yield, fcf_yield and book_to_price (each at or above its minimum). An ETF reads the first and the
    last (look through values of its holdings). A future reads price_to_average, the price over its long run average, which
    must be at or below `max_price_to_average`. The facts come from the fundamentals module and the price history.
    """

    asset_class: str = "equity"
    min_earnings_yield: float = 0.08
    min_fcf_yield: float = 0.06
    min_book_to_price: float = 1.0
    max_price_to_average: float = 0.8

    SCHEMA: ClassVar[ParamSchema] = ParamSchema((
        Param("asset_class", "choice", "equity", choices=ASSET_CLASSES), Param("min_earnings_yield", "float", 0.08, 0.0, 1.0),
        Param("min_fcf_yield", "float", 0.06, 0.0, 1.0), Param("min_book_to_price", "float", 1.0, 0.0, 100.0),
        Param("max_price_to_average", "float", 0.8, 0.01, 5.0)))
    CRITERIA_BY_ASSET_CLASS: ClassVar[Mapping[str, tuple[str, ...]]] = CRITERIA_BY_ASSET_CLASS

    def __post_init__(self) -> None:
        if self.asset_class not in ASSET_CLASSES:
            raise ValueError(f"asset_class is one of {', '.join(ASSET_CLASSES)}")

    @property
    def FACTS(self) -> tuple[str, ...]:      # noqa: N802 - the same name the other alphas use, here per asset class
        return CRITERIA_BY_ASSET_CLASS[self.asset_class]

    @property
    def lookback(self) -> int:
        return 1

    def _limit(self, name: str) -> float:
        return {"earnings_yield": self.min_earnings_yield, "fcf_yield": self.min_fcf_yield, "book_to_price": self.min_book_to_price,
                "price_to_average": self.max_price_to_average}[name]

    def signal(self, ctx: SignalContext) -> Signal:
        names = self.FACTS
        values = {n: fact(ctx, n) for n in names}
        gaps = tuple(n for n, v in values.items() if v is None)
        if gaps:
            return ctx.abstain("Some value inputs are not available. No view is given until all of them have data.", gaps)
        criteria = []
        for name in names:
            level, limit = float(values[name][-1]), self._limit(name)      # type: ignore[index]
            passed = level <= limit if name == "price_to_average" else level >= limit
            criteria.append(Criterion(name, level, limit, passed, "fundamentals.value" if name != "price_to_average" else "data.close"))
        met = sum(1 for c in criteria if c.passed)
        return view(ctx, "alpha.value_screen", met / len(criteria), criteria,
                    f"{met} of {len(criteria)} value criteria are met for this {self.asset_class}. "
                    "The view is long only: it grows with the share of criteria met and is flat when none is met.")

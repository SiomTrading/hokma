"""Inflection alpha (SPEC-020: AH-32). The change of a rate over a window: acceleration is a long view, deterioration a short view."""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from hokma.desks.core.signal import Criterion, Signal, SignalContext
from hokma.factory.alphas._base import fact, view
from hokma.factory.params import Param, ParamSchema

SOURCES = ("growth", "price_roc", "macro_change")


@dataclass(frozen=True)
class Inflection:
    """value = (x[-1] - x[-1-window]) / cap, clipped to [-1, +1], where x is the series named by `source`.

    `growth` is the growth rate of a fundamental (statements), `price_roc` the rate of change of the price and
    `macro_change` the change of a macro series (read with a fixed vintage). A line chooses its own `window`, in observations
    of the series: quarters for statements, bars for prices, months for macro data.
    """

    window: int = 4
    cap: float = 0.05
    source: str = "growth"

    SCHEMA: ClassVar[ParamSchema] = ParamSchema((Param("window", "int", 4, 1, 1000), Param("cap", "float", 0.05, 1e-9, 100.0),
                                                 Param("source", "choice", "growth", choices=SOURCES)))

    def __post_init__(self) -> None:
        if self.source not in SOURCES:
            raise ValueError(f"source is one of {', '.join(SOURCES)}")

    @property
    def FACTS(self) -> tuple[str, ...]:      # noqa: N802 - the same name the other alphas use, here per source
        return (self.source,)

    @property
    def lookback(self) -> int:
        return self.window + 1

    def signal(self, ctx: SignalContext) -> Signal:
        series = fact(ctx, self.source, self.lookback)
        if series is None:
            return ctx.abstain("The series is too short for the window. No view is given until it has enough observations.", (self.source,))
        change = float(series[-1] - series[-self.lookback])
        word = "rose" if change > 0 else "fell" if change < 0 else "did not change"
        return view(ctx, "alpha.inflection", change / self.cap, (Criterion(f"{self.source}_change", change, 0.0, change > 0, f"data.{self.source}"),),
                    f"The {self.source} series {word} over the last {self.window} observations. "
                    "Acceleration is a long view and deterioration a short view.")

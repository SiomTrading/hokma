"""RSI mean reversion alpha (SPEC-020: L-52). Oversold is a long view, overbought is a short view."""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from hokma.desks.core.signal import Criterion, Signal, SignalContext
from hokma.factory.alphas._base import fact, view
from hokma.factory.params import Param, ParamSchema
from hokma.factory.strategy import IndicatorRequest


@dataclass(frozen=True)
class RsiReversion:
    """Below `oversold` the value is (oversold - rsi) / oversold, above `overbought` it is -(rsi - overbought) / (100 - overbought)."""

    length: int = 14
    oversold: float = 30.0
    overbought: float = 70.0

    SCHEMA: ClassVar[ParamSchema] = ParamSchema((Param("length", "int", 14, 2, 500), Param("oversold", "float", 30.0, 1.0, 49.0),
                                                 Param("overbought", "float", 70.0, 51.0, 99.0)))
    FACTS: ClassVar[tuple[str, ...]] = ("rsi",)

    def __post_init__(self) -> None:
        if not 0 < self.oversold < 50 < self.overbought < 100:
            raise ValueError("need 0 < oversold < 50 < overbought < 100")

    @property
    def lookback(self) -> int:
        return 1

    def requests(self) -> tuple[IndicatorRequest, ...]:
        return (IndicatorRequest("rsi", "rsi", "close", (("length", self.length),), self.length + 1),)

    def signal(self, ctx: SignalContext) -> Signal:
        rsi = fact(ctx, "rsi")
        if rsi is None:
            return ctx.abstain("The relative strength index is not available yet. No view is given until it has data.", ("rsi",))
        level = float(rsi[-1])
        if level < self.oversold:
            value, text = (self.oversold - level) / self.oversold, "below the oversold level"
        elif level > self.overbought:
            value, text = -(level - self.overbought) / (100.0 - self.overbought), "above the overbought level"
        else:
            value, text = 0.0, "between the two levels"
        return view(ctx, "alpha.rsi_reversion", value, (Criterion("rsi", level, self.oversold, level < self.oversold, "indicator.rsi"),),
                    f"The index is {text}. The view leans against the recent move and is flat inside the band.")

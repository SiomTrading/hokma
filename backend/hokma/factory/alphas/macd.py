"""MACD alpha (SPEC-020: L-52). The line above its signal line is a long view, below is a short view."""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from hokma.desks.core.signal import Criterion, Signal, SignalContext
from hokma.factory.alphas._base import fact, view
from hokma.factory.params import Param, ParamSchema
from hokma.factory.strategy import IndicatorRequest


@dataclass(frozen=True)
class Macd:
    """value = (macd - signal line) / close divided by `scale`, clipped to [-1, +1]. Reads macd_line, macd_signal and close."""

    fast: int = 12
    slow: int = 26
    signal_length: int = 9
    scale: float = 0.002

    SCHEMA: ClassVar[ParamSchema] = ParamSchema((Param("fast", "int", 12, 1, 500), Param("slow", "int", 26, 2, 1000),
                                                 Param("signal_length", "int", 9, 1, 500), Param("scale", "float", 0.002, 1e-6, 1.0)))
    FACTS: ClassVar[tuple[str, ...]] = ("macd_line", "macd_signal", "close")

    def __post_init__(self) -> None:
        if self.fast >= self.slow:
            raise ValueError("fast must be shorter than slow")

    @property
    def lookback(self) -> int:
        return 1

    def requests(self) -> tuple[IndicatorRequest, ...]:
        base = (("fast", self.fast), ("slow", self.slow))
        return (IndicatorRequest("macd_line", "macd_line", "close", base, self.slow),
                IndicatorRequest("macd_signal", "macd_signal", "close", base + (("signal", self.signal_length),),
                                 self.slow + self.signal_length))

    def signal(self, ctx: SignalContext) -> Signal:
        line, sig, close = fact(ctx, "macd_line"), fact(ctx, "macd_signal"), fact(ctx, "close")
        if line is None or sig is None or close is None or close[-1] <= 0:
            return ctx.abstain("The MACD line, its signal line or the close is not available yet. No view is given.",
                               ("macd_line", "macd_signal", "close"))
        gap = float((line[-1] - sig[-1]) / close[-1])
        side = "above" if gap > 0 else "below" if gap < 0 else "equal to"
        return view(ctx, "alpha.macd", gap / self.scale, (Criterion("macd_gap", gap, 0.0, gap > 0, "indicator.macd"),),
                    f"The MACD line is {side} its signal line. The view follows the sign of the gap relative to the close.")

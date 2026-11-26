"""Exponential moving average cross alpha (SPEC-020: L-52). Fast above slow is a long view, below is a short view."""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from hokma.desks.core.signal import Criterion, Signal, SignalContext
from hokma.factory.alphas._base import fact, view
from hokma.factory.params import Param, ParamSchema
from hokma.factory.strategy import IndicatorRequest


@dataclass(frozen=True)
class EmaCross:
    """value = (fast - slow) / slow divided by `scale`, clipped to [-1, +1]. Reads the facts ema_fast and ema_slow."""

    fast: int = 12
    slow: int = 26
    scale: float = 0.01

    SCHEMA: ClassVar[ParamSchema] = ParamSchema((Param("fast", "int", 12, 1, 500), Param("slow", "int", 26, 2, 1000),
                                                 Param("scale", "float", 0.01, 1e-6, 1.0)))
    FACTS: ClassVar[tuple[str, ...]] = ("ema_fast", "ema_slow")

    def __post_init__(self) -> None:
        if self.fast >= self.slow:
            raise ValueError("fast must be shorter than slow")

    @property
    def lookback(self) -> int:
        return 1

    def requests(self) -> tuple[IndicatorRequest, ...]:
        return (IndicatorRequest("ema_fast", "ema", "close", (("length", self.fast),), self.fast),
                IndicatorRequest("ema_slow", "ema", "close", (("length", self.slow),), self.slow))

    def signal(self, ctx: SignalContext) -> Signal:
        fast, slow = fact(ctx, "ema_fast"), fact(ctx, "ema_slow")
        if fast is None or slow is None or slow[-1] == 0:
            return ctx.abstain("The two averages are not available yet. No view is given until both have data.", ("ema_fast", "ema_slow"))
        gap = float(fast[-1] / slow[-1] - 1.0)
        side = "above" if gap > 0 else "below" if gap < 0 else "equal to"
        return view(ctx, "alpha.ema_cross", gap / self.scale, (Criterion("ema_gap", gap, 0.0, gap > 0, "indicator.ema"),),
                    f"The fast average is {side} the slow average. The view follows the sign of the gap.")

"""Historical returns alpha (SPEC-020: L-52). The past return over a window is the view."""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from hokma.desks.core.signal import Criterion, Signal, SignalContext
from hokma.factory.alphas._base import fact, view
from hokma.factory.params import Param, ParamSchema


@dataclass(frozen=True)
class HistoricalReturns:
    """value = (close[-1] / close[-1-periods] - 1) divided by `cap`, clipped to [-1, +1]. Reads the fact close."""

    periods: int = 252
    cap: float = 0.20

    SCHEMA: ClassVar[ParamSchema] = ParamSchema((Param("periods", "int", 252, 1, 5000), Param("cap", "float", 0.20, 1e-6, 10.0)))
    FACTS: ClassVar[tuple[str, ...]] = ("close",)

    @property
    def lookback(self) -> int:
        return self.periods + 1

    def signal(self, ctx: SignalContext) -> Signal:
        close = fact(ctx, "close", self.lookback)
        if close is None or close[-self.lookback] <= 0:
            return ctx.abstain("There are not enough closes for the return window. No view is given until the window is full.", ("close",))
        ret = float(close[-1] / close[-self.lookback] - 1.0)
        word = "positive" if ret > 0 else "negative" if ret < 0 else "flat"
        return view(ctx, "alpha.historical_returns", ret / self.cap, (Criterion("window_return", ret, 0.0, ret > 0, "data.close"),),
                    f"The return over the window is {word}. The view is that return as a share of the cap.")

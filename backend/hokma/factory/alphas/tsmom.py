"""Time series momentum alpha (SPEC-020: FM-20). The sign of the 12 month return, weighted by 1 over sigma to a volatility target."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import ClassVar

import numpy as np

from hokma.desks.core.signal import Criterion, Signal, SignalContext
from hokma.factory.alphas._base import fact, view
from hokma.factory.params import Param, ParamSchema


def tsmom_weight(trailing_return: float, sigma_annual: float, target_vol: float) -> float:
    """sign(return) * target_vol / sigma, clipped to [-1, +1]. Zero when the return is zero."""
    if sigma_annual <= 0 or not math.isfinite(sigma_annual):
        raise ValueError("sigma must be a positive number")
    sign = (trailing_return > 0) - (trailing_return < 0)
    return max(-1.0, min(1.0, sign * target_vol / sigma_annual))


@dataclass(frozen=True)
class Tsmom:
    """Sign of the return over `periods` bars (252 daily bars is 12 months), weight 1 / sigma, volatility target `target_vol`.

    Sigma is the annualized sample volatility of the last `vol_periods` simple returns, computed by hokma.quant.metrics.
    Reads the fact close.
    """

    periods: int = 252
    vol_periods: int = 60
    target_vol: float = 0.10
    periods_per_year: int = 252

    SCHEMA: ClassVar[ParamSchema] = ParamSchema((Param("periods", "int", 252, 2, 5000), Param("vol_periods", "int", 60, 2, 5000),
                                                 Param("target_vol", "float", 0.10, 0.001, 2.0),
                                                 Param("periods_per_year", "int", 252, 1, 100000)))
    FACTS: ClassVar[tuple[str, ...]] = ("close",)

    @property
    def lookback(self) -> int:
        return max(self.periods, self.vol_periods) + 1

    def signal(self, ctx: SignalContext) -> Signal:
        from hokma.quant.metrics.volatility import volatility   # imported here: the metrics module loads its library on import

        close = fact(ctx, "close", self.lookback)
        if close is None or np.any(close[-self.lookback:] <= 0):
            return ctx.abstain("There are not enough positive closes for the momentum window. No view is given until it is full.", ("close",))
        trailing = float(close[-1] / close[-1 - self.periods] - 1.0)
        window = close[-self.vol_periods - 1:]
        result = volatility(window[1:] / window[:-1] - 1.0, periods_per_year=self.periods_per_year)
        sigma = result.value
        if sigma is None or not math.isfinite(sigma) or sigma <= 0:
            return ctx.abstain("The volatility of the window is not defined. No view is given.", ("volatility",))
        value = tsmom_weight(trailing, float(sigma), self.target_vol)
        word = "positive" if trailing > 0 else "negative" if trailing < 0 else "flat"
        return view(ctx, "alpha.tsmom", value, (Criterion("trailing_return", trailing, 0.0, trailing > 0, "data.close"),
                                               Criterion("sigma_annual", float(sigma), self.target_vol, None, "metrics.volatility")),
                    f"The return over the window is {word}. The weight is scaled so the view carries {self.target_vol:.0%} annual volatility.")

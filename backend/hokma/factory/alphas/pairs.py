"""Pairs alpha by correlation (SPEC-020: L-52). The spread of two correlated prices reverts to its mean."""
from __future__ import annotations

import numpy as np
from dataclasses import dataclass
from typing import ClassVar

from hokma.desks.core.signal import Criterion, Signal, SignalContext
from hokma.factory.alphas._base import fact, view
from hokma.factory.params import Param, ParamSchema


@dataclass(frozen=True)
class PairsCorrelation:
    """View on the first symbol against a partner, from the z-score of log(a) - beta * log(b) over `window` bars.

    The hedge ratio beta is the least squares slope of log(a) on log(b). A rich spread (z above zero) is a short view,
    a cheap one a long view: value = -z / (2 * entry_z), clipped. The view is flat when the correlation of the two log
    price changes is below `min_corr`. Reads the facts close and pair_close.
    """

    window: int = 60
    min_corr: float = 0.7
    entry_z: float = 2.0

    SCHEMA: ClassVar[ParamSchema] = ParamSchema((Param("window", "int", 60, 5, 5000), Param("min_corr", "float", 0.7, 0.0, 1.0),
                                                 Param("entry_z", "float", 2.0, 0.1, 10.0)))
    FACTS: ClassVar[tuple[str, ...]] = ("close", "pair_close")

    @property
    def lookback(self) -> int:
        return self.window

    def signal(self, ctx: SignalContext) -> Signal:
        a, b = fact(ctx, "close", self.window), fact(ctx, "pair_close", self.window)
        if a is None or b is None or np.any(a[-self.window:] <= 0) or np.any(b[-self.window:] <= 0):
            return ctx.abstain("Both price series need a full window of positive closes. No view is given until they have it.",
                               ("close", "pair_close"))
        la, lb = np.log(a[-self.window:]), np.log(b[-self.window:])
        var_b = float(np.sum((lb - lb.mean()) ** 2))
        if var_b == 0.0:
            return ctx.abstain("The partner price did not move in the window. The hedge ratio cannot be estimated.", ("pair_close",))
        beta = float(np.sum((lb - lb.mean()) * (la - la.mean())) / var_b)
        spread = la - beta * lb
        sd = float(np.std(spread, ddof=1))
        corr_in = np.corrcoef(np.diff(la), np.diff(lb))[0, 1] if self.window > 2 else float("nan")
        if sd == 0.0 or not np.isfinite(corr_in):
            return ctx.abstain("The spread or the correlation is not defined in the window. No view is given.", ("close", "pair_close"))
        z = float((spread[-1] - spread.mean()) / sd)
        criteria = (Criterion("spread_z", z, self.entry_z, abs(z) >= self.entry_z, "data.close"),
                    Criterion("change_correlation", float(corr_in), self.min_corr, corr_in >= self.min_corr, "data.close"))
        if corr_in < self.min_corr:
            return view(ctx, "alpha.pairs_correlation", 0.0, criteria,
                        "The two prices are not correlated enough. The view is flat.")
        side = "rich" if z > 0 else "cheap" if z < 0 else "at its mean"
        return view(ctx, "alpha.pairs_correlation", -z / (2.0 * self.entry_z), criteria,
                    f"The spread against the partner is {side}. The view bets on its return to the mean.")

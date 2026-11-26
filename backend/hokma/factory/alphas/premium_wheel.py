"""Premium wheel alpha (SPEC-020: AS-006). A view that selling option premium pays, from the ratio of implied to realized volatility."""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from hokma.desks.core.signal import Criterion, Signal, SignalContext
from hokma.factory.alphas._base import fact, view
from hokma.factory.params import Param, ParamSchema


@dataclass(frozen=True)
class PremiumWheel:
    """value = (ratio - threshold) / cap, in [0, 1], where ratio is the implied over the realized volatility.

    The wheel has two phases: cash secured puts while the account holds fewer than `lot` shares of the underlying and covered
    calls once it holds a lot. Both phases are long exposure that earns the premium, so the view is never negative.
    `threshold` is a grid value for backtests: the threshold of a desk is the calibrated one. Selling premium needs the premium
    switch of the desk to be open, and this alpha cannot be built without declaring it: REQUIRES_PREMIUM_SWITCH is always true.
    """

    threshold: float = 1.2
    cap: float = 0.6
    lot: int = 100
    requires_premium_switch: bool = True

    REQUIRES_PREMIUM_SWITCH: ClassVar[bool] = True
    SCHEMA: ClassVar[ParamSchema] = ParamSchema((Param("threshold", "float", 1.2, 0.01, 20.0), Param("cap", "float", 0.6, 1e-6, 20.0),
                                                 Param("lot", "int", 100, 1, 100000), Param("requires_premium_switch", "bool", True)))
    FACTS: ClassVar[tuple[str, ...]] = ("iv_rv_ratio", "shares_held")

    def __post_init__(self) -> None:
        if self.requires_premium_switch is not True:
            raise ValueError("a premium selling recipe always requires the premium switch; it cannot be declared false")

    @property
    def lookback(self) -> int:
        return 1

    def signal(self, ctx: SignalContext) -> Signal:
        ratio, shares = fact(ctx, "iv_rv_ratio"), fact(ctx, "shares_held")
        if ratio is None or shares is None:
            return ctx.abstain("The volatility ratio and the shares held are both needed. No view is given until both have data.",
                               ("iv_rv_ratio", "shares_held"))
        level = float(ratio[-1])
        phase = "covered calls" if float(shares[-1]) >= self.lot else "cash secured puts"
        return view(ctx, "alpha.premium_wheel", max(0.0, (level - self.threshold) / self.cap),
                    (Criterion("iv_rv_ratio", level, self.threshold, level > self.threshold, "options.iv_rv_ratio"),
                     Criterion("shares_held", float(shares[-1]), float(self.lot), float(shares[-1]) >= self.lot, "account.shares")),
                    f"The implied volatility is {level:.2f} times the realized one against a threshold of {self.threshold:.2f}. "
                    f"The phase of the wheel is {phase}. The view is flat when the premium is not rich enough.")

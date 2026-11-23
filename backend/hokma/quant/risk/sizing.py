"""Position sizing caps."""
from __future__ import annotations

import math

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.risk import _common as c
from hokma.quant.types import QuantResult


@registry.register("risk.kelly_payoff", module="risk",
                   summary="Kelly fraction for a bet with win probability p and payoff b (win b per unit risked): f = (p*b - (1-p)) / b. Never negative: a losing edge gives 0 with a warning. Use it as a cap, each desk sets its own fraction.",
                   inputs={"p_win": {"type": "float", "required": True}, "payoff": {"type": "float", "required": True}},
                   piece_ids=("FM-06",))
def kelly_payoff(p_win: float, payoff: float) -> QuantResult:
    name, unit, method, pieces = "risk.kelly_payoff", "fraction", "kelly_payoff", ("FM-06",)
    for label, val in (("p_win", p_win), ("payoff", payoff)):
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise BadInput(f"{label} must be a number, got {type(val).__name__}")
    params = {"p_win": p_win, "payoff": payoff}
    if not (math.isfinite(p_win) and math.isfinite(payoff)) or not 0.0 <= p_win <= 1.0 or payoff <= 0.0:
        return c.empty(name, unit, method, "p_win must be in [0, 1] and payoff must be positive", params=params, piece_ids=pieces)
    raw = (p_win * payoff - (1.0 - p_win)) / payoff
    warns: tuple[str, ...] = ()
    if raw <= 0.0:
        warns = (f"no edge: the Kelly fraction is {raw:.6f}, sized at 0",)
    return c.build(name, max(0.0, raw), unit=unit, method=method, params=params, piece_ids=pieces, inputs=(params,), warnings=warns)

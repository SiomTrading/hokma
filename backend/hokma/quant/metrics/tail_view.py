"""Tail view: value at risk, conditional value at risk and expected shortfall of a return series.

Two estimators: parametric (normal distribution fitted to the series) and historical (the empirical quantile and the mean
of the returns at or below it). Figures are returns, negative when they are losses.
"""
from __future__ import annotations

from typing import Any

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.metrics import _lib
from hokma.quant.metrics.summary import _SERIES_IN, empty, finish, prepare_series, scalar
from hokma.quant.types import QuantResult

_METHODS = ("parametric", "historical")


@registry.register("metrics.tail_view", module="metrics",
                   summary="Value at risk, conditional value at risk and expected shortfall at a confidence level, parametric or historical.",
                   inputs={"returns": _SERIES_IN, "level": {"type": "float", "required": False, "default": 0.95},
                           "method": {"type": "str", "required": False, "default": "parametric"}}, piece_ids=("QS-023",))
def tail_view(returns: Any, level: float = 0.95, method: str = "parametric") -> QuantResult:
    name, pieces = "metrics.tail_view", ("QS-023",)
    if method not in _METHODS:
        raise BadInput(f"method must be one of {_METHODS}, got {method!r}")
    if not isinstance(level, (int, float)) or isinstance(level, bool) or not 0.5 <= level < 1.0:
        raise BadInput("level must be a confidence between 0.5 and 1 (exclusive)")
    params = {"level": level, "method": method}
    label = "normal distribution fitted to the series" if method == "parametric" else "empirical quantile and tail mean"
    r, warns = prepare_series(returns, min_obs=5)
    if r is None:
        return empty(name, "pct", label, warns, pieces, params)
    s = _lib.returns_stats()
    if method == "parametric":
        var = scalar(s.value_at_risk, r, confidence=level, prepare_returns=False)
        cvar = scalar(s.conditional_value_at_risk, r, confidence=level, prepare_returns=False, method="parametric")
    else:
        var = scalar(r.quantile, 1.0 - level)
        cvar = scalar(s.conditional_value_at_risk, r, confidence=level, prepare_returns=False, method="historical")
    value = {"value_at_risk": var, "conditional_value_at_risk": cvar, "expected_shortfall": cvar}
    return finish(name, value, unit="pct", method=label, series=r, pieces=pieces, params=params, warnings=warns)

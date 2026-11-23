"""Drawdown based risk: depth, duration, tails of the drawdown distribution and the account pain index.

Drawdowns are compounded from the period returns and reported as positive depths (0.20 means a 20 percent fall).
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.risk import _common as c
from hokma.quant.risk import _lib
from hokma.quant.types import QuantResult

_RET = {"type": "series", "required": True}
_LEVEL = {"type": "float", "required": False, "default": 0.95}
MEASURES = ("max", "average", "dar", "cdar", "edar", "ulcer")


def _drawdowns(s: pd.Series) -> np.ndarray:
    return _lib.sk_measures.get_drawdowns(s.to_numpy(), compounded=True)


def _tail(name: str, returns: Any, level: float, piece: str, method: str, fn) -> QuantResult:
    return c.measure(name, returns, unit="pct_depth", method=method, params={"level": level}, piece_ids=(piece,),
                     compute=lambda s: fn(_drawdowns(s), level), level=level)


def _by_measure(measure: str, s: pd.Series, level: float) -> float:
    dd = _drawdowns(s)
    m = _lib.sk_measures
    return {
        "max": lambda: m.max_drawdown(dd),
        "average": lambda: m.average_drawdown(dd),
        "dar": lambda: m.drawdown_at_risk(dd, beta=level),
        "cdar": lambda: m.cdar(dd, beta=level),
        "edar": lambda: m.edar(dd, beta=level),
        "ulcer": lambda: m.ulcer_index(dd),
    }[measure]()


@registry.register("risk.drawdown_risk", module="risk",
                   summary="One drawdown measure of the returns: max, average, dar, cdar, edar or ulcer (compounded, positive depth).",
                   inputs={"returns": _RET, "measure": {"type": "str", "required": False, "default": "cdar"}, "level": _LEVEL},
                   piece_ids=("SK-028", "SK-029", "SK-030", "FT-050", "FT-053"))
def drawdown_measure(returns: Any, measure: str = "cdar", level: float = 0.95) -> QuantResult:
    pieces = ("SK-028", "SK-029", "SK-030", "FT-050", "FT-053")
    params = {"measure": measure, "level": level}
    if measure not in MEASURES:
        return c.empty("risk.drawdown_risk", "pct_depth", measure, f"unknown measure {measure!r}; use one of {list(MEASURES)}",
                       params=params, piece_ids=pieces)
    return c.measure("risk.drawdown_risk", returns, unit="pct_depth", method=measure, params=params, piece_ids=pieces,
                     compute=lambda s: _by_measure(measure, s, level), level=level)


@registry.register("risk.drawdown_series", module="risk",
                   summary="Cumulative returns and drawdowns of a returns series (the base of the P&L and NAV curves).",
                   inputs={"returns": _RET, "compounded": {"type": "bool", "required": False, "default": True}},
                   piece_ids=("SK-027",))
def drawdown_series(returns: Any, compounded: bool = True) -> QuantResult:
    def compute(s: pd.Series) -> dict:
        x = s.to_numpy()
        return {"cumulative_returns": _lib.sk_measures.get_cumulative_returns(x, compounded=compounded),
                "drawdowns": _lib.sk_measures.get_drawdowns(x, compounded=compounded)}

    return c.measure("risk.drawdown_series", returns, unit="series", method="compounded" if compounded else "cumulative_sum",
                     params={"compounded": compounded}, piece_ids=("SK-027",), compute=compute)


@registry.register("risk.worst_realization", module="risk", summary="Worst realized period loss of the series, as a positive number.",
                   inputs={"returns": _RET}, piece_ids=("SK-023",))
def worst_realization(returns: Any) -> QuantResult:
    return c.measure("risk.worst_realization", returns, unit="loss_fraction", method="worst_realization", params={}, piece_ids=("SK-023",),
                     compute=lambda s: _lib.sk_measures.worst_realization(s.to_numpy()))


@registry.register("risk.max_drawdown", module="risk", summary="Maximum drawdown depth (compounded).",
                   inputs={"returns": _RET}, piece_ids=("SK-028",))
def max_drawdown(returns: Any) -> QuantResult:
    return c.measure("risk.max_drawdown", returns, unit="pct_depth", method="max_drawdown", params={}, piece_ids=("SK-028",),
                     compute=lambda s: _lib.sk_measures.max_drawdown(_drawdowns(s)))


@registry.register("risk.average_drawdown", module="risk", summary="Average drawdown depth over the sample (compounded).",
                   inputs={"returns": _RET}, piece_ids=("SK-028",))
def average_drawdown(returns: Any) -> QuantResult:
    return c.measure("risk.average_drawdown", returns, unit="pct_depth", method="average_drawdown", params={}, piece_ids=("SK-028",),
                     compute=lambda s: _lib.sk_measures.average_drawdown(_drawdowns(s)))


@registry.register("risk.drawdown_at_risk", module="risk", summary="Drawdown at risk: the drawdown depth not exceeded with the given confidence.",
                   inputs={"returns": _RET, "level": _LEVEL}, piece_ids=("SK-028",))
def drawdown_at_risk(returns: Any, level: float = 0.95) -> QuantResult:
    return _tail("risk.drawdown_at_risk", returns, level, "SK-028", "historical_dar",
                 lambda dd, lv: _lib.sk_measures.drawdown_at_risk(dd, beta=lv))


@registry.register("risk.cdar", module="risk", summary="Conditional drawdown at risk: mean depth of the worst drawdowns beyond the confidence level.",
                   inputs={"returns": _RET, "level": _LEVEL}, piece_ids=("SK-029", "FT-053"))
def cdar(returns: Any, level: float = 0.95) -> QuantResult:
    return c.measure("risk.cdar", returns, unit="pct_depth", method="historical_cdar", params={"level": level},
                     piece_ids=("SK-029", "FT-053"), compute=lambda s: _lib.sk_measures.cdar(_drawdowns(s), beta=level), level=level)


@registry.register("risk.edar", module="risk", summary="Entropic drawdown at risk: the exponential bound on the drawdown tail.",
                   inputs={"returns": _RET, "level": _LEVEL}, piece_ids=("SK-029",))
def edar(returns: Any, level: float = 0.95) -> QuantResult:
    return _tail("risk.edar", returns, level, "SK-029", "entropic_dar", lambda dd, lv: _lib.sk_measures.edar(dd, beta=lv))


@registry.register("risk.ulcer_index", module="risk", summary="Ulcer index: root mean square of the drawdowns, the pain of holding the account.",
                   inputs={"returns": _RET}, piece_ids=("SK-030", "FT-050"))
def ulcer_index(returns: Any) -> QuantResult:
    return c.measure("risk.ulcer_index", returns, unit="pct_depth", method="rms_of_drawdowns", params={}, piece_ids=("SK-030", "FT-050"),
                     compute=lambda s: _lib.sk_measures.ulcer_index(_drawdowns(s)))


@registry.register("risk.rolling_cdar", module="risk",
                   summary="Conditional drawdown at risk over a rolling window; the latest value and the full path.",
                   inputs={"returns": _RET, "window": {"type": "int", "required": True}, "level": _LEVEL}, piece_ids=("FT-053",))
def rolling_cdar(returns: Any, window: int, level: float = 0.95) -> QuantResult:
    if isinstance(window, bool) or not isinstance(window, (int, np.integer)):
        raise BadInput(f"window must be an integer, got {type(window).__name__}")
    params = {"window": int(window), "level": level}

    def compute(s: pd.Series) -> dict:
        if window < 2 or window > len(s):
            raise ValueError(f"window must be between 2 and {len(s)}")
        path = -_lib.ft_risk_model.get_rolling_conditional_drawdown_at_risk(c.period_index(s), 1.0 - level, int(window))
        path = path.dropna()
        return {"latest": path.iloc[-1], "index": [str(i) for i in path.index], "values": path.to_numpy()}

    return c.measure("risk.rolling_cdar", returns, unit="pct_depth", method="rolling_cdar", params=params, piece_ids=("FT-053",),
                     compute=compute, min_obs=3, level=level)


@registry.register("risk.drawdown_stats", module="risk",
                   summary="Maximum drawdown depth, its duration and its recovery time (in index periods of the series; recovery is None while the high is not regained).",
                   inputs={"returns": _RET}, piece_ids=("FT-049",))
def drawdown_stats(returns: Any) -> QuantResult:
    def compute(s: pd.Series) -> dict:
        indexed = c.period_index(s)
        model = _lib.ft_risk_model
        out: dict[str, Any] = {"max_drawdown": -float(model.get_max_drawdown(indexed))}
        for key, fn in (("duration", model.get_max_drawdown_duration), ("recovery_time", model.get_max_drawdown_recovery_time)):
            try:
                out[key] = fn(indexed)
            except Exception:  # the index has no calendar: depth is still valid
                out[key] = None
        return out

    return c.measure("risk.drawdown_stats", returns, unit="pct_depth", method="max_drawdown_duration_recovery", params={},
                     piece_ids=("FT-049",), compute=compute)

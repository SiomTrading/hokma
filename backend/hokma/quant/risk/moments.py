"""Moments and dispersion measures of a returns series (risk module).

Every function takes a returns Series (or list/array) and returns a QuantResult. Measures are per period, not annualized.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from hokma.quant import registry
from hokma.quant.risk import _common as c
from hokma.quant.risk import _lib
from hokma.quant.types import QuantResult

_RET = {"type": "series", "required": True}
_MAR = {"type": "float", "required": False, "default": None}
_BIASED = {"type": "bool", "required": False, "default": False}


def _series_measure(name: str, returns: Any, piece: str, method: str, unit: str, fn, params: dict | None = None) -> QuantResult:
    return c.measure(name, returns, unit=unit, method=method, params=params or {}, piece_ids=(piece,),
                     compute=lambda s: fn(s.to_numpy()))


@registry.register("risk.mean", module="risk", summary="Arithmetic mean of the period returns.",
                   inputs={"returns": _RET}, piece_ids=("SK-016",))
def mean(returns: Any) -> QuantResult:
    return _series_measure("risk.mean", returns, "SK-016", "arithmetic_mean", "return", _lib.sk_measures.mean)


@registry.register("risk.mean_absolute_deviation", module="risk",
                   summary="Mean absolute deviation around a minimum acceptable return (default: the mean).",
                   inputs={"returns": _RET, "min_acceptable_return": _MAR}, piece_ids=("SK-017",))
def mean_absolute_deviation(returns: Any, min_acceptable_return: float | None = None) -> QuantResult:
    return _series_measure("risk.mean_absolute_deviation", returns, "SK-017", "mean_absolute_deviation", "return",
                           lambda x: _lib.sk_measures.mean_absolute_deviation(x, min_acceptable_return=min_acceptable_return),
                           {"min_acceptable_return": min_acceptable_return})


@registry.register("risk.first_lower_partial_moment", module="risk",
                   summary="First lower partial moment: mean shortfall below a minimum acceptable return.",
                   inputs={"returns": _RET, "min_acceptable_return": _MAR}, piece_ids=("SK-018",))
def first_lower_partial_moment(returns: Any, min_acceptable_return: float | None = None) -> QuantResult:
    return _series_measure("risk.first_lower_partial_moment", returns, "SK-018", "first_lower_partial_moment", "return",
                           lambda x: _lib.sk_measures.first_lower_partial_moment(x, min_acceptable_return=min_acceptable_return),
                           {"min_acceptable_return": min_acceptable_return})


@registry.register("risk.variance", module="risk", summary="Sample variance of the period returns (ddof=1 unless biased).",
                   inputs={"returns": _RET, "biased": _BIASED}, piece_ids=("SK-019",))
def variance(returns: Any, biased: bool = False) -> QuantResult:
    return _series_measure("risk.variance", returns, "SK-019", "biased_variance" if biased else "sample_variance", "return^2",
                           lambda x: _lib.sk_measures.variance(x, biased=biased), {"biased": biased})


@registry.register("risk.semi_variance", module="risk",
                   summary="Variance of the returns below a minimum acceptable return (default: the mean).",
                   inputs={"returns": _RET, "min_acceptable_return": _MAR, "biased": _BIASED}, piece_ids=("SK-019",))
def semi_variance(returns: Any, min_acceptable_return: float | None = None, biased: bool = False) -> QuantResult:
    return _series_measure("risk.semi_variance", returns, "SK-019", "semi_variance", "return^2",
                           lambda x: _lib.sk_measures.semi_variance(x, min_acceptable_return=min_acceptable_return, biased=biased),
                           {"min_acceptable_return": min_acceptable_return, "biased": biased})


@registry.register("risk.standard_deviation", module="risk", summary="Sample standard deviation of the period returns.",
                   inputs={"returns": _RET, "biased": _BIASED}, piece_ids=("SK-020",))
def standard_deviation(returns: Any, biased: bool = False) -> QuantResult:
    return _series_measure("risk.standard_deviation", returns, "SK-020", "standard_deviation", "return",
                           lambda x: _lib.sk_measures.standard_deviation(x, biased=biased), {"biased": biased})


@registry.register("risk.semi_deviation", module="risk",
                   summary="Downside deviation: square root of the semi variance.",
                   inputs={"returns": _RET, "min_acceptable_return": _MAR, "biased": _BIASED}, piece_ids=("SK-020",))
def semi_deviation(returns: Any, min_acceptable_return: float | None = None, biased: bool = False) -> QuantResult:
    return _series_measure("risk.semi_deviation", returns, "SK-020", "semi_deviation", "return",
                           lambda x: _lib.sk_measures.semi_deviation(x, min_acceptable_return=min_acceptable_return, biased=biased),
                           {"min_acceptable_return": min_acceptable_return, "biased": biased})


@registry.register("risk.third_central_moment", module="risk", summary="Third central moment of the returns.",
                   inputs={"returns": _RET}, piece_ids=("SK-021",))
def third_central_moment(returns: Any) -> QuantResult:
    return _series_measure("risk.third_central_moment", returns, "SK-021", "third_central_moment", "return^3",
                           _lib.sk_measures.third_central_moment)


@registry.register("risk.skew", module="risk", summary="Skewness: third central moment over the cubed standard deviation.",
                   inputs={"returns": _RET}, piece_ids=("SK-021",))
def skew(returns: Any) -> QuantResult:
    return _series_measure("risk.skew", returns, "SK-021", "skewness", "ratio", _lib.sk_measures.skew)


@registry.register("risk.fourth_central_moment", module="risk", summary="Fourth central moment of the returns.",
                   inputs={"returns": _RET}, piece_ids=("SK-022",))
def fourth_central_moment(returns: Any) -> QuantResult:
    return _series_measure("risk.fourth_central_moment", returns, "SK-022", "fourth_central_moment", "return^4",
                           _lib.sk_measures.fourth_central_moment)


@registry.register("risk.kurtosis", module="risk", summary="Raw kurtosis: fourth central moment over the squared variance (normal = 3).",
                   inputs={"returns": _RET}, piece_ids=("SK-022",))
def kurtosis(returns: Any) -> QuantResult:
    return _series_measure("risk.kurtosis", returns, "SK-022", "raw_kurtosis", "ratio", _lib.sk_measures.kurtosis)


@registry.register("risk.fourth_lower_partial_moment", module="risk",
                   summary="Fourth lower partial moment below a minimum acceptable return.",
                   inputs={"returns": _RET, "min_acceptable_return": _MAR}, piece_ids=("SK-022",))
def fourth_lower_partial_moment(returns: Any, min_acceptable_return: float | None = None) -> QuantResult:
    return _series_measure("risk.fourth_lower_partial_moment", returns, "SK-022", "fourth_lower_partial_moment", "return^4",
                           lambda x: _lib.sk_measures.fourth_lower_partial_moment(x, min_acceptable_return=min_acceptable_return),
                           {"min_acceptable_return": min_acceptable_return})


@registry.register("risk.correlation", module="risk",
                   summary="Correlation matrix of a returns panel, optionally with observation weights.",
                   inputs={"panel": {"type": "dataframe", "required": True}, "sample_weight": {"type": "list", "required": False, "default": None}},
                   piece_ids=("SK-033",))
def correlation(panel: Any, sample_weight: Any = None) -> QuantResult:
    name, unit, method, pieces = "risk.correlation", "ratio", "weighted_correlation" if sample_weight is not None else "pearson", ("SK-033",)
    frame = c.as_panel(panel)
    cleaned, warns = c.clean_panel(frame, min_obs=3)
    params = {"weighted": sample_weight is not None}
    if not c.panel_ok(cleaned, warns, 3):
        return c.empty(name, unit, method, warns or ("degenerate input",), params=params, piece_ids=pieces)
    weights = None
    if sample_weight is not None:
        weights = np.asarray(sample_weight, dtype=float)
        if weights.shape != (len(frame),):
            raise c.BadInput(f"sample_weight: expected {len(frame)} values, got shape {weights.shape}")
        weights = weights[frame.replace([np.inf, -np.inf], np.nan).notna().all(axis=1).to_numpy()]
        if (weights < 0).any() or weights.sum() <= 0:
            return c.empty(name, unit, method, warns + ("sample_weight must be non negative with a positive sum",), params=params, piece_ids=pieces)
        weights = weights / weights.sum()
    try:
        matrix = _lib.sk_measures.correlation(cleaned.to_numpy(), sample_weight=weights)
    except Exception as exc:
        return c.empty(name, unit, method, warns + (f"{type(exc).__name__}: {exc}",), params=params, piece_ids=pieces)
    value = {"assets": [str(x) for x in cleaned.columns], "matrix": matrix}
    return c.build(name, value, unit=unit, method=method, params=params, piece_ids=pieces, inputs=(cleaned,),
                   as_of_source=cleaned.iloc[:, 0], warnings=warns)

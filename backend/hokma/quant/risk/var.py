"""Value at risk and expected shortfall.

Convention of the whole module: a risk figure is a positive loss as a fraction of the position
(0.02 means a loss of 2 percent). `level` is the confidence (0.95 means the worst 5 percent of periods).
"""
from __future__ import annotations

import warnings
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
_METHOD = {"type": "str", "required": False, "default": "historical"}

VAR_METHODS = ("historical", "empirical", "gaussian", "student_t", "cornish_fisher", "evt")
CVAR_METHODS = ("historical", "empirical", "gaussian", "student_t", "laplace", "logistic", "cornish_fisher", "evt")
_DISTRIBUTION = {"historical": "historic", "gaussian": "gaussian", "cornish_fisher": "cornish-fisher", "student_t": "studentt"}

VAR_PIECES = ("FT-041", "FT-042", "FT-043", "SK-024", "CC-06")
CVAR_PIECES = ("FT-045", "SK-025")


def _f(value: Any) -> float:
    """One float out of a float, a one element Series or an array."""
    return float(np.asarray(value, dtype=float).ravel()[0])


def _quiet(fn, *args, **kwargs):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return fn(*args, **kwargs)


def _var(s: pd.Series, level: float, method: str) -> float:
    alpha = 1.0 - level
    ft = _lib.ft_var
    if method == "empirical":
        return _lib.sk_measures.value_at_risk(s.to_numpy(), beta=level)
    if method == "historical":
        return -_f(ft.get_var_historic(s, alpha))
    if method == "gaussian":
        return -_f(ft.get_var_gaussian(s, alpha))
    if method == "student_t":
        return -_f(_quiet(ft.get_var_studentt, s, alpha))
    if method == "cornish_fisher":
        return -_f(ft.get_var_cornish_fisher(s, alpha))
    return -_f(_quiet(ft.get_var_evt, s, alpha, warn=False))


def _cvar(s: pd.Series, level: float, method: str) -> float:
    alpha = 1.0 - level
    ft = _lib.ft_cvar
    if method == "empirical":
        return _lib.sk_measures.cvar(s.to_numpy(), beta=level)
    if method == "evt":
        return -_f(_quiet(ft.get_cvar_evt, s, alpha, warn=False))
    fn = {"historical": ft.get_cvar_historic, "gaussian": ft.get_cvar_gaussian, "student_t": ft.get_cvar_studentt,
          "laplace": ft.get_cvar_laplace, "logistic": ft.get_cvar_logistic, "cornish_fisher": ft.get_cvar_cornish_fisher}[method]
    return -_f(_quiet(fn, s, alpha))


@registry.register("risk.var", module="risk",
                   summary="Value at risk as a positive loss. Methods: historical, empirical, gaussian, student_t, cornish_fisher, evt.",
                   inputs={"returns": _RET, "level": _LEVEL, "method": _METHOD}, piece_ids=VAR_PIECES)
def value_at_risk(returns: Any, level: float = 0.95, method: str = "historical") -> QuantResult:
    params = {"level": level, "method": method}
    if method not in VAR_METHODS:
        return c.empty("risk.var", "loss_fraction", method, f"unknown method {method!r}; use one of {list(VAR_METHODS)}",
                       params=params, piece_ids=VAR_PIECES)
    return c.measure("risk.var", returns, unit="loss_fraction", method=method, params=params, piece_ids=VAR_PIECES,
                     compute=lambda s: _var(s, level, method), min_obs=30 if method == "evt" else 5, level=level)


@registry.register("risk.cvar", module="risk",
                   summary="Expected shortfall as a positive loss. Methods: historical, empirical, gaussian, student_t, laplace, logistic, cornish_fisher, evt.",
                   inputs={"returns": _RET, "level": _LEVEL, "method": _METHOD}, piece_ids=CVAR_PIECES)
def conditional_value_at_risk(returns: Any, level: float = 0.95, method: str = "historical") -> QuantResult:
    params = {"level": level, "method": method}
    if method not in CVAR_METHODS:
        return c.empty("risk.cvar", "loss_fraction", method, f"unknown method {method!r}; use one of {list(CVAR_METHODS)}",
                       params=params, piece_ids=CVAR_PIECES)
    return c.measure("risk.cvar", returns, unit="loss_fraction", method=method, params=params, piece_ids=CVAR_PIECES,
                     compute=lambda s: _cvar(s, level, method), min_obs=30 if method == "evt" else 5, level=level)


def _rolling(name: str, returns: Any, window: int, level: float, pieces: tuple[str, ...], fn, method: str) -> QuantResult:
    if isinstance(window, bool) or not isinstance(window, (int, np.integer)):
        raise BadInput(f"window must be an integer, got {type(window).__name__}")
    params = {"window": int(window), "level": level}

    def compute(s: pd.Series) -> dict:
        if window < 2 or window > len(s):
            raise ValueError(f"window must be between 2 and {len(s)}")
        path = -fn(s, 1.0 - level, int(window)).dropna()
        return {"latest": path.iloc[-1], "index": [str(i) for i in path.index], "values": path.to_numpy()}

    return c.measure(name, returns, unit="loss_fraction", method=method, params=params, piece_ids=pieces, compute=compute,
                     min_obs=3, level=level)


@registry.register("risk.rolling_var", module="risk",
                   summary="Historical value at risk over a rolling window: the latest value and the full path.",
                   inputs={"returns": _RET, "window": {"type": "int", "required": True}, "level": _LEVEL}, piece_ids=("FT-041",))
def rolling_var(returns: Any, window: int, level: float = 0.95) -> QuantResult:
    return _rolling("risk.rolling_var", returns, window, level, ("FT-041",), _lib.ft_var.get_rolling_var_historic, "rolling_historical")


@registry.register("risk.rolling_cvar", module="risk",
                   summary="Historical expected shortfall over a rolling window: the latest value and the full path.",
                   inputs={"returns": _RET, "window": {"type": "int", "required": True}, "level": _LEVEL}, piece_ids=("FT-045",))
def rolling_cvar(returns: Any, window: int, level: float = 0.95) -> QuantResult:
    return _rolling("risk.rolling_cvar", returns, window, level, ("FT-045",), _lib.ft_cvar.get_rolling_cvar_historic, "rolling_historical")


@registry.register("risk.gpd_tail", module="risk",
                   summary="Generalized Pareto fit of the losses beyond a threshold percentile: shape, scale, threshold and exceedance probability.",
                   inputs={"returns": _RET, "threshold_percentile": {"type": "float", "required": False, "default": 0.95}},
                   piece_ids=("FT-042",))
def gpd_tail(returns: Any, threshold_percentile: float = 0.95) -> QuantResult:
    params = {"threshold_percentile": threshold_percentile}

    def compute(s: pd.Series) -> dict:
        shape, scale, threshold, prob = _quiet(_lib.ft_var.fit_gpd_tail, s.to_frame("x"), threshold_percentile)
        return {"shape": shape[0], "scale": scale[0], "threshold": threshold[0], "exceedance_probability": prob[0]}

    return c.measure("risk.gpd_tail", returns, unit="gpd", method="peaks_over_threshold", params=params, piece_ids=("FT-042",),
                     compute=compute, min_obs=30, level=threshold_percentile)


def _contribution(name: str, panel: Any, weights: Any, level: float, distribution: str, fn, piece: str, component: bool) -> QuantResult:
    unit, method, pieces = "loss_fraction", distribution, (piece,)
    params = {"level": level, "distribution": distribution}
    if distribution not in _DISTRIBUTION:
        return c.empty(name, unit, method, f"unknown distribution {distribution!r}; use one of {list(_DISTRIBUTION)}", params=params, piece_ids=pieces)
    problem = c.level_warning(level)
    if problem:
        return c.empty(name, unit, method, problem, params=params, piece_ids=pieces)
    frame = c.as_panel(panel)
    w = c.weights_series(weights, frame.columns)
    cleaned, warns = c.clean_panel(frame, min_obs=30)
    if not c.panel_ok(cleaned, warns, 30):
        return c.empty(name, unit, method, warns or ("degenerate input",), params=params, piece_ids=pieces)
    try:
        raw = _quiet(fn, cleaned, w, 1.0 - level, _DISTRIBUTION[distribution])
    except Exception as exc:
        return c.empty(name, unit, method, warns + (f"{type(exc).__name__}: {exc}",), params=params, piece_ids=pieces)
    contributions = {str(k): -float(v) for k, v in raw.items()}
    value: dict[str, Any] = {"contributions": contributions}
    if component:
        value["total"] = float(sum(contributions.values()))
    return c.build(name, value, unit=unit, method=method, params=params, piece_ids=pieces, inputs=(cleaned, w.to_dict()),
                   as_of_source=cleaned.iloc[:, 0], warnings=warns)


@registry.register("risk.marginal_var", module="risk",
                   summary="Marginal VaR of each asset: sensitivity of the portfolio VaR to a small increase of the asset weight.",
                   inputs={"panel": {"type": "dataframe", "required": True}, "weights": {"type": "dict", "required": True},
                           "level": _LEVEL, "distribution": _METHOD}, piece_ids=("FT-044",))
def marginal_var(panel: Any, weights: Any, level: float = 0.95, distribution: str = "historical") -> QuantResult:
    return _contribution("risk.marginal_var", panel, weights, level, distribution, _lib.ft_var.get_marginal_var, "FT-044", False)


@registry.register("risk.component_var", module="risk",
                   summary="Component VaR of each asset or desk: its share of the portfolio VaR. The components add up to the total.",
                   inputs={"panel": {"type": "dataframe", "required": True}, "weights": {"type": "dict", "required": True},
                           "level": _LEVEL, "distribution": _METHOD}, piece_ids=("FT-044",))
def component_var(panel: Any, weights: Any, level: float = 0.95, distribution: str = "historical") -> QuantResult:
    return _contribution("risk.component_var", panel, weights, level, distribution, _lib.ft_var.get_component_var, "FT-044", True)


@registry.register("risk.evar", module="risk",
                   summary="Entropic value at risk as a positive loss. Methods: gaussian (closed form) or empirical (from the sample).",
                   inputs={"returns": _RET, "level": _LEVEL, "method": {"type": "str", "required": False, "default": "gaussian"}},
                   piece_ids=("FT-046", "SK-026"))
def evar(returns: Any, level: float = 0.95, method: str = "gaussian") -> QuantResult:
    pieces = ("FT-046", "SK-026")
    params = {"level": level, "method": method}
    if method not in ("gaussian", "empirical"):
        return c.empty("risk.evar", "loss_fraction", method, f"unknown method {method!r}; use gaussian or empirical", params=params, piece_ids=pieces)

    def compute(s: pd.Series) -> float:
        if method == "gaussian":
            return -_f(_lib.ft_evar.get_evar_gaussian(s, 1.0 - level))
        return _lib.sk_measures.evar(s.to_numpy(), beta=level)

    return c.measure("risk.evar", returns, unit="loss_fraction", method=method, params=params, piece_ids=pieces, compute=compute,
                     min_obs=5, level=level)


@registry.register("risk.entropic_risk_measure", module="risk",
                   summary="Entropic risk measure with risk aversion theta at the given confidence.",
                   inputs={"returns": _RET, "theta": {"type": "float", "required": False, "default": 1.0}, "level": _LEVEL},
                   piece_ids=("SK-026",))
def entropic_risk_measure(returns: Any, theta: float = 1.0, level: float = 0.95) -> QuantResult:
    params = {"theta": theta, "level": level}
    if isinstance(theta, bool) or not isinstance(theta, (int, float)):
        raise BadInput(f"theta must be a number, got {type(theta).__name__}")
    if not theta > 0:
        return c.empty("risk.entropic_risk_measure", "loss_fraction", "entropic", "theta must be positive", params=params, piece_ids=("SK-026",))
    return c.measure("risk.entropic_risk_measure", returns, unit="loss_fraction", method="entropic", params=params, piece_ids=("SK-026",),
                     compute=lambda s: _lib.sk_measures.entropic_risk_measure(s.to_numpy(), theta=theta, beta=level), min_obs=5, level=level)


@registry.register("risk.covar", module="risk",
                   summary="CoVaR and delta CoVaR: the VaR of a series when a conditioning series is at its own VaR (contagion between assets or desks). Positive losses.",
                   inputs={"returns": _RET, "conditioning_returns": {"type": "series", "required": True}, "level": _LEVEL},
                   piece_ids=("FT-047",))
def covar(returns: Any, conditioning_returns: Any, level: float = 0.95) -> QuantResult:
    name, unit, method, pieces = "risk.covar", "loss_fraction", "quantile_regression", ("FT-047",)
    params = {"level": level}
    problem = c.level_warning(level)
    if problem:
        return c.empty(name, unit, method, problem, params=params, piece_ids=pieces)
    x, y, warns = c.pair(returns, conditioning_returns)
    if len(x) < 30 or c.is_constant(x) or c.is_constant(y):
        return c.empty(name, unit, method, warns + ("need 30 complete observations in both series and no constant series",),
                       params=params, piece_ids=pieces)
    try:
        table = _lib.ft_covar.get_covar(x, y, 1.0 - level)
    except Exception as exc:
        return c.empty(name, unit, method, warns + (f"{type(exc).__name__}: {exc}",), params=params, piece_ids=pieces)
    value = {"covar": -float(table["CoVaR"]), "delta_covar": -float(table["Delta-CoVaR"]),
             "slope": float(table["Quantile Regression Slope"]), "intercept": float(table["Quantile Regression Intercept"]),
             "observations": int(table["Observations"])}
    return c.build(name, value, unit=unit, method=method, params=params, piece_ids=pieces, inputs=(x, y), as_of_source=x, warnings=warns)

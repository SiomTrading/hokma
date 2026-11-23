"""Metrics against a benchmark: alpha and beta, CAPM, factor regressions, capture ratios, selectivity against risk and market timing.

Returns and benchmark are period returns on the same dates; the risk-free rate is an annual rate. Figures that need a yearly
scale (alpha, Jensen's alpha, residual deviation) are annualised. Factor datasets are passed in by the caller: this module
never downloads anything.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.metrics import _lib
from hokma.quant.metrics.summary import (_PPY_IN, _RF_IN, _SERIES_IN, align_pair, empty, finish, prepare_series, quiet, scalar)
from hokma.quant.types import QuantResult

_PAIR_IN = {"returns": _SERIES_IN, "benchmark": {"type": "series", "required": True}}


def _annual(r: pd.Series, ppy: int) -> float | None:
    total = float((1.0 + r).prod())
    years = len(r) / ppy
    return None if total <= 0 or years <= 0 else total ** (1.0 / years) - 1.0


def _daily_rf(rf: float, ppy: int) -> float:
    return (1.0 + rf) ** (1.0 / ppy) - 1.0


def _bad_args(rf: float, ppy: int) -> None:
    if not isinstance(rf, (int, float)) or isinstance(rf, bool) or not math.isfinite(rf) or rf <= -1.0:
        raise BadInput("rf must be a finite annual rate above -100%")
    if not isinstance(ppy, int) or isinstance(ppy, bool) or ppy < 1:
        raise BadInput("periods_per_year must be a positive integer")


def _pair(returns: Any, benchmark: Any, name: str, unit: str, method: str, pieces: tuple[str, ...], params: dict):
    r, b, warns = align_pair(returns, benchmark)
    if r is None:
        return None, None, None, empty(name, unit, method, warns, pieces, params)
    return r, b, warns, None


# --------------------------------------------------------------------------------------------------------------
# QS-033: alpha and beta against the benchmark (not option greeks)
# --------------------------------------------------------------------------------------------------------------
@registry.register("metrics.alpha_beta", module="metrics", summary="Annualised alpha and beta of the series against a benchmark. These are not option greeks.",
                   inputs={**_PAIR_IN, "periods_per_year": _PPY_IN}, piece_ids=("QS-033",))
def alpha_beta(returns: Any, benchmark: Any, periods_per_year: int = 252) -> QuantResult:
    name, pieces, method = "metrics.alpha_beta", ("QS-033",), "beta = cov / var; alpha = (mean return - beta * mean benchmark) * periods"
    params = {"periods_per_year": periods_per_year}
    _bad_args(0.0, periods_per_year)
    r, b, warns, bad = _pair(returns, benchmark, name, "mixed", method, pieces, params)
    if bad is not None:
        return bad
    with quiet():
        g = _lib.returns_stats().greeks(r, b, periods=periods_per_year, prepare_returns=False)
    return finish(name, {"alpha": g.get("alpha"), "beta": g.get("beta")}, unit="mixed", method=method, series=r, pieces=pieces, params=params,
                  warnings=warns, hash_extra=(b,))


@registry.register("metrics.alpha_beta_rolling", module="metrics", summary="Rolling alpha (per period) and beta against a benchmark over a window.",
                   inputs={**_PAIR_IN, "window": {"type": "int", "required": False, "default": 126}}, piece_ids=("QS-033",))
def alpha_beta_rolling(returns: Any, benchmark: Any, window: int = 126) -> QuantResult:
    name, pieces, method = "metrics.alpha_beta_rolling", ("QS-033",), "window regression: beta = corr * sd / sd benchmark; alpha = mean - beta * mean benchmark"
    if not isinstance(window, int) or isinstance(window, bool) or window < 3:
        raise BadInput("window must be an integer of at least 3")
    params = {"window": window}
    r, b, warns, bad = _pair(returns, benchmark, name, "mixed", method, pieces, params)
    if bad is not None:
        return bad
    if len(r) < window:
        return empty(name, "mixed", method, tuple(warns) + (f"need at least {window} observations, got {len(r)}",), pieces, params)
    with quiet():
        table = _lib.returns_stats().rolling_greeks(r, b, periods=window, prepare_returns=False).dropna()
    value = {"dates": [str(i)[:10] for i in table.index], "beta": table["beta"].tolist(), "alpha": table["alpha"].tolist(), "window": window}
    return finish(name, value, unit="mixed", method=method, series=r, pieces=pieces, params=params, warnings=warns, hash_extra=(b,))


# --------------------------------------------------------------------------------------------------------------
# QS-050: benchmark block of the summary
# --------------------------------------------------------------------------------------------------------------
@registry.register("metrics.versus_benchmark", module="metrics", summary="Benchmark block: beta, alpha, correlation and Treynor ratio.",
                   inputs={**_PAIR_IN, "rf": _RF_IN, "periods_per_year": _PPY_IN}, piece_ids=("QS-050",))
def versus_benchmark(returns: Any, benchmark: Any, rf: float = 0.0, periods_per_year: int = 252) -> QuantResult:
    name, pieces, method = "metrics.versus_benchmark", ("QS-050",), "regression slope and intercept, Pearson correlation, excess return over beta"
    _bad_args(rf, periods_per_year)
    params = {"rf": rf, "periods_per_year": periods_per_year}
    r, b, warns, bad = _pair(returns, benchmark, name, "mixed", method, pieces, params)
    if bad is not None:
        return bad
    s = _lib.returns_stats()
    with quiet():
        g = s.greeks(r, b, periods=periods_per_year, prepare_returns=False)
    value = {"beta": g.get("beta"), "alpha": g.get("alpha"), "correlation": float(r.corr(b)),
             "treynor_ratio": scalar(s.treynor_ratio, r, b, periods_per_year, rf)}
    return finish(name, value, unit="mixed", method=method, series=r, pieces=pieces, params=params, warnings=warns, hash_extra=(b,))


# --------------------------------------------------------------------------------------------------------------
# FT-069: CAPM, beta, alpha, Jensen, residuals, appraisal
# --------------------------------------------------------------------------------------------------------------
@registry.register("metrics.capm", module="metrics",
                   summary="Beta, CAPM expected return, Jensen's alpha, residual deviation and appraisal ratio against a benchmark (annualised).",
                   inputs={**_PAIR_IN, "rf": _RF_IN, "periods_per_year": _PPY_IN}, piece_ids=("FT-069",))
def capm(returns: Any, benchmark: Any, rf: float = 0.0, periods_per_year: int = 252) -> QuantResult:
    name, pieces, method = "metrics.capm", ("FT-069",), "beta on raw returns; Jensen alpha = R - [rf + beta (Rm - rf)] on annualised returns; appraisal = alpha / residual deviation"
    _bad_args(rf, periods_per_year)
    params = {"rf": rf, "periods_per_year": periods_per_year}
    r, b, warns, bad = _pair(returns, benchmark, name, "mixed", method, pieces, params)
    if bad is not None:
        return bad
    pm = _lib.performance_model()
    ra, ba = _annual(r, periods_per_year), _annual(b, periods_per_year)
    if ra is None or ba is None:
        return empty(name, "mixed", method, tuple(warns) + ("compounded wealth is not positive",), pieces, params)
    with quiet():
        beta = float(pm.get_beta(r, b))
        expected = float(pm.get_capital_asset_pricing_model(rf, beta, ba))
        jensen = float(pm.get_jensens_alpha(ra, rf, beta, ba))
        daily = _daily_rf(rf, periods_per_year)
        residuals = ((r - daily) - beta * (b - daily)) * math.sqrt(periods_per_year)
        resid_sd = float(residuals.std(ddof=1))
        appraisal = jensen / resid_sd if resid_sd > 0 else None
    warns = tuple(warns)
    if appraisal is None:
        warns += ("no residual risk: appraisal ratio is not defined",)
    value = {"beta": beta, "capm_expected_return": expected, "jensens_alpha": jensen, "residual_std_annual": resid_sd, "appraisal_ratio": appraisal,
             "return_annual": ra, "benchmark_return_annual": ba}
    return finish(name, value, unit="mixed", method=method, series=r, pieces=pieces, params=params, warnings=warns, hash_extra=(b,))


# --------------------------------------------------------------------------------------------------------------
# FT-070: Fama-French three factors and Carhart four factors from a dataset supplied by the caller
# --------------------------------------------------------------------------------------------------------------
_FACTORS = {"ff3": ("Mkt-RF", "SMB", "HML"), "carhart": ("Mkt-RF", "SMB", "HML", "Mom")}


@registry.register("metrics.factor_model", module="metrics",
                   summary="Three-factor (Mkt-RF, SMB, HML) or four-factor (plus Mom) regression of the excess return, on a factor dataset in decimals.",
                   inputs={"returns": _SERIES_IN, "factors": {"type": "dataframe", "required": True}, "rf": _RF_IN, "periods_per_year": _PPY_IN,
                           "model": {"type": "str", "required": False, "default": "ff3"}}, piece_ids=("FT-070",))
def factor_model(returns: Any, factors: Any, rf: float = 0.0, periods_per_year: int = 252, model: str = "ff3") -> QuantResult:
    name, pieces, method = "metrics.factor_model", ("FT-070",), "least squares of the excess return on the factor returns, with intercept"
    if model not in _FACTORS:
        raise BadInput(f"model must be one of {sorted(_FACTORS)}, got {model!r}")
    _bad_args(rf, periods_per_year)
    if not isinstance(factors, pd.DataFrame):
        raise BadInput(f"factors: expected a DataFrame, got {type(factors).__name__}")
    missing = [c for c in _FACTORS[model] if c not in factors.columns]
    if missing:
        raise BadInput(f"factors: missing column(s) {missing} for model {model!r}")
    params = {"rf": rf, "periods_per_year": periods_per_year, "model": model}
    r, warns = prepare_series(returns, min_obs=len(_FACTORS[model]) + 3)
    if r is None:
        return empty(name, "mixed", method, warns, pieces, params)
    dataset = factors[list(_FACTORS[model])].astype(float).replace([np.inf, -np.inf], np.nan)
    if not isinstance(dataset.index, pd.DatetimeIndex) or not isinstance(r.index, pd.DatetimeIndex):
        return empty(name, "mixed", method, tuple(warns) + ("factors and returns need a date index",), pieces, params)
    excess = r - _daily_rf(rf, periods_per_year)
    shared = excess.index.intersection(dataset.dropna().index)
    if len(shared) < len(_FACTORS[model]) + 3:
        return empty(name, "mixed", method, tuple(warns) + (f"returns and factors share only {len(shared)} date(s)",), pieces, params)
    with quiet():
        results, residuals, error = _lib.performance_model().get_fama_and_french_model_multi(excess.loc[shared], dataset.loc[shared])
    warns = tuple(warns) + ((error,) if error else ())
    value: dict[str, Any] = {"alpha": results.get("Intercept"), "alpha_annual": None, "r_squared": results.get("R Squared"),
                             "mean_squared_error": results.get("Mean Squared Error (MSE)"), "observations": int(len(shared)),
                             "loadings": {c: results.get(f"{c} Slope") for c in _FACTORS[model]}}
    if value["alpha"] is not None and not (isinstance(value["alpha"], float) and math.isnan(value["alpha"])):
        value["alpha_annual"] = float(value["alpha"]) * periods_per_year
    return finish(name, value, unit="mixed", method=method, series=excess.loc[shared], pieces=pieces, params=params, warnings=warns,
                  hash_extra=(dataset.loc[shared],))


# --------------------------------------------------------------------------------------------------------------
# FT-073: capture ratios
# --------------------------------------------------------------------------------------------------------------
@registry.register("metrics.capture_ratios", module="metrics",
                   summary="Upside and downside capture against the benchmark: geometric mean return of the series over that of the benchmark on up (down) days.",
                   inputs=_PAIR_IN, piece_ids=("FT-073",))
def capture_ratios(returns: Any, benchmark: Any) -> QuantResult:
    name, pieces, method = "metrics.capture_ratios", ("FT-073",), "geometric average of the series over geometric average of the benchmark, on benchmark up and down periods"
    r, b, warns, bad = _pair(returns, benchmark, name, "ratio", method, pieces, {})
    if bad is not None:
        return bad
    pm = _lib.performance_model()
    up, down = scalar(pm.get_upside_capture_ratio, r, b), scalar(pm.get_downside_capture_ratio, r, b)
    value = {"upside_capture": up, "downside_capture": down, "capture_spread": None if up is None or down is None else up - down}
    return finish(name, value, unit="ratio", method=method, series=r, pieces=pieces, warnings=warns, hash_extra=(b,))


# --------------------------------------------------------------------------------------------------------------
# FT-075: Fama decomposition (selectivity against risk)
# --------------------------------------------------------------------------------------------------------------
@registry.register("metrics.fama_decomposition", module="metrics",
                   summary="Splits Jensen's alpha into net selectivity (against total risk) and diversification (the cost of the risk not explained by the market).",
                   inputs={**_PAIR_IN, "rf": _RF_IN, "periods_per_year": _PPY_IN}, piece_ids=("FT-075",))
def fama_decomposition(returns: Any, benchmark: Any, rf: float = 0.0, periods_per_year: int = 252) -> QuantResult:
    name, pieces, method = "metrics.fama_decomposition", ("FT-075",), "net selectivity = (R - rf) - (sd / sd_m)(Rm - rf); diversification = (sd / sd_m - beta)(Rm - rf)"
    _bad_args(rf, periods_per_year)
    params = {"rf": rf, "periods_per_year": periods_per_year}
    r, b, warns, bad = _pair(returns, benchmark, name, "pct", method, pieces, params)
    if bad is not None:
        return bad
    ra, ba = _annual(r, periods_per_year), _annual(b, periods_per_year)
    if ra is None or ba is None:
        return empty(name, "pct", method, tuple(warns) + ("compounded wealth is not positive",), pieces, params)
    pm = _lib.performance_model()
    with quiet():
        beta = float(pm.get_beta(r, b))
        selectivity, diversification = pm.get_fama_decomposition(ra, rf, beta, ba, float(r.std(ddof=1)), float(b.std(ddof=1)))
        jensen = float(pm.get_jensens_alpha(ra, rf, beta, ba))
    value = {"net_selectivity": float(selectivity), "diversification": float(diversification), "jensens_alpha": jensen, "beta": beta,
             "total_risk_ratio": float(r.std(ddof=1) / b.std(ddof=1))}
    return finish(name, value, unit="pct", method=method, series=r, pieces=pieces, params=params, warnings=warns, hash_extra=(b,))


# --------------------------------------------------------------------------------------------------------------
# FT-076: market timing, Treynor-Mazuy and Henriksson-Merton
# --------------------------------------------------------------------------------------------------------------
@registry.register("metrics.market_timing", module="metrics",
                   summary="Market timing skill: quadratic (Treynor-Mazuy gamma) and piecewise (Henriksson-Merton up-market beta) regressions on excess returns.",
                   inputs={**_PAIR_IN, "rf": _RF_IN, "periods_per_year": _PPY_IN}, piece_ids=("FT-076",))
def market_timing(returns: Any, benchmark: Any, rf: float = 0.0, periods_per_year: int = 252) -> QuantResult:
    name, pieces, method = "metrics.market_timing", ("FT-076",), "excess = alpha + beta * mkt + gamma * mkt^2  and  excess = alpha + beta * mkt + beta_up * max(mkt, 0)"
    _bad_args(rf, periods_per_year)
    params = {"rf": rf, "periods_per_year": periods_per_year}
    r, b, warns, bad = _pair(returns, benchmark, name, "mixed", method, pieces, params)
    if bad is not None:
        return bad
    daily = _daily_rf(rf, periods_per_year)
    excess, bexcess = r - daily, b - daily
    pm = _lib.performance_model()
    with quiet():
        tm, _ = pm.get_treynor_mazuy_model(excess, bexcess)
        hm, _ = pm.get_henriksson_merton_model(excess, bexcess)
    value = {"treynor_mazuy": {"alpha": tm["Alpha"], "alpha_annual": None if tm["Alpha"] is None else float(tm["Alpha"]) * periods_per_year,
                               "beta": tm["Beta"], "gamma": tm["Gamma"], "r_squared": tm["R Squared"]},
             "henriksson_merton": {"alpha": hm["Alpha"], "alpha_annual": None if hm["Alpha"] is None else float(hm["Alpha"]) * periods_per_year,
                                   "down_market_beta": hm["Beta"], "up_market_beta_extra": hm["Up Market Beta"],
                                   "up_market_beta": None if hm["Beta"] is None or hm["Up Market Beta"] is None else float(hm["Beta"]) + float(hm["Up Market Beta"]),
                                   "r_squared": hm["R Squared"]}}
    return finish(name, value, unit="mixed", method=method, series=r, pieces=pieces, params=params, warnings=warns, hash_extra=(b,))

"""Volatility estimators of the stats module: range based estimators, HAR-RV, GJR-GARCH and EGARCH.

Rows FT-061, FT-062, FT-063, FT-064, FM-09 and FM-10. Yang-Zhang and Rogers-Satchell live here once: the indicators
module registers aliases that point at the same functions (see `ALIASES`).
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import limits, registry
from hokma.quant.errors import BadInput
from hokma.quant.stats import _common as c
from hokma.quant.stats import _lib
from hokma.quant.types import QuantResult

_RET = {"type": "series", "required": True}
_BARS = {"open_": {"type": "series", "required": True}, "high": {"type": "series", "required": True},
         "low": {"type": "series", "required": True}, "close": {"type": "series", "required": True}}
PERIODS = ("weekly", "monthly", "quarterly", "yearly")
ESTIMATORS = ("parkinson", "garman_klass", "rogers_satchell", "yang_zhang")
_RANGE_PIECES = ("FT-063",)
_YZ_PIECES = ("FT-063", "FM-09")
_RS_PIECES = ("FT-063", "FM-10")

# Public names under which the indicators module re-registers the same function objects (FM-09 and FM-10).
ALIASES = {"indicators.yang_zhang": "yang_zhang", "indicators.rogers_satchell": "rogers_satchell"}


def rogers_satchell_term(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """Per bar Rogers-Satchell variance term: ln(H/C) ln(H/O) + ln(L/C) ln(L/O). The one definition every estimator uses."""
    return np.log(high / close) * np.log(high / open_) + np.log(low / close) * np.log(low / open_)


def _period_path(estimator: str, bars: pd.DataFrame, period: str) -> pd.Series:
    indexed = bars.copy()
    if isinstance(indexed.index, pd.DatetimeIndex):
        indexed.index = indexed.index.to_period("D")
    else:
        raise BadInput("open_, high, low and close need a datetime index")
    o, h, lo, cl = indexed["open"], indexed["high"], indexed["low"], indexed["close"]
    fn = {"parkinson": lambda: _lib.ft_realized.get_parkinson_volatility(h, lo, period),
          "garman_klass": lambda: _lib.ft_realized.get_garman_klass_volatility(o, h, lo, cl, period),
          "rogers_satchell": lambda: _lib.ft_realized.get_rogers_satchell_volatility(o, h, lo, cl, period),
          "yang_zhang": lambda: _lib.ft_realized.get_yang_zhang_volatility(o, h, lo, cl, period)}[estimator]
    return fn().dropna()


def _estimate(name: str, estimator: str, pieces: tuple[str, ...], open_: Any, high: Any, low: Any, close: Any, period: str) -> QuantResult:
    params = {"estimator": estimator, "period": period}
    if estimator not in ESTIMATORS:
        return c.empty(name, "return", estimator, f"estimator must be one of {list(ESTIMATORS)}", params=params, piece_ids=pieces)
    if period not in PERIODS:
        return c.empty(name, "return", estimator, f"period must be one of {list(PERIODS)}", params=params, piece_ids=pieces)

    def compute() -> Any:
        bars, warns = c.ohlc_frame(open_, high, low, close, datetime_index=True)
        path = _period_path(estimator, bars, period)
        if path.empty:
            raise c.Domain("no complete period to estimate")
        value: dict[str, Any] = {"latest": float(path.iloc[-1]), "index": [str(i) for i in path.index], "values": path.to_numpy()}
        if estimator == "rogers_satchell":
            term = rogers_satchell_term(bars["open"], bars["high"], bars["low"], bars["close"])
            value["term_index"] = [str(i) for i in term.index]
            value["term"] = term.to_numpy()
        return value, warns

    return c.run(name, unit="return", method=estimator, params=params, piece_ids=pieces, compute=compute,
                 inputs=(open_, high, low, close) if all(isinstance(v, pd.Series) for v in (open_, high, low, close)) else ())


@registry.register("stats.range_estimators", module="stats",
                   summary="Range based volatility per period from open, high, low and close bars: parkinson, garman_klass, rogers_satchell or yang_zhang. Series need a datetime index.",
                   inputs={**_BARS, "estimator": {"type": "str", "required": False, "default": "yang_zhang"},
                           "period": {"type": "str", "required": False, "default": "monthly"}}, piece_ids=_RANGE_PIECES)
def range_estimators(open_: Any, high: Any, low: Any, close: Any, estimator: str = "yang_zhang", period: str = "monthly") -> QuantResult:
    if estimator == "yang_zhang":
        core = yang_zhang(open_, high, low, close, period)
    elif estimator == "rogers_satchell":
        core = rogers_satchell(open_, high, low, close, period)
    else:
        return _estimate("stats.range_estimators", estimator, _RANGE_PIECES, open_, high, low, close, period)
    return QuantResult(name="stats.range_estimators", value=core.value, unit=core.unit, method=core.method,
                       params=core.params, inputs_hash=core.inputs_hash, as_of=core.as_of, warnings=core.warnings,
                       piece_ids=_RANGE_PIECES)


@registry.register("stats.yang_zhang", module="stats",
                   summary="Yang-Zhang volatility per period from open, high, low and close bars; handles overnight gaps. Series need a datetime index.",
                   inputs={**_BARS, "period": {"type": "str", "required": False, "default": "monthly"}}, piece_ids=_YZ_PIECES)
def yang_zhang(open_: Any, high: Any, low: Any, close: Any, period: str = "monthly") -> QuantResult:
    return _estimate("stats.yang_zhang", "yang_zhang", _YZ_PIECES, open_, high, low, close, period)


@registry.register("stats.rogers_satchell", module="stats",
                   summary="Rogers-Satchell volatility per period, with the per bar variance term the other range estimators build on. Series need a datetime index.",
                   inputs={**_BARS, "period": {"type": "str", "required": False, "default": "monthly"}}, piece_ids=_RS_PIECES)
def rogers_satchell(open_: Any, high: Any, low: Any, close: Any, period: str = "monthly") -> QuantResult:
    return _estimate("stats.rogers_satchell", "rogers_satchell", _RS_PIECES, open_, high, low, close, period)


@registry.register("stats.har_rv", module="stats",
                   summary="HAR-RV: regress future daily realized variance on its daily, weekly and monthly averages; returns the coefficients and the forecast for the next days.",
                   inputs={"realized_variance": _RET, "weekly_window": {"type": "int", "required": False, "default": 5},
                           "monthly_window": {"type": "int", "required": False, "default": 22},
                           "horizon": {"type": "int", "required": False, "default": 1}}, piece_ids=("FT-064",))
def har_rv(realized_variance: Any, weekly_window: int = 5, monthly_window: int = 22, horizon: int = 1) -> QuantResult:
    name, pieces = "stats.har_rv", ("FT-064",)
    for label, v in (("weekly_window", weekly_window), ("monthly_window", monthly_window), ("horizon", horizon)):
        limits.check_param(label, v)
        limits.as_int(label, v)
    params = {"weekly_window": int(weekly_window), "monthly_window": int(monthly_window), "horizon": int(horizon)}

    def compute() -> Any:
        if not (1 <= weekly_window < monthly_window) or horizon < 1:
            raise c.Domain("need 1 <= weekly_window < monthly_window and horizon >= 1")
        rv, warns = c.clean_series(realized_variance, name="realized_variance", min_obs=monthly_window + horizon + 12)
        if (rv < 0).any():
            raise c.Domain("realized variance cannot be negative; pass variances, not volatilities of mixed sign")
        path = _lib.ft_realized.get_har_rv_forecast(rv, weekly_window, monthly_window, horizon)
        if path.dropna().empty:
            raise c.Domain("not enough observations for the HAR-RV regression")
        frame = pd.concat([rv.shift(-horizon), rv, rv.rolling(weekly_window).mean(), rv.rolling(monthly_window).mean()], axis=1)
        frame.columns = ["target", "daily", "weekly", "monthly"]
        sample = frame.dropna()
        x = np.column_stack([np.ones(len(sample)), sample[["daily", "weekly", "monthly"]].to_numpy()])
        y = sample["target"].to_numpy()
        beta = np.linalg.lstsq(x, y, rcond=None)[0]
        resid = y - x @ beta
        tss = float(np.sum((y - y.mean()) ** 2))
        r2 = 1.0 - float(np.sum(resid ** 2)) / tss if tss > 0 else None
        fitted = path.dropna()
        return ({"coefficients": {"intercept": beta[0], "daily": beta[1], "weekly": beta[2], "monthly": beta[3]},
                 "r2": r2, "n_obs": len(sample), "forecast_variance": float(fitted.iloc[-1]),
                 "forecast_volatility": math.sqrt(max(float(fitted.iloc[-1]), 0.0)),
                 "fitted_index": [str(i) for i in fitted.index], "fitted": fitted.to_numpy()}, warns)

    return c.run(name, unit="return^2", method="har_rv_ols", params=params, piece_ids=pieces, compute=compute,
                 inputs=(realized_variance,) if isinstance(realized_variance, pd.Series) else ())


def _persistence_stats(persistence: float) -> dict:
    half = math.log(2.0) / -math.log(persistence) if 0.0 < persistence < 1.0 else None
    return {"persistence": persistence, "half_life": half}


def _garch_family(name: str, kind: str, pieces: tuple[str, ...], returns: Any, horizon: int, min_obs: int = 100) -> QuantResult:
    limits.check_param("horizon", horizon)
    limits.as_int("horizon", horizon)
    params = {"horizon": int(horizon), "distribution": "gaussian"}

    def compute() -> Any:
        if horizon < 1:
            raise c.Domain("horizon must be at least 1")
        series, warns = c.clean_series(returns, name="returns", min_obs=min_obs)
        if c.is_constant(series):
            raise c.Domain("constant series: dispersion is zero")
        values = series.to_numpy(dtype=float)
        if kind == "gjr":
            weights = np.asarray(_lib.ft_garch.get_gjr_garch_weights(values), dtype=float)
        else:
            weights = np.asarray(_lib.ft_garch.get_egarch_weights(values), dtype=float)
        if weights.shape != (4,) or not np.all(np.isfinite(weights)):
            raise c.Domain("the fit did not converge")
        omega, alpha, gamma, beta = (float(w) for w in weights)
        if kind == "gjr":
            cond = np.asarray(_lib.ft_garch.get_gjr_garch(values, weights, len(values)), dtype=float)
            forecast = np.asarray(_lib.ft_garch.get_gjr_garch_forecast(values, weights, time_steps=int(horizon)), dtype=float)
            persistence = alpha + gamma / 2.0 + beta
            long_run = omega / (1.0 - persistence) if persistence < 1.0 else None
            leverage = gamma > 0.0
        else:
            cond = np.asarray(_lib.ft_garch.get_egarch(values, weights, len(values)), dtype=float)
            forecast = np.asarray(_lib.ft_garch.get_egarch_forecast(values, weights, time_steps=int(horizon)), dtype=float)
            persistence = abs(beta)
            long_run = math.exp(omega / (1.0 - beta)) if abs(beta) < 1.0 else None
            leverage = gamma < 0.0
        z = values / np.sqrt(cond)
        q, p = c.ljung_box(z ** 2, lags=min(10, max(len(z) // 5, 2)), model_df=0)
        extra = ["gaussian likelihood: fat tailed returns bias the persistence upward; read the fit with the standardized residual check",
                 "intraday returns need the intraday seasonality removed before the fit (stats.intraday_seasonality)"]
        if p < 0.05:
            extra.append("standardized squared residuals still show autocorrelation (Ljung-Box p < 0.05): the model leaves volatility structure")
        out = {"omega": omega, "alpha": alpha, "gamma": gamma, "beta": beta, **_persistence_stats(persistence),
               "long_run_variance": long_run, "leverage_effect": bool(leverage),
               "forecast_variance": forecast, "forecast_volatility": np.sqrt(forecast),
               "standardized_residuals": {"mean": float(z.mean()), "std": float(z.std(ddof=1)), "ljung_box_squared_q": q, "ljung_box_squared_p": p}}
        return out, warns + tuple(extra)

    return c.run(name, unit="return^2", method=f"{kind}_garch_1_1" if kind == "gjr" else "egarch_1_1", params=params, piece_ids=pieces,
                 compute=compute, inputs=(returns,) if isinstance(returns, pd.Series) else ())


@registry.register("stats.gjr_garch", module="stats",
                   summary="GJR-GARCH(1,1,1): asymmetric volatility with the leverage term gamma; parameters, persistence, half life, standardized residual check and variance forecast.",
                   inputs={"returns": _RET, "horizon": {"type": "int", "required": False, "default": 10}}, piece_ids=("FT-061",))
def gjr_garch(returns: Any, horizon: int = 10) -> QuantResult:
    return _garch_family("stats.gjr_garch", "gjr", ("FT-061",), returns, horizon)


@registry.register("stats.egarch", module="stats",
                   summary="EGARCH(1,1) on the log variance: parameters, persistence, half life, standardized residual check and the variance forecast of the next periods.",
                   inputs={"returns": _RET, "horizon": {"type": "int", "required": False, "default": 10}}, piece_ids=("FT-062",))
def egarch(returns: Any, horizon: int = 10) -> QuantResult:
    return _garch_family("stats.egarch", "egarch", ("FT-062",), returns, horizon)

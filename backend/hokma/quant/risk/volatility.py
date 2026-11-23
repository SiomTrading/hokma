"""Volatility estimators: exponentially weighted, GARCH(1,1) and the range based Yang-Zhang estimator."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import helpers, registry
from hokma.quant.errors import BadInput
from hokma.quant.risk import _common as c
from hokma.quant.risk import _lib
from hokma.quant.types import QuantResult

_RET = {"type": "series", "required": True}


@registry.register("risk.ewma_volatility", module="risk",
                   summary="Exponentially weighted volatility (decay lambda, 0.94 by default); the latest per period value.",
                   inputs={"returns": _RET, "lambda_": {"type": "float", "required": False, "default": 0.94}}, piece_ids=("FT-056",))
def ewma_volatility(returns: Any, lambda_: float = 0.94) -> QuantResult:
    params = {"lambda_": lambda_}
    if isinstance(lambda_, bool) or not isinstance(lambda_, (int, float)):
        raise BadInput(f"lambda_ must be a number, got {type(lambda_).__name__}")
    if not 0.0 < lambda_ < 1.0:
        return c.empty("risk.ewma_volatility", "return", "ewma", "lambda_ must be strictly between 0 and 1", params=params, piece_ids=("FT-056",))
    return c.measure("risk.ewma_volatility", returns, unit="return", method="ewma", params=params, piece_ids=("FT-056",),
                     compute=lambda s: _lib.ft_risk_model.get_ewma_volatility(s, lambda_=lambda_).dropna().iloc[-1], min_obs=5)


@registry.register("risk.garch", module="risk",
                   summary="GARCH(1,1) fit (omega, alpha, beta, persistence, long run variance) and the variance and volatility forecast for the next periods.",
                   inputs={"returns": _RET, "horizon": {"type": "int", "required": False, "default": 10}}, piece_ids=("FT-060",))
def garch(returns: Any, horizon: int = 10) -> QuantResult:
    if isinstance(horizon, bool) or not isinstance(horizon, (int, np.integer)):
        raise BadInput(f"horizon must be an integer, got {type(horizon).__name__}")
    from hokma.quant import limits

    limits.check_param("horizon", horizon)
    params = {"horizon": int(horizon), "p": 1, "q": 1}

    def compute(s: pd.Series) -> dict:
        if horizon < 1:
            raise ValueError("horizon must be at least 1")
        values = s.to_numpy()
        omega, alpha, beta = (float(v) for v in _lib.ft_garch.get_garch_weights(values))
        forecast = np.asarray(_lib.ft_garch.get_garch_forecast(values, [omega, alpha, beta], time_steps=int(horizon)), dtype=float)
        persistence = alpha + beta
        return {"omega": omega, "alpha": alpha, "beta": beta, "persistence": persistence,
                "long_run_variance": omega / (1.0 - persistence) if persistence < 1.0 else None,
                "forecast_variance": forecast, "forecast_volatility": np.sqrt(forecast)}

    return c.measure("risk.garch", returns, unit="return^2", method="garch_1_1", params=params, piece_ids=("FT-060",),
                     compute=compute, min_obs=100)


@registry.register("risk.yang_zhang_volatility", module="risk",
                   summary="Yang-Zhang volatility per period from open, high, low and close bars; handles overnight gaps. Series need a datetime index.",
                   inputs={"open_": {"type": "series", "required": True}, "high": {"type": "series", "required": True},
                           "low": {"type": "series", "required": True}, "close": {"type": "series", "required": True},
                           "period": {"type": "str", "required": False, "default": "monthly"}},
                   piece_ids=("CC-06",))
def yang_zhang_volatility(open_: Any, high: Any, low: Any, close: Any, period: str = "monthly") -> QuantResult:
    name, unit, method, pieces = "risk.yang_zhang_volatility", "return", "yang_zhang", ("CC-06",)
    params = {"period": period}
    bars = [helpers.as_series(v, name=n) for v, n in ((open_, "open_"), (high, "high"), (low, "low"), (close, "close"))]
    if len({len(b) for b in bars}) != 1:
        raise BadInput("open_, high, low and close must have the same length")
    if not all(isinstance(b.index, pd.DatetimeIndex) for b in bars):
        raise BadInput("open_, high, low and close need a datetime index")
    frame = pd.concat(bars, axis=1, keys=["open", "high", "low", "close"]).replace([np.inf, -np.inf], np.nan)
    cleaned = frame.dropna()
    warns = (f"dropped {len(frame) - len(cleaned)} row(s) with missing values",) if len(cleaned) != len(frame) else ()
    if len(cleaned) < 10 or (cleaned <= 0).any().any():
        return c.empty(name, unit, method, warns + ("need 10 complete bars with positive prices",), params=params, piece_ids=pieces)
    if (cleaned["high"] < cleaned[["open", "close"]].max(axis=1)).any() or (cleaned["low"] > cleaned[["open", "close"]].min(axis=1)).any():
        return c.empty(name, unit, method, warns + ("bars are inconsistent: high below open or close, or low above them",), params=params, piece_ids=pieces)
    indexed = c.period_index(cleaned)
    try:
        path = _lib.ft_realized.get_yang_zhang_volatility(indexed["open"], indexed["high"], indexed["low"], indexed["close"], period).dropna()
    except Exception as exc:
        return c.empty(name, unit, method, warns + (f"{type(exc).__name__}: {exc}",), params=params, piece_ids=pieces)
    if path.empty:
        return c.empty(name, unit, method, warns + ("no complete period to estimate",), params=params, piece_ids=pieces)
    value = {"latest": path.iloc[-1], "index": [str(i) for i in path.index], "values": path.to_numpy()}
    return c.build(name, value, unit=unit, method=method, params=params, piece_ids=pieces, inputs=(cleaned,), as_of_source=cleaned["close"], warnings=warns)

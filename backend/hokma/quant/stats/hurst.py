"""Hurst exponent (FT-059): persistence or mean reversion of a series, for the regime context of a desk."""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import limits, registry
from hokma.quant.stats import _common as c
from hokma.quant.stats import _lib
from hokma.quant.types import QuantResult

METHODS = ("structure", "rescaled_range")
_SERIES = {"type": "series", "required": True}


NULL_SIMULATIONS = 200
NULL_SEED = 20_260_101


def structure_exponent(levels: np.ndarray, max_lag: int) -> float:
    """Slope of log(std of lagged differences) on log(lag) over the lags 2..max_lag; NaN when fewer than 2 lags are usable."""
    lags = np.arange(2, min(max_lag, len(levels) - 2) + 1)
    sds = np.array([np.std(levels[lag:] - levels[:-lag]) for lag in lags])
    ok = np.isfinite(sds) & (sds > 0)
    if ok.sum() < 2:
        return float("nan")
    return float(np.polyfit(np.log(lags[ok]), np.log(sds[ok]), 1)[0])


def rescaled_range_exponent(returns: np.ndarray, min_window: int = 8) -> float:
    """Hurst exponent from rescaled range analysis of a returns series (non overlapping windows, sizes doubling)."""
    n = len(returns)
    sizes = []
    size = min_window
    while size <= n // 2:
        sizes.append(size)
        size *= 2
    if len(sizes) < 3:
        raise c.Domain(f"rescaled range needs at least {min_window * 8} observations")
    log_n, log_rs = [], []
    for size in sizes:
        values = []
        for start in range(0, n - size + 1, size):
            chunk = returns[start:start + size]
            dev = np.cumsum(chunk - chunk.mean())
            spread = float(dev.max() - dev.min())
            s = float(chunk.std())
            if s > 0 and spread > 0:
                values.append(spread / s)
        if values:
            log_n.append(math.log(size))
            log_rs.append(math.log(float(np.mean(values))))
    if len(log_n) < 3:
        raise c.Domain("not enough usable windows for rescaled range")
    return float(np.polyfit(np.array(log_n), np.array(log_rs), 1)[0])


@registry.register("stats.hurst", module="stats",
                   summary="Hurst exponent of a series of levels (log prices) or returns: below 0.5 mean reverting, above 0.5 persistent. method structure (scaling of lagged differences) or rescaled_range.",
                   inputs={"series": _SERIES, "max_lag": {"type": "int", "required": False, "default": 20},
                           "method": {"type": "str", "required": False, "default": "structure"},
                           "input_kind": {"type": "str", "required": False, "default": "levels"}}, piece_ids=("FT-059",))
def hurst(series: Any, max_lag: int = 20, method: str = "structure", input_kind: str = "levels") -> QuantResult:
    name, pieces = "stats.hurst", ("FT-059",)
    limits.check_param("max_lag", max_lag)
    limits.as_int("max_lag", max_lag)
    params = {"max_lag": int(max_lag), "method": method, "input_kind": input_kind}

    def compute() -> Any:
        if method not in METHODS:
            raise c.Domain(f"method must be one of {list(METHODS)}")
        if input_kind not in ("levels", "returns"):
            raise c.Domain("input_kind must be levels or returns")
        if max_lag < 3:
            raise c.Domain("max_lag must be at least 3")
        values, warns = c.clean_series(series, name="series", min_obs=max(max_lag + 3, 30))
        if c.is_constant(values):
            raise c.Domain("constant series: the exponent is undefined")
        levels = values if input_kind == "levels" else pd.Series(np.cumsum(values.to_numpy()))
        extra = []
        if input_kind == "levels" and float(levels.min()) > 0 and abs(float(levels.mean())) > 5 * float(levels.std()):
            extra.append("levels far from zero: use log prices, the exponent of raw prices depends on the price level")
        arr = levels.to_numpy(dtype=float)
        rng = np.random.default_rng(NULL_SEED)
        null = []
        for _ in range(NULL_SIMULATIONS):
            steps = rng.standard_normal(len(arr) - 1 if input_kind == "levels" else len(values))
            if method == "structure":
                null.append(structure_exponent(np.concatenate([[0.0], np.cumsum(steps)]), int(max_lag)))
            else:
                null.append(rescaled_range_exponent(steps))
        if method == "structure":
            h = float(_lib.ft_risk_model.get_hurst_exponent(pd.Series(arr), max_lag=int(max_lag)))
        else:
            rets = np.diff(arr) if input_kind == "levels" else values.to_numpy(dtype=float)
            h = rescaled_range_exponent(rets)
            extra.append("rescaled range is biased upward in short samples: compare with method structure")
        if not math.isfinite(h):
            raise c.Domain("the exponent could not be estimated")
        lo, hi = (float(v) for v in np.nanquantile(null, [0.025, 0.975]))
        reading = "persistent" if h > hi else "mean_reverting" if h < lo else "indistinguishable_from_random_walk"
        extra.append("the reading compares the exponent with the 95% interval of the same estimator on simulated random walks of the same length; calibrate any trading band in backtest")
        return {"hurst": h, "null_interval_95": [lo, hi], "null_std": float(np.nanstd(null, ddof=1)), "reading": reading, "n_obs": len(arr)}, tuple(warns) + tuple(extra)

    return c.run(name, unit="ratio", method=f"hurst_{method}", params=params, piece_ids=pieces, compute=compute,
                 inputs=(series,) if isinstance(series, pd.Series) else ())

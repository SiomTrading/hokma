"""The only file of the metrics module that imports quantitative or data libraries (SPEC-017, rule 4).

Every other file reaches those libraries through this one. A scan test fails if any other file imports them.

This file is also the compatibility layer of the metrics module (QS-061): aliases for pandas frequency codes,
period-index conversion, warning control and version information live here, so a change in a dependency is fixed
in one place.
"""
from __future__ import annotations

import contextlib
import warnings
from typing import Any, Iterator

import numpy as np
import pandas as pd

import ffn
import quantstats.stats as qstats
import quantstats.utils as qutils
from financetoolkit.performance import performance_model as perf_model
from financetoolkit.risk import risk_model
from scipy import stats as scipy_stats
from skfolio.preprocessing import prices_to_returns as _sk_prices_to_returns


# Frequency codes that changed names between pandas releases: one table, used everywhere.
RESAMPLE_ALIASES: dict[str, str] = {
    "day": "D", "week": "W-MON", "month": "ME", "quarter": "QE", "year": "YE",
    "daily": "D", "weekly": "W-MON", "monthly": "ME", "quarterly": "QE", "yearly": "YE",
    "D": "D", "W": "W-MON", "M": "ME", "ME": "ME", "Q": "QE", "QE": "QE", "A": "YE", "Y": "YE", "YE": "YE",
}

# Period labels the period-based risk functions accept.
PERIOD_LABELS: dict[str, str] = {"weekly": "W", "monthly": "M", "quarterly": "Q", "yearly": "Y"}


@contextlib.contextmanager
def quiet() -> Iterator[None]:
    """Run library code without its deprecation and runtime warnings: results carry their own warnings."""
    with warnings.catch_warnings(), np.errstate(all="ignore"):
        warnings.simplefilter("ignore")
        yield


def resample_alias(label: str) -> str:
    """Pandas frequency code for a human label such as 'month' or 'yearly'. Unknown labels raise ValueError."""
    try:
        return RESAMPLE_ALIASES[label]
    except KeyError as exc:
        raise ValueError(f"unknown period {label!r}; use one of {sorted(set(RESAMPLE_ALIASES))}") from exc


def to_period_index(series: pd.Series | pd.DataFrame, freq: str = "D") -> pd.Series | pd.DataFrame:
    """Copy of the series indexed by periods, the index type the period-based risk functions require."""
    out = series.copy()
    out.index = pd.DatetimeIndex(out.index).to_period(freq)
    return out


def versions() -> dict[str, str]:
    """Versions of the numerical stack in use."""
    from importlib import metadata
    names = ("numpy", "pandas", "scipy", "skfolio", "ffn", "quantstats", "financetoolkit")
    return {name: metadata.version(name) for name in names}


def normal_ppf(probability: float, mean: float = 0.0, std: float = 1.0) -> float:
    """Inverse of the normal distribution function."""
    return float(scipy_stats.norm.ppf(probability, loc=mean, scale=std))


def normal_pdf(x: float) -> float:
    """Density of the standard normal distribution."""
    return float(scipy_stats.norm.pdf(x))


def prices_to_returns(prices: pd.Series | pd.DataFrame, *, log: bool = False) -> pd.Series | pd.DataFrame:
    """Prices to simple or log returns, dropping the first (undefined) observation."""
    frame = prices.to_frame(name=prices.name if prices.name is not None else "price") if isinstance(prices, pd.Series) else prices
    out = _sk_prices_to_returns(frame, log_returns=log, drop_inceptions_nan=False, fill_nan=False)
    return out.iloc[:, 0].rename(prices.name) if isinstance(prices, pd.Series) else out


def reference_probabilistic_sharpe(returns: pd.Series, *, rf: float = 0.0, periods: int = 252) -> float:
    """Value of the library probabilistic Sharpe, kept only so tests can show it differs from the correct one.

    The module never calls this function: the metrics that use it are blocked in the registry.
    """
    with quiet():
        return float(qstats.probabilistic_sharpe_ratio(returns, rf=rf, periods=periods, annualize=False))


# ---------------------------------------------------------------------------------------------------------------
# Handles used by the benchmark, summary, trades, tail view, montecarlo and attribution files (metrics part B).
# Imports are lazy so that loading the module stays cheap.
# ---------------------------------------------------------------------------------------------------------------
def returns_stats():
    """Module with the period-return statistics (Sharpe family, drawdown, trade statistics, greeks)."""
    import quantstats.stats as _stats
    return _stats


def performance_model():
    """Module with the benchmark-relative performance functions (beta, alpha, capture, timing regressions)."""
    import financetoolkit.performance.performance_model as _model
    return _model


def price_stats():
    """Module with the price-series statistics classes and functions."""
    import ffn as _ffn
    return _ffn


def attribution_lib():
    """Module with the factor attribution functions and containers."""
    import skfolio.attribution as _attribution
    return _attribution

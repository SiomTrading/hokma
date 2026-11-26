"""Adapter to the statistics modules of H3 (SPEC-019), loaded only when a study needs them.

Each adapter has the small signature that a study expects (numbers in, a number or an array out) and calls the registered H3 function that
does the work; none of them computes a statistic. A study takes an injected callable instead when the caller has one (that is the seam of
the tests). A missing module is a stable `h3_unavailable` error that names the function: no value is invented in its place.
"""
from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

from hokma.quant.types import QuantResult


class H3Unavailable(RuntimeError):
    """The H3 function a study needs is not installed. `code` is stable: h3_unavailable."""

    code = "h3_unavailable"


ALLOWED_PREFIX = "hokma.quant."      # the only place a study may load from: never an execution, broker or channel module


def resolve(module: str, name: str) -> Callable[..., Any]:
    """The function `name` of the module, or H3Unavailable naming both."""
    if not module.startswith(ALLOWED_PREFIX):
        raise H3Unavailable(f"h3_unavailable: {module} is not a quant module")
    try:
        target = importlib.import_module(module)
    except ImportError:
        raise H3Unavailable(f"h3_unavailable: {module}.{name} is not installed") from None
    fn = getattr(target, name, None)
    if not callable(fn):
        raise H3Unavailable(f"h3_unavailable: {module} has no function {name}")
    return fn


def scalar(result: Any) -> float | None:
    """The number of a QuantResult or a plain number, or None when it is missing or not finite."""
    value = result.value if isinstance(result, QuantResult) else result
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number and abs(number) != float("inf") else None


# ---- the adapters: the real functions of H3 behind the signatures that the studies use ----

IC_MODULE, IC_FUNCTION = "hokma.quant.portfolio.ic", "cs_spearman_correlation"
EVENT_MODULE, EVENT_FUNCTION = "hokma.quant.stats.events", "event_stats"
COINT_MODULE, COINT_FUNCTION = "hokma.quant.stats.cointegration", "engle_granger"
HURST_MODULE, HURST_FUNCTION = "hokma.quant.stats.hurst", "hurst"
KALMAN_MODULE, KALMAN_FUNCTION = "hokma.quant.stats.kalman", "kalman_filter"
HALF_LIFE_MODULE, HALF_LIFE_FUNCTION = "hokma.quant.stats.timeseries", "half_life"
# Where the Engle-Granger statistic falls, as the upper bound of its p-value: the band edge above it. H3 gives the band, never a number.
P_UPPER_BOUND = {"<0.01": 0.01, "0.01-0.05": 0.05, "0.05-0.10": 0.10, ">0.10": 1.0}


def _field(result: Any, key: str) -> Any:
    """The entry `key` of the value of a QuantResult, or None when the function could not compute (the reason stays in its warnings)."""
    value = result.value if isinstance(result, QuantResult) else result
    return value.get(key) if isinstance(value, dict) else None


def information_coefficient(signal: Any, target: Any) -> float | None:
    """The rank information coefficient of one cross section: the Spearman correlation of H3 between the signal and the target."""
    return scalar(_field(resolve(IC_MODULE, IC_FUNCTION)(list(map(float, signal)), list(map(float, target))), "mean"))


def event_stats(values: Any, seed: int = 0) -> QuantResult:
    """The statistics of event returns (t statistic, seeded bootstrap interval, share positive) from H3, as the result it gives."""
    return resolve(EVENT_MODULE, EVENT_FUNCTION)([float(v) for v in values], seed=int(seed))


def engle_granger_p(log_a: Any, log_b: Any) -> float | None:
    """The upper bound of the Engle-Granger p-value of two log price series, from the band that H3 reports for the residual statistic.

    H3 gives the band of the p-value (below 0.01, 0.01 to 0.05, 0.05 to 0.10, above 0.10) and not a number. The edge above the band is a bound
    that is never smaller than the true p-value, so a pair passes a correction only when even this bound does."""
    out = resolve(COINT_MODULE, COINT_FUNCTION)(list(map(float, log_a)), list(map(float, log_b)), ratio_basis="log")
    return P_UPPER_BOUND.get(_field(out, "p_value_band"))


def hurst_exponent(spread: Any) -> float | None:
    """The Hurst exponent of a spread (a series of levels) from H3."""
    return scalar(_field(resolve(HURST_MODULE, HURST_FUNCTION)(list(map(float, spread)), input_kind="levels"), "hurst"))


def dynamic_hedge_ratio(log_a: Any, log_b: Any) -> Any:
    """The causal hedge ratio of log A on log B at each row: the filtered beta of the Kalman regression of H3, never the smoother.

    The noise variances are calibrated by H3 on the sample it is given: a run that uses these betas inside a test window must pass only the
    rows before the window to calibrate (the warnings of H3 say so)."""
    beta = _field(resolve(KALMAN_MODULE, KALMAN_FUNCTION)(list(map(float, log_a)), list(map(float, log_b)), model="regression"), "beta")
    if beta is None:
        raise H3Unavailable("h3_unavailable: the Kalman filter of H3 could not compute the hedge ratio for these rows")
    return beta


def half_life(values: Any) -> float | None:
    """The mean reversion half life, in bars, of a series from H3; None when it does not revert."""
    return scalar(_field(resolve(HALF_LIFE_MODULE, HALF_LIFE_FUNCTION)(list(map(float, values))), "half_life"))

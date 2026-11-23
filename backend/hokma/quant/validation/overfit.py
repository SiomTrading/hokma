"""Overfitting controls: probabilistic Sharpe, deflated Sharpe, chance hurdle, PBO and autocorrelation penalty.

SPEC-017 rows FM-01 to FM-05, FF-010 to FF-012, FT-067, CC-04.

Conventions fixed here and used by every caller:
  * Sharpe ratios are per period (mean over sample standard deviation, ddof=1), never annualized.
  * Skewness is the sample skewness and kurtosis is the RAW sample kurtosis (a normal distribution gives 3).
  * The probabilistic Sharpe lives once, in `psr`. The metrics module delegates to it.
  * PBO lives once, in `pbo`. No other function in the registry computes it.
"""
from __future__ import annotations

import math
from statistics import NormalDist
from typing import Any

import numpy as np
import pandas as pd

from hokma.quant import helpers, registry
from hokma.quant.errors import BadInput
from hokma.quant.types import QuantResult
from hokma.quant.validation import _lib
from hokma.quant.validation._checks import clean_returns

_NORMAL = NormalDist()
EULER_GAMMA = 0.5772156649015329


# --------------------------------------------------------------------------- pure formulas

def sharpe_moments(returns: pd.Series) -> tuple[float, float, float, int]:
    """Per period Sharpe, sample skewness, raw kurtosis and observation count of a clean series."""
    sr = float(returns.mean() / returns.std(ddof=1))
    skew = float(returns.skew())
    kurt_raw = float(returns.kurt()) + 3.0
    return sr, skew, kurt_raw, int(len(returns))


def psr_from_moments(sr: float, sr_benchmark: float, skew: float, kurtosis_raw: float, n: int) -> float | None:
    """Probability that the true Sharpe ratio exceeds `sr_benchmark`.

    PSR = Phi((sr - sr*) * sqrt(n - 1) / sqrt(1 - skew * sr + (kurtosis_raw - 1) / 4 * sr^2)).
    Returns None when the variance term is not positive or n < 2.
    """
    if n < 2:
        return None
    adjustment = 1.0 - skew * sr + (kurtosis_raw - 1.0) / 4.0 * sr * sr
    if not adjustment > 0.0:
        return None
    return _NORMAL.cdf((sr - sr_benchmark) * math.sqrt(n - 1) / math.sqrt(adjustment))


def expected_max_sharpe(n_trials: int, sr_std: float) -> float:
    """Expected maximum Sharpe ratio of `n_trials` skill-less strategies (the chance hurdle).

    Same units as `sr_std`. One trial, or no dispersion, gives 0.
    """
    if n_trials < 1:
        raise ValueError("n_trials must be at least 1")
    if n_trials == 1 or not sr_std > 0.0:
        return 0.0
    return sr_std * (
        (1.0 - EULER_GAMMA) * _NORMAL.inv_cdf(1.0 - 1.0 / n_trials)
        + EULER_GAMMA * _NORMAL.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    )


def autocorr_penalty_from_rho(rhos: list[float] | np.ndarray) -> float | None:
    """sqrt(1 + 2 * sum_k (1 - k / (q + 1)) * rho_k) for lags 1..q. None when the radicand is not positive."""
    rho = np.asarray(rhos, dtype=float)
    q = len(rho)
    weights = 1.0 - np.arange(1, q + 1) / (q + 1.0)
    radicand = 1.0 + 2.0 * float(np.sum(weights * rho))
    return math.sqrt(radicand) if radicand > 0.0 else None


def sample_autocorrelations(values: np.ndarray, lags: int) -> np.ndarray:
    x = np.asarray(values, dtype=float)
    x = x - x.mean()
    denom = float(np.dot(x, x))
    return np.array([float(np.dot(x[:-k], x[k:])) / denom for k in range(1, lags + 1)])


# --------------------------------------------------------------------------- helpers

def _prepare(returns: Any, name: str, *, min_obs: int = 3) -> tuple[pd.Series, tuple[str, ...], bool]:
    return clean_returns(returns, min_obs=min_obs)


_RETURNS_INPUT = {"type": "series", "required": True}


# --------------------------------------------------------------------------- registered functions

@registry.register(
    "validation.psr", module="validation",
    summary="Probabilistic Sharpe ratio with raw (non annualized) kurtosis: P(true Sharpe > sr_benchmark). Per period units.",
    inputs={"returns": _RETURNS_INPUT, "sr_benchmark": {"type": "float", "required": False, "default": 0.0}},
    piece_ids=("FM-01", "FT-067"),
)
def psr(returns: Any, sr_benchmark: float = 0.0) -> QuantResult:
    name = "validation.psr"
    series, warnings, degenerate = _prepare(returns, name)
    if degenerate:
        return QuantResult.empty(name, "prob", "psr_raw_kurtosis", warnings[-1])
    sr, skew, kurt, n = sharpe_moments(series)
    value = psr_from_moments(sr, float(sr_benchmark), skew, kurt, n)
    if value is None:
        return QuantResult.empty(name, "prob", "psr_raw_kurtosis", "variance of the Sharpe estimator is not positive")
    return QuantResult(
        name=name, value=value, unit="prob", method="psr_raw_kurtosis",
        params={"sr": sr, "sr_benchmark": float(sr_benchmark), "skew": skew, "kurtosis_raw": kurt, "n": n},
        inputs_hash=helpers.hash_inputs(series, float(sr_benchmark)), as_of=helpers.last_as_of(series),
        warnings=warnings, piece_ids=("FM-01", "FT-067"),
    )


@registry.register(
    "validation.psr_moments", module="validation",
    summary="Probabilistic Sharpe ratio from stated moments: per period sharpe, skewness, raw kurtosis and n.",
    inputs={"sr": {"type": "float", "required": True}, "sr_benchmark": {"type": "float", "required": False, "default": 0.0},
            "skew": {"type": "float", "required": True}, "kurtosis_raw": {"type": "float", "required": True},
            "n": {"type": "int", "required": True}},
    piece_ids=("FM-01",),
)
def psr_moments(sr: float, skew: float, kurtosis_raw: float, n: int, sr_benchmark: float = 0.0) -> QuantResult:
    name = "validation.psr_moments"
    value = psr_from_moments(float(sr), float(sr_benchmark), float(skew), float(kurtosis_raw), int(n))
    if value is None:
        return QuantResult.empty(name, "prob", "psr_raw_kurtosis", "variance of the Sharpe estimator is not positive or n < 2")
    return QuantResult(name=name, value=value, unit="prob", method="psr_raw_kurtosis",
                       params={"sr": sr, "sr_benchmark": sr_benchmark, "skew": skew, "kurtosis_raw": kurtosis_raw, "n": n},
                       inputs_hash=helpers.hash_inputs([sr, sr_benchmark, skew, kurtosis_raw, n]), piece_ids=("FM-01",))


@registry.register(
    "validation.expected_max_sharpe", module="validation",
    summary="Chance hurdle: expected maximum Sharpe ratio of n_trials skill-less strategies, in the units of sr_std.",
    inputs={"n_trials": {"type": "int", "required": True}, "sr_std": {"type": "float", "required": True}},
    piece_ids=("FM-02", "FF-010"),
)
def expected_max_sharpe_result(n_trials: int, sr_std: float) -> QuantResult:
    name = "validation.expected_max_sharpe"
    if n_trials < 1 or not math.isfinite(float(sr_std)) or sr_std < 0:
        return QuantResult.empty(name, "ratio", "extreme_value_normal", "n_trials must be >= 1 and sr_std a finite number >= 0")
    return QuantResult(name=name, value=expected_max_sharpe(int(n_trials), float(sr_std)), unit="ratio", method="extreme_value_normal",
                       params={"n_trials": int(n_trials), "sr_std": float(sr_std)},
                       inputs_hash=helpers.hash_inputs([int(n_trials), float(sr_std)]), piece_ids=("FM-02", "FF-010"))


@registry.register(
    "validation.dsr", module="validation",
    summary="Deflated Sharpe ratio: PSR evaluated at the chance hurdle of n_trials trials. sr_std is the per period std of trial Sharpes.",
    inputs={"returns": _RETURNS_INPUT, "n_trials": {"type": "int", "required": True}, "sr_std": {"type": "float", "required": True}},
    piece_ids=("FM-03", "FF-011", "FT-067"),
)
def dsr(returns: Any, n_trials: int, sr_std: float) -> QuantResult:
    name = "validation.dsr"
    series, warnings, degenerate = _prepare(returns, name)
    if degenerate:
        return QuantResult.empty(name, "prob", "deflated_sharpe", warnings[-1])
    if n_trials < 1 or not math.isfinite(float(sr_std)) or sr_std < 0:
        return QuantResult.empty(name, "prob", "deflated_sharpe", "n_trials must be >= 1 and sr_std a finite number >= 0")
    sr, skew, kurt, n = sharpe_moments(series)
    sr0 = expected_max_sharpe(int(n_trials), float(sr_std))
    value = psr_from_moments(sr, sr0, skew, kurt, n)
    if value is None:
        return QuantResult.empty(name, "prob", "deflated_sharpe", "variance of the Sharpe estimator is not positive")
    return QuantResult(
        name=name, value=value, unit="prob", method="deflated_sharpe",
        params={"sr": sr, "sr0": sr0, "n_trials": int(n_trials), "sr_std": float(sr_std), "skew": skew, "kurtosis_raw": kurt, "n": n},
        inputs_hash=helpers.hash_inputs(series, int(n_trials), float(sr_std)), as_of=helpers.last_as_of(series),
        warnings=warnings, piece_ids=("FM-03", "FF-011", "FT-067"),
    )


@registry.register(
    "validation.dsr_crosscheck", module="validation",
    summary="DSR from three independent implementations (own and two external references) on the same input, with the largest gap.",
    inputs={"returns": _RETURNS_INPUT, "n_trials": {"type": "int", "required": True}, "sr_std": {"type": "float", "required": True}},
    piece_ids=("CC-04", "FF-011", "FT-067"),
)
def dsr_crosscheck(returns: Any, n_trials: int, sr_std: float) -> QuantResult:
    name = "validation.dsr_crosscheck"
    own = dsr(returns, n_trials, sr_std)
    if own.value is None:
        return QuantResult.empty(name, "prob", "dsr_three_sources", own.warnings[-1] if own.warnings else "not computable")
    series, _, _ = _prepare(returns, name)
    sr, skew, kurt, n = sharpe_moments(series)
    trials = _trial_sharpes(int(n_trials), float(sr_std))
    ffn_value = float(_lib.ffn_deflated_sharpe_ratio(series, trials, rf=0.0, nperiods=252, annualized_trials=False))
    ft_value = float(_lib.ft_deflated_sharpe_ratio(sr, float(sr_std) ** 2, int(n_trials), n, skew, kurt))
    values = {"own": float(own.value), "reference_a": ffn_value, "reference_b": ft_value}
    finite = [v for v in values.values() if math.isfinite(v)]
    gap = max(finite) - min(finite) if finite else None
    return QuantResult(name=name, value={**values, "max_abs_gap": gap}, unit="prob", method="dsr_three_sources",
                       params=dict(own.params), inputs_hash=own.inputs_hash, as_of=own.as_of, warnings=own.warnings,
                       piece_ids=("CC-04", "FF-011", "FT-067"))


def _trial_sharpes(n_trials: int, sr_std: float) -> np.ndarray:
    """A deterministic set of n_trials per period Sharpe values with mean 0 and sample std exactly sr_std."""
    if n_trials < 2:
        return np.zeros(max(n_trials, 1))
    z = np.linspace(-1.0, 1.0, n_trials)
    z = (z - z.mean()) / z.std(ddof=1)
    return z * sr_std


@registry.register(
    "validation.pbo", module="validation",
    summary="Probability of backtest overfitting by combinatorially symmetric cross validation over a trials matrix (one column per trial).",
    inputs={"matrix": {"type": "dataframe", "required": True}, "n_blocks": {"type": "int", "required": False, "default": 16}},
    piece_ids=("FM-04", "FF-012", "CC-04"),
)
def pbo(matrix: pd.DataFrame, n_blocks: int = 16) -> QuantResult:
    name = "validation.pbo"
    if not isinstance(matrix, pd.DataFrame):
        raise BadInput("matrix must be a DataFrame with one column per trial")
    warnings: list[str] = []
    frame = matrix.astype(float).replace([np.inf, -np.inf], np.nan)
    complete = frame.dropna(how="any")
    if len(complete) < len(frame):
        warnings.append(f"dropped {len(frame) - len(complete)} row(s) with NaN or infinite values")
    from hokma.quant import limits

    limits.check_param("n_blocks", n_blocks)
    if n_blocks < 2 or n_blocks % 2:
        return QuantResult.empty(name, "prob", "cscv", "n_blocks must be an even integer >= 2")
    if complete.shape[1] < 2 or len(complete) < n_blocks:
        return QuantResult.empty(name, "prob", "cscv", f"need at least 2 trials and {n_blocks} observations, got {complete.shape[1]} and {len(complete)}")
    out = _lib.ffn_prob_backtest_overfitting(complete, n_blocks=int(n_blocks), full_output=True)
    value = out["pbo"]
    if not math.isfinite(value):
        return QuantResult.empty(name, "prob", "cscv", "a trial has constant or undefined performance in some block combination")
    return QuantResult(name=name, value=float(value), unit="prob", method="cscv",
                       params={"n_blocks": int(n_blocks), "n_trials": int(complete.shape[1]), "n_combinations": int(len(out["logits"])),
                               "mean_oos_rank": float(out["mean_oos_rank"])},
                       inputs_hash=helpers.hash_inputs(complete, int(n_blocks)), as_of=helpers.last_as_of(complete.iloc[:, 0]),
                       warnings=tuple(warnings), piece_ids=("FM-04", "FF-012", "CC-04"))


@registry.register(
    "validation.autocorr_penalty", module="validation",
    summary="Sharpe standard error inflation for autocorrelated returns: sqrt(1 + 2 * Bartlett weighted sum of autocorrelations).",
    inputs={"returns": _RETURNS_INPUT, "lags": {"type": "int", "required": False, "default": None}},
    piece_ids=("FM-05",),
)
def autocorr_penalty(returns: Any, lags: int | None = None) -> QuantResult:
    name = "validation.autocorr_penalty"
    series, warnings, degenerate = _prepare(returns, name, min_obs=5)
    if degenerate:
        return QuantResult.empty(name, "ratio", "bartlett_autocorrelation", warnings[-1])
    q = int(lags) if lags is not None else min(10, max(1, len(series) // 5))
    if q < 1 or q >= len(series):
        return QuantResult.empty(name, "ratio", "bartlett_autocorrelation", "lags must be between 1 and n - 1")
    rho = sample_autocorrelations(series.to_numpy(), q)
    value = autocorr_penalty_from_rho(rho)
    if value is None:
        return QuantResult.empty(name, "ratio", "bartlett_autocorrelation", "radicand is not positive")
    return QuantResult(name=name, value=value, unit="ratio", method="bartlett_autocorrelation",
                       params={"lags": q, "rho": [float(r) for r in rho]},
                       inputs_hash=helpers.hash_inputs(series, q), as_of=helpers.last_as_of(series),
                       warnings=warnings, piece_ids=("FM-05",))

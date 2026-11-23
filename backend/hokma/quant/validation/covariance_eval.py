"""Calibration of covariance forecasts (SPEC-017 rows SK-042, SK-043, SK-044).

Three registered functions share one evaluation runner:
  * cov_forecast_online: distribution of the per step calibration ratios (online updates).
  * cov_forecast_compare: several half-lives evaluated in batch and compared.
  * cov_calibration: the headline metrics (calibration ratios, bias statistic, QLIKE, exceedance rates).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from hokma.quant import helpers, registry
from hokma.quant.errors import BadInput
from hokma.quant.types import QuantResult
from hokma.quant.validation import _lib

online_covariance_forecast_evaluation = _lib.online_covariance_forecast_evaluation
CovarianceForecastEvaluation = _lib.CovarianceForecastEvaluation
CovarianceForecastComparison = _lib.CovarianceForecastComparison


def _frame(matrix, min_rows: int):
    if not isinstance(matrix, pd.DataFrame):
        raise BadInput("matrix must be a DataFrame of returns, one column per asset")
    frame = matrix.astype(float).replace([np.inf, -np.inf], np.nan).dropna(how="any")
    if len(frame) < min_rows or frame.shape[1] < 1:
        return None, f"need at least {min_rows} observations and 1 asset, got {len(frame)} and {frame.shape[1]}"
    if ((frame.max() - frame.min()) == 0).any():
        return None, "constant column: dispersion is zero"
    return frame, ""


def online_evaluation(frame: pd.DataFrame, half_life: float, warmup_size: int, test_size: int, purge: int = 0):
    return _lib.online_covariance_forecast_evaluation(_lib.EWCovariance(half_life=half_life), frame, warmup_size=int(warmup_size),
                                                      test_size=int(test_size), purged_size=int(purge))


def calibration_metrics(evaluation) -> dict:
    """Headline calibration numbers of an evaluation (targets: ratios 1.0, bias statistic 1.0; QLIKE lower is better)."""
    exceed = evaluation.exceedance_summary()
    return {
        "mahalanobis_ratio": float(np.nanmean(evaluation.mahalanobis_calibration_ratio)),
        "diagonal_ratio": float(np.nanmean(evaluation.diagonal_calibration_ratio)),
        "bias_statistic": float(np.nanmean(evaluation.bias_statistic)),
        "qlike": float(np.nanmean(evaluation.portfolio_variance_qlike_loss)),
        "exceedance_rate": {str(level): float(row["observed_rate"]) for level, row in exceed.iterrows()},
        "n_steps": int(len(evaluation.observations)),
    }


def _distribution(values: np.ndarray) -> dict:
    v = np.asarray(values, dtype=float)
    v = v[np.isfinite(v)]
    return {"mean": float(v.mean()), "p05": float(np.quantile(v, 0.05)), "p50": float(np.quantile(v, 0.5)), "p95": float(np.quantile(v, 0.95))}


_COMMON = {"matrix": {"type": "dataframe", "required": True}, "half_life": {"type": "float", "required": False, "default": 30.0},
           "warmup_size": {"type": "int", "required": False, "default": 252}, "test_size": {"type": "int", "required": False, "default": 1}}


@registry.register(
    "validation.cov_forecast_online", module="validation",
    summary="Online evaluation of an exponentially weighted covariance forecast: distribution of the per step calibration ratios.",
    inputs=dict(_COMMON, purge={"type": "int", "required": False, "default": 0}), piece_ids=("SK-042",),
)
def cov_forecast_online(matrix: pd.DataFrame, half_life: float = 30.0, warmup_size: int = 252, test_size: int = 1, purge: int = 0) -> QuantResult:
    name = "validation.cov_forecast_online"
    frame, why = _frame(matrix, warmup_size + test_size + purge + 1)
    if frame is None:
        return QuantResult.empty(name, "ratio", "online_covariance_evaluation", why)
    ev = online_evaluation(frame, half_life, warmup_size, test_size, purge)
    value = {"n_steps": int(len(ev.observations)), "horizon": int(ev.horizon),
             "mahalanobis_ratio": _distribution(ev.mahalanobis_calibration_ratio), "diagonal_ratio": _distribution(ev.diagonal_calibration_ratio)}
    return QuantResult(name=name, value=value, unit="ratio", method="online_covariance_evaluation",
                       params={"half_life": float(half_life), "warmup_size": int(warmup_size), "test_size": int(test_size), "purge": int(purge)},
                       inputs_hash=helpers.hash_inputs(frame, half_life, warmup_size, test_size, purge), as_of=helpers.last_as_of(frame.iloc[:, 0]),
                       piece_ids=("SK-042",))


@registry.register(
    "validation.cov_forecast_compare", module="validation",
    summary="Batch evaluation of several covariance half-lives (refit on every window) and their comparison by QLIKE.",
    inputs={"matrix": {"type": "dataframe", "required": True}, "half_lives": {"type": "list", "required": True},
            "train_size": {"type": "int", "required": False, "default": 252}, "test_size": {"type": "int", "required": False, "default": 1}},
    piece_ids=("SK-043",),
)
def cov_forecast_compare(matrix: pd.DataFrame, half_lives: list[float], train_size: int = 252, test_size: int = 1) -> QuantResult:
    name = "validation.cov_forecast_compare"
    if not isinstance(half_lives, (list, tuple)) or len(half_lives) < 2:
        raise BadInput("half_lives must be a list with at least two values")
    frame, why = _frame(matrix, train_size + test_size + 1)
    if frame is None:
        return QuantResult.empty(name, "ratio", "covariance_comparison", why)
    evaluations = [_lib.covariance_forecast_evaluation(_lib.EWCovariance(half_life=h), frame, train_size=int(train_size), test_size=int(test_size))
                   for h in half_lives]
    comparison = _lib.CovarianceForecastComparison(evaluations, names=[f"half_life_{h}" for h in half_lives])
    metrics = {str(h): calibration_metrics(ev) for h, ev in zip(half_lives, evaluations)}
    best = min(metrics, key=lambda h: metrics[h]["qlike"])
    value = {"models": metrics, "best_by_qlike": float(best), "n_models": len(evaluations),
             "comparison_rows": int(len(comparison.summary()))}
    return QuantResult(name=name, value=value, unit="ratio", method="covariance_comparison",
                       params={"half_lives": [float(h) for h in half_lives], "train_size": int(train_size), "test_size": int(test_size)},
                       inputs_hash=helpers.hash_inputs(frame, list(half_lives), train_size, test_size), as_of=helpers.last_as_of(frame.iloc[:, 0]),
                       piece_ids=("SK-043",))


@registry.register(
    "validation.cov_calibration", module="validation",
    summary="Does the risk model get it right: calibration ratios, bias statistic, QLIKE and exceedance rates of the covariance forecast.",
    inputs=dict(_COMMON, purge={"type": "int", "required": False, "default": 0}), piece_ids=("SK-044",),
)
def cov_calibration(matrix: pd.DataFrame, half_life: float = 30.0, warmup_size: int = 252, test_size: int = 1, purge: int = 0) -> QuantResult:
    name = "validation.cov_calibration"
    frame, why = _frame(matrix, warmup_size + test_size + purge + 1)
    if frame is None:
        return QuantResult.empty(name, "ratio", "covariance_calibration", why)
    metrics = calibration_metrics(online_evaluation(frame, half_life, warmup_size, test_size, purge))
    return QuantResult(name=name, value=metrics, unit="ratio", method="covariance_calibration",
                       params={"half_life": float(half_life), "warmup_size": int(warmup_size), "test_size": int(test_size), "purge": int(purge)},
                       inputs_hash=helpers.hash_inputs(frame, half_life, warmup_size, test_size, purge), as_of=helpers.last_as_of(frame.iloc[:, 0]),
                       piece_ids=("SK-044",))

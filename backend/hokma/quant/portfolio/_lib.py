"""The only file of the portfolio module that imports quantitative or data libraries (SPEC-019, rule 3).

Every other file reaches those libraries through this one. A scan test fails if any other file imports them.
The alpha estimators, the descriptors and the asset panel of the portfolio library are imported here and in the
`_lib.py` of the fundamentals module only (correction C10 of the inventory).
"""
from __future__ import annotations

import warnings
from typing import Any

import cvxpy as cp  # noqa: E402,F401
import numpy as np  # noqa: E402,F401
import pandas as pd
from scipy import stats as sp_stats  # noqa: E402,F401
from skfolio import alpha as sk_alpha  # noqa: E402,F401
from skfolio import descriptor as sk_descriptor  # noqa: E402,F401
from skfolio import factor_exposure as sk_factor_exposure  # noqa: E402,F401
from skfolio import measures as sk_measures  # noqa: E402,F401
from skfolio import moments as sk_moments  # noqa: E402,F401
from skfolio import optimization as sk_optimization  # noqa: E402,F401
from skfolio import pre_selection as sk_pre_selection  # noqa: E402,F401
from skfolio import preprocessing as sk_preprocessing  # noqa: E402,F401
from skfolio import prior as sk_prior  # noqa: E402,F401
from skfolio import uncertainty_set as sk_uncertainty  # noqa: E402,F401
from skfolio.containers import AssetPanel as SkAssetPanel  # noqa: E402,F401
from skfolio.exceptions import OptimizationError, SolverError  # noqa: E402,F401
from skfolio.prior import FactorModel as SkFactorModel  # noqa: E402,F401
from skfolio.population import Population as SkPopulation  # noqa: E402,F401
from skfolio.portfolio import FailedPortfolio as SkFailedPortfolio  # noqa: E402,F401
from skfolio.portfolio import Portfolio as SkPortfolio  # noqa: E402,F401
from skfolio.utils import stats as sk_stats  # noqa: E402,F401
from sklearn import linear_model as sk_linear_model  # noqa: E402,F401

SOLVER_ERRORS: tuple[type[BaseException], ...] = (SolverError, OptimizationError, cp.error.SolverError, cp.error.DCPError, cp.error.DGPError)

RiskMeasure = sk_measures.RiskMeasure
ObjectiveFunction = sk_optimization.ObjectiveFunction
ForecastUnit = sk_alpha.ForecastUnit

RISK_MEASURES: tuple[str, ...] = tuple(m.value for m in RiskMeasure)
OBJECTIVES: dict[str, Any] = {
    "min_risk": ObjectiveFunction.MINIMIZE_RISK,
    "max_return": ObjectiveFunction.MAXIMIZE_RETURN,
    "max_utility": ObjectiveFunction.MAXIMIZE_UTILITY,
    "max_ratio": ObjectiveFunction.MAXIMIZE_RATIO,
}
# Every measure a portfolio object answers by name: risk, performance, ratio and the extra risk measures.
MEASURE_ENUMS = (sk_measures.RiskMeasure, sk_measures.PerfMeasure, sk_measures.RatioMeasure, sk_measures.ExtraRiskMeasure)
ALL_MEASURES: tuple[str, ...] = tuple(dict.fromkeys(m.value for e in MEASURE_ENUMS for m in e))
FORECAST_UNITS: dict[str, Any] = {"idio_return": ForecastUnit.IDIO_RETURN, "idio_sharpe": ForecastUnit.IDIO_SHARPE}
SOLVERS: tuple[str, ...] = tuple(s for s in cp.installed_solvers() if s in {"CLARABEL", "SCIPY", "OSQP", "SCS", "ECOS", "HIGHS"})

# Public name to the class of the library, for the estimators the module exposes by name.
SCORERS = {
    "standard": sk_preprocessing.CSStandardScaler,
    "gaussian_rank": sk_preprocessing.CSGaussianRankScaler,
    "percentile_rank": sk_preprocessing.CSPercentileRankScaler,
    "tanh": sk_preprocessing.CSTanhShrinker,
    "winsorizer": sk_preprocessing.CSWinsorizer,
}
PREDICTORS = {
    "ridge": sk_linear_model.Ridge,
    "linear": sk_linear_model.LinearRegression,
    "lasso": sk_linear_model.Lasso,
}


def quiet(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Call a library function with its warnings muted (a warning is not an error and never reaches the agent)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return fn(*args, **kwargs)


def sk_panel(fields: dict[str, pd.DataFrame], active: pd.DataFrame | None = None) -> Any:
    """A library asset panel from wide frames (rows are dates, columns are assets, all with the same labels)."""
    first = next(iter(fields.values()))
    arrays = {name: frame.to_numpy(dtype=float) for name, frame in fields.items()}
    mask = None if active is None else active.to_numpy(dtype=bool)
    return SkAssetPanel(fields=arrays, observations=np.asarray(first.index), asset_names=np.asarray([str(c) for c in first.columns]),
                        active_mask=mask)


def chi2_quantile(level: float, dof: int) -> float:
    """Quantile of the chi-square distribution (radius of an uncertainty ellipsoid is its square root)."""
    return float(sp_stats.chi2.ppf(level, dof))

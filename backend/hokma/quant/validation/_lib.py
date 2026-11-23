"""The only file of the validation module that imports quantitative or data libraries (SPEC-017, rule 4).

Every other file reaches those libraries through this one. A scan test fails if any other file imports them.
"""

# --- validation handles (cv, bootstrap, covariance evaluation, overfitting references, tests) ---
from scipy import stats as sp_stats  # noqa: E402,F401
from skfolio import RatioMeasure  # noqa: E402,F401
from skfolio.metrics import make_scorer  # noqa: E402,F401
from skfolio.model_selection import (  # noqa: E402,F401
    BaseCombinatorialCV,
    CombinatorialPurgedCV,
    CovarianceForecastComparison,
    CovarianceForecastEvaluation,
    MultipleRandomizedCV,
    OnlineGridSearch,
    OnlineRandomizedSearch,
    WalkForward,
    covariance_forecast_evaluation,
    cross_val_predict,
    online_covariance_forecast_evaluation,
    online_predict,
    online_score,
    optimal_folds_number,
)
from skfolio.moments import EWCovariance, EWMu  # noqa: E402,F401
from skfolio.optimization import EqualWeighted, InverseVolatility, MeanRisk  # noqa: E402,F401
from skfolio.prior import EmpiricalPrior  # noqa: E402,F401
from skfolio.utils.bootstrap import optimal_block_size, stationary_bootstrap  # noqa: E402,F401

import ffn.core as ffn_core  # noqa: E402,F401
from financetoolkit.performance import performance_model as ft_performance  # noqa: E402,F401

ffn_expected_max_sharpe = ffn_core.calc_expected_max_sharpe
ffn_deflated_sharpe_ratio = ffn_core.calc_deflated_sharpe_ratio
ffn_prob_backtest_overfitting = ffn_core.calc_prob_backtest_overfitting
ft_probabilistic_sharpe_ratio = ft_performance.get_probabilistic_sharpe_ratio
ft_deflated_sharpe_ratio = ft_performance.get_deflated_sharpe_ratio


def t_sf(statistic: float, df: float) -> float:
    """Two-sided p-value of a Student t statistic."""
    return float(2.0 * sp_stats.t.sf(abs(statistic), df))


def chi2_sf(statistic: float, df: float) -> float:
    return float(sp_stats.chi2.sf(statistic, df))


def f_sf(statistic: float, df1: float, df2: float) -> float:
    return float(sp_stats.f.sf(statistic, df1, df2))


def norm_cdf(x: float) -> float:
    return float(sp_stats.norm.cdf(x))


def jarque_bera(values) -> tuple[float, float]:
    result = sp_stats.jarque_bera(values)
    return float(result.statistic), float(result.pvalue)


def two_sample_t(a, b, equal_var: bool = False) -> tuple[float, float]:
    result = sp_stats.ttest_ind(a, b, equal_var=equal_var)
    return float(result.statistic), float(result.pvalue)

"""Risk-adjusted ratios and benchmark-relative measures (SPEC-017, metrics module).

The Sharpe-type probabilities (PSR and friends) are not here: they live in `validation` and the defective library
versions are blocked in `locks.py`.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from hokma.quant.metrics import _lib
from hokma.quant.metrics.returns import metric


def _rf(p: dict) -> float:
    return float(p["rf"])


def _ppy(p: dict) -> int:
    return int(p["periods_per_year"])


def _per_period_rf(p: dict) -> float:
    return _rf(p) / _ppy(p)


def _excess(s: pd.Series, p: dict) -> pd.Series:
    return s - _per_period_rf(p)


def _scalar(value: object) -> float:
    """A number out of a library result that may be a one-element container."""
    if isinstance(value, (pd.Series, pd.DataFrame)):
        value = np.asarray(value).ravel()[0]
    return float(value)  # type: ignore[arg-type]


# --- Sharpe, Sortino and variants (QS-010, QS-011, QS-012, FF-007) ---------------------------------------------

autocorr_penalty = metric(
    "metrics.autocorr_penalty", piece_ids=("QS-010",), unit="ratio", method="sqrt(1 + 2 * sum over lags of (1 - lag/n) * autocorrelation(lag))",
    summary="Factor by which serial correlation inflates the Sharpe ratio; above 1 flags smoothed returns.", min_obs=3,
    compute=lambda s, b, p: _lib.qstats.autocorr_penalty(s))

sharpe = metric(
    "metrics.sharpe", piece_ids=("QS-011", "FF-007"), unit="ratio",
    method="mean excess return / sample standard deviation, times sqrt(periods per year); annual rf de-annualized by compounding",
    summary="Sharpe ratio, annualized by default.", rf=True, ppy=True, defaults={"annualize": True},
    compute=lambda s, b, p: _lib.qstats.sharpe(s, rf=_rf(p), periods=_ppy(p), annualize=bool(p["annualize"])))

smart_sharpe = metric(
    "metrics.smart_sharpe", piece_ids=("QS-011",), unit="ratio", method="Sharpe ratio divided by the autocorrelation penalty",
    summary="Sharpe ratio penalized for serial correlation.", rf=True, ppy=True, defaults={"annualize": True}, min_obs=3,
    compute=lambda s, b, p: _lib.qstats.smart_sharpe(s, rf=_rf(p), periods=_ppy(p), annualize=bool(p["annualize"])))

rolling_sharpe = metric(
    "metrics.rolling_sharpe", piece_ids=("QS-011",), unit="ratio", method="Sharpe ratio over a rolling window",
    summary="Sharpe ratio over a rolling window.", rf=True, ppy=True, defaults={"window": 126, "annualize": True},
    compute=lambda s, b, p: _lib.qstats.rolling_sharpe(s, rf=_rf(p), rolling_period=int(p["window"]), annualize=bool(p["annualize"]),
                                                        periods_per_year=_ppy(p)).dropna())

sortino = metric(
    "metrics.sortino", piece_ids=("QS-012", "FF-007"), unit="ratio",
    method="mean excess return / downside deviation, times sqrt(periods per year); annual rf de-annualized by compounding",
    summary="Sortino ratio, annualized by default.", rf=True, ppy=True, defaults={"annualize": True},
    compute=lambda s, b, p: _lib.qstats.sortino(s, rf=_rf(p), periods=_ppy(p), annualize=bool(p["annualize"])))

smart_sortino = metric(
    "metrics.smart_sortino", piece_ids=("QS-012",), unit="ratio", method="Sortino ratio divided by the autocorrelation penalty",
    summary="Sortino ratio penalized for serial correlation.", rf=True, ppy=True, defaults={"annualize": True}, min_obs=3,
    compute=lambda s, b, p: _lib.qstats.smart_sortino(s, rf=_rf(p), periods=_ppy(p), annualize=bool(p["annualize"])))

rolling_sortino = metric(
    "metrics.rolling_sortino", piece_ids=("QS-012",), unit="ratio", method="Sortino ratio over a rolling window",
    summary="Sortino ratio over a rolling window.", rf=True, ppy=True, defaults={"window": 126, "annualize": True},
    compute=lambda s, b, p: _lib.qstats.rolling_sortino(s, rf=_rf(p), rolling_period=int(p["window"]), annualize=bool(p["annualize"]),
                                                         periods_per_year=_ppy(p)).dropna())

adjusted_sortino = metric(
    "metrics.adjusted_sortino", piece_ids=("QS-012",), unit="ratio", method="Sortino ratio divided by sqrt(2), to compare with the Sharpe ratio",
    summary="Sortino ratio scaled to be comparable with the Sharpe ratio.", rf=True, ppy=True, defaults={"annualize": True},
    compute=lambda s, b, p: _lib.qstats.adjusted_sortino(s, rf=_rf(p), periods=_ppy(p), annualize=bool(p["annualize"])))

calmar_price_based = metric(
    "metrics.calmar_price_based", piece_ids=("FF-007",), unit="ratio", method="CAGR of the price index (year fraction by elapsed time) / |maximum drawdown|",
    summary="Calmar ratio computed on the price index built from the returns.",
    compute=lambda s, b, p: _scalar(_lib.ffn.calc_calmar_ratio(_lib.ffn.to_price_index(s))))


def _risk_return(s: pd.Series, b: object, p: dict) -> float:
    if bool(p["annualize"]):
        return _scalar(_lib.ffn.calc_risk_return_ratio(s, nperiods=_ppy(p), annualize=True))
    return _scalar(_lib.qstats.risk_return_ratio(s))


risk_return_ratio = metric(
    "metrics.risk_return_ratio", piece_ids=("QS-029", "FF-007"), unit="ratio", method="mean return / sample standard deviation, times sqrt(periods per year) when annualized",
    summary="Mean return over standard deviation, per period by default.", ppy=True, defaults={"annualize": False}, compute=_risk_return)


# --- Pezier adjusted Sharpe (FT-068) ---------------------------------------------------------------------------

def _adjusted_sharpe(s: pd.Series, b: object, p: dict) -> float:
    sr = float((s - _per_period_rf(p)).mean() / s.std(ddof=1))
    skew = float(s.skew())
    raw_kurtosis = float(s.kurt()) + 3.0
    adjusted = _scalar(_lib.perf_model.get_adjusted_sharpe_ratio(sr, skew, raw_kurtosis))
    return adjusted * math.sqrt(_ppy(p)) if bool(p["annualize"]) else adjusted


adjusted_sharpe = metric(
    "metrics.adjusted_sharpe", piece_ids=("FT-068",), unit="ratio", method="SR * (1 + skew/6 * SR - (raw kurtosis - 3)/24 * SR^2), SR per period",
    summary="Sharpe ratio corrected for skewness and kurtosis (per period unless annualize is true).", rf=True, ppy=True,
    defaults={"annualize": False}, min_obs=4, compute=_adjusted_sharpe)


# --- Benchmark-relative (QS-014, QS-032, FF-008, FT-071) --------------------------------------------------------

treynor_ratio = metric(
    "metrics.treynor_ratio", piece_ids=("QS-014", "FT-071"), unit="ratio", method="(compounded return - rf) / beta against the benchmark",
    summary="Treynor ratio against a benchmark.", benchmark=True, rf=True, ppy=True,
    compute=lambda s, b, p: _lib.qstats.treynor_ratio(s, b, periods=float(_ppy(p)), rf=_rf(p)))

r_squared = metric(
    "metrics.r_squared", piece_ids=("QS-032",), unit="ratio", method="squared correlation between returns and benchmark",
    summary="Share of the variance of returns explained by the benchmark.", benchmark=True,
    compute=lambda s, b, p: _lib.qstats.r_squared(s, b))

information_ratio = metric(
    "metrics.information_ratio", piece_ids=("QS-032", "FF-008", "FT-071"), unit="ratio", method="mean(returns - benchmark) / std(returns - benchmark), per period",
    summary="Information ratio against a benchmark, per period.", benchmark=True,
    compute=lambda s, b, p: _lib.qstats.information_ratio(s, b))

prob_momentum = metric(
    "metrics.prob_momentum", piece_ids=("FF-008",), unit="prob", method="Student t distribution function of the mean difference over its standard error (sqrt(n) scaling)",
    summary="Probability that the strategy return beats the benchmark return, from the t statistic of the difference.", benchmark=True, min_obs=3,
    compute=lambda s, b, p: _scalar(_lib.ffn.calc_prob_mom(s, b)))

tracking_error = metric(
    "metrics.tracking_error", piece_ids=("FT-071",), unit="ratio", method="sample standard deviation of returns - benchmark, per period (times sqrt(periods) when annualized)",
    summary="Tracking error against a benchmark.", benchmark=True, ppy=True, defaults={"annualize": False},
    compute=lambda s, b, p: _scalar(_lib.perf_model.get_tracking_error(s, b)) * (math.sqrt(_ppy(p)) if bool(p["annualize"]) else 1.0))


def _m2(s: pd.Series, b: pd.Series, p: dict) -> float:
    rf_p = _per_period_rf(p)
    per_period = _scalar(_lib.perf_model.get_m2_ratio(float(s.mean()), rf_p, float(s.std(ddof=1)), float(b.std(ddof=1))))
    return per_period * _ppy(p) if bool(p["annualize"]) else per_period


m2_ratio = metric(
    "metrics.m2_ratio", piece_ids=("FT-071",), unit="ratio", method="rf + Sharpe * benchmark standard deviation (per period, times periods when annualized)",
    summary="Modigliani risk-adjusted return: the return the strategy would have with the benchmark volatility.", benchmark=True, rf=True, ppy=True,
    defaults={"annualize": True}, compute=_m2)

rolling_information_ratio = metric(
    "metrics.rolling_information_ratio", piece_ids=("FT-071",), unit="ratio", method="information ratio over a rolling window",
    summary="Information ratio over a rolling window.", benchmark=True, defaults={"window": 60},
    compute=lambda s, b, p: _lib.perf_model.get_rolling_information_ratio(s, b, int(p["window"])).dropna())

rolling_tracking_error = metric(
    "metrics.rolling_tracking_error", piece_ids=("FT-071",), unit="ratio", method="tracking error over a rolling window",
    summary="Tracking error over a rolling window.", benchmark=True, defaults={"window": 60},
    compute=lambda s, b, p: _lib.perf_model.get_rolling_tracking_error(s, b, int(p["window"])).dropna())


def _rolling_m2(s: pd.Series, b: pd.Series, p: dict) -> pd.Series:
    rf_series = pd.Series(_per_period_rf(p), index=s.index)
    return _lib.perf_model.get_rolling_m2_ratio(s, rf_series, b, int(p["window"])).dropna()


rolling_m2_ratio = metric(
    "metrics.rolling_m2_ratio", piece_ids=("FT-071",), unit="ratio", method="M2 ratio over a rolling window",
    summary="Modigliani ratio over a rolling window.", benchmark=True, rf=True, ppy=True, defaults={"window": 60}, compute=_rolling_m2)


# --- Drawdown-based ratios (QS-019, QS-020, QS-021, QS-028, FM-08, FT-072, FF-009) ------------------------------

calmar = metric(
    "metrics.calmar", piece_ids=("QS-019", "FT-072"), unit="ratio", method="CAGR / |maximum drawdown|",
    summary="Calmar ratio: annual growth over the largest drawdown.", ppy=True,
    compute=lambda s, b, p: _lib.qstats.calmar(s, periods=_ppy(p)))


def _cagr(s: pd.Series, p: dict) -> float:
    return float(_lib.qstats.cagr(s, rf=0.0, periods=_ppy(p)))


def _sterling(s: pd.Series, b: object, p: dict) -> float:
    avg_dd = _scalar(_lib.perf_model.get_average_drawdown(s))
    return _scalar(_lib.perf_model.get_sterling_ratio(pd.Series([_cagr(s, p)]), pd.Series([avg_dd]), float(p["adjustment"])))


sterling_ratio = metric(
    "metrics.sterling_ratio", piece_ids=("FT-072",), unit="ratio", method="CAGR / (|average drawdown| + adjustment)",
    summary="Sterling ratio: annual growth over the average drawdown plus an adjustment.", ppy=True, defaults={"adjustment": 0.1},
    compute=_sterling)


def _burke(s: pd.Series, b: object, p: dict) -> float:
    measure = _scalar(_lib.perf_model.get_burke_drawdown_measure(s))
    return _scalar(_lib.perf_model.get_burke_ratio(pd.Series([_cagr(s, p) - _rf(p)]), pd.Series([measure])))


burke_ratio = metric(
    "metrics.burke_ratio", piece_ids=("FT-072",), unit="ratio", method="(CAGR - rf) / sqrt(sum of squared drawdowns)",
    summary="Burke ratio: excess annual growth over the root of the squared drawdowns.", rf=True, ppy=True, compute=_burke)

ulcer_index = metric(
    "metrics.ulcer_index", piece_ids=("QS-020",), unit="ratio", method="sqrt(sum(drawdown^2) / (n - 1))",
    summary="Ulcer index: depth and length of drawdowns (fraction scale).", compute=lambda s, b, p: _lib.qstats.ulcer_index(s))

ulcer_performance_index = metric(
    "metrics.ulcer_performance_index", piece_ids=("QS-020",), unit="ratio", method="(compounded return - rf) / ulcer index",
    summary="Return per unit of ulcer index.", rf=True, compute=lambda s, b, p: _lib.qstats.ulcer_performance_index(s, rf=_rf(p)))

ulcer_index_pct = metric(
    "metrics.ulcer_index_pct", piece_ids=("FF-009",), unit="pct", method="sqrt(mean(drawdown in percent^2)) on the price index",
    summary="Ulcer index in percent points, computed on the price index.",
    compute=lambda s, b, p: _scalar(_lib.ffn.to_ulcer_index(_lib.ffn.to_price_index(s))))

ulcer_performance_index_pct = metric(
    "metrics.ulcer_performance_index_pct", piece_ids=("FF-009",), unit="ratio", method="(CAGR of the price index - rf) / ulcer index in percent",
    summary="Ulcer performance index computed on the price index.", rf=True, ppy=True,
    compute=lambda s, b, p: _scalar(_lib.ffn.to_ulcer_performance_index(_lib.ffn.to_price_index(s), rf=_rf(p), nperiods=_ppy(p))))


def _serenity(s: pd.Series, b: object, p: dict) -> float:
    wealth = (1.0 + s).cumprod()
    dd = wealth / np.maximum(wealth.cummax(), 1.0) - 1.0       # the starting capital (1.0) counts as a peak
    std = float(s.std(ddof=1))
    ulcer = math.sqrt(float((dd ** 2).sum()) / (len(dd) - 1))
    alpha = 1.0 - float(p["confidence"])
    # Expected shortfall of a normal distribution fitted to the drawdowns: mean - sigma * pdf(z_alpha) / alpha.
    z = _lib.normal_ppf(alpha)
    tail_mean = float(dd.mean()) - float(dd.std(ddof=1)) * _lib.normal_pdf(z) / alpha
    pitfall = -tail_mean / std
    return (float(s.sum()) - _rf(p)) / (ulcer * pitfall)


serenity_index = metric(
    "metrics.serenity_index", piece_ids=("QS-021", "FM-08"), unit="ratio",
    method="(sum of returns - rf) / (ulcer index * pitfall), pitfall = -normal expected shortfall of drawdowns / standard deviation",
    summary="Serenity index: return over ulcer index and the tail of the drawdown distribution.", rf=True,
    defaults={"confidence": 0.95}, min_obs=5, compute=_serenity)

recovery_factor = metric(
    "metrics.recovery_factor", piece_ids=("QS-028",), unit="ratio", method="total return / |maximum drawdown|",
    summary="Total return over the largest drawdown.", rf=True, compute=lambda s, b, p: _lib.qstats.recovery_factor(s, rf=_rf(p)))


# --- Gain and tail ratios (QS-015, QS-016, QS-024, QS-026, FT-054, FT-074) --------------------------------------

omega = metric(
    "metrics.omega", piece_ids=("QS-015", "FT-074"), unit="ratio", method="sum of gains above the threshold / sum of losses below it",
    summary="Omega ratio at a required return.", rf=True, ppy=True, defaults={"required_return": 0.0},
    compute=lambda s, b, p: _lib.qstats.omega(s, rf=_rf(p), required_return=float(p["required_return"]), periods=_ppy(p)))

gain_to_pain_ratio = metric(
    "metrics.gain_to_pain_ratio", piece_ids=("QS-016", "FT-074"), unit="ratio", method="sum of returns / |sum of negative returns|",
    summary="Gain to pain ratio of the series.", compute=lambda s, b, p: _lib.qstats.gain_to_pain_ratio(s))

tail_ratio = metric(
    "metrics.tail_ratio", piece_ids=("QS-024", "FT-054"), unit="ratio", method="|quantile(cutoff) / quantile(1 - cutoff)|",
    summary="Right tail over left tail of the return distribution.", defaults={"cutoff": 0.95},
    compute=lambda s, b, p: _lib.qstats.tail_ratio(s, cutoff=float(p["cutoff"])))

cpc_index = metric(
    "metrics.cpc_index", piece_ids=("QS-026",), unit="ratio", method="profit factor * win rate * payoff ratio",
    summary="CPC index: profit factor, win rate and payoff ratio in one number.", compute=lambda s, b, p: _lib.qstats.cpc_index(s))

common_sense_ratio = metric(
    "metrics.common_sense_ratio", piece_ids=("QS-026",), unit="ratio", method="profit factor * tail ratio",
    summary="Common sense ratio: profit factor times tail ratio.", compute=lambda s, b, p: _lib.qstats.common_sense_ratio(s))


def _kappa(s: pd.Series, b: object, p: dict) -> float:
    return _scalar(_lib.perf_model.get_kappa_ratio(_excess(s, p), int(p["order"])))


kappa_ratio = metric(
    "metrics.kappa_ratio", piece_ids=("FT-074",), unit="ratio", method="mean excess return / lower partial moment of the given order^(1/order)",
    summary="Kappa ratio of a given order (3 by default).", rf=True, ppy=True, defaults={"order": 3}, compute=_kappa)


def _starr(s: pd.Series, b: object, p: dict) -> float:
    return _scalar(_lib.perf_model.get_starr_ratio(float(_excess(s, p).mean()), s, float(p["alpha"])))


starr_ratio = metric(
    "metrics.starr_ratio", piece_ids=("FT-074",), unit="ratio", method="mean excess return / |historical expected shortfall at alpha|",
    summary="STARR ratio: excess return per unit of expected shortfall.", rf=True, ppy=True, defaults={"alpha": 0.05}, compute=_starr)

rachev_ratio = metric(
    "metrics.rachev_ratio", piece_ids=("FT-074",), unit="ratio", method="expected gain of the best alpha tail / expected loss of the worst alpha tail",
    summary="Rachev ratio between the right and left tails.", defaults={"alpha": 0.05},
    compute=lambda s, b, p: _scalar(_lib.perf_model.get_rachev_ratio(s, float(p["alpha"]))))

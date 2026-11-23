"""Volatility and dispersion measures (SPEC-017, metrics module)."""
from __future__ import annotations

import pandas as pd

from hokma.quant.metrics import _lib
from hokma.quant.metrics.returns import metric

_PERIODS = "weekly, monthly, quarterly or yearly"


def _period(label: object) -> str:
    text = str(label).lower()
    if text not in _lib.PERIOD_LABELS:
        raise ValueError(f"period must be {_PERIODS}, got {label!r}")
    return text


def _periodic(s: pd.Series) -> pd.Series:
    return _lib.to_period_index(s, "D")


volatility = metric(
    "metrics.volatility", piece_ids=("QS-009",), unit="ratio", method="sample standard deviation, times sqrt(periods per year) when annualized",
    summary="Volatility of returns, annualized by default.", ppy=True, defaults={"annualize": True},
    compute=lambda s, b, p: _lib.qstats.volatility(s, periods=int(p["periods_per_year"]), annualize=bool(p["annualize"])))

rolling_volatility = metric(
    "metrics.rolling_volatility", piece_ids=("QS-009",), unit="ratio", method="rolling sample standard deviation times sqrt(periods per year)",
    summary="Annualized volatility over a rolling window.", ppy=True, defaults={"window": 126},
    compute=lambda s, b, p: _lib.qstats.rolling_volatility(s, rolling_period=int(p["window"]), periods_per_year=int(p["periods_per_year"])).dropna())

implied_volatility = metric(
    "metrics.implied_volatility", piece_ids=("QS-009",), unit="ratio", method="rolling standard deviation of log returns times sqrt(periods)",
    summary="Volatility of log returns over a rolling window of periods_per_year observations.", ppy=True, defaults={"annualize": True},
    compute=lambda s, b, p: _lib.qstats.implied_volatility(s, periods=int(p["periods_per_year"]), annualize=bool(p["annualize"])))

variance_by_period = metric(
    "metrics.variance_by_period", piece_ids=("FT-052",), unit="ratio", method="sample variance inside each calendar period, scaled to the period length",
    summary="Variance per week, month, quarter or year.", defaults={"period": "yearly"},
    compute=lambda s, b, p: _lib.risk_model.get_variance(_periodic(s), _period(p["period"])))

volatility_by_period = metric(
    "metrics.volatility_by_period", piece_ids=("FT-052",), unit="ratio", method="sample standard deviation inside each calendar period, scaled to the period length",
    summary="Volatility per week, month, quarter or year.", defaults={"period": "yearly"},
    compute=lambda s, b, p: _lib.risk_model.get_volatility(_periodic(s), _period(p["period"])))

rolling_period_variance = metric(
    "metrics.rolling_period_variance", piece_ids=("FT-052",), unit="ratio", method="rolling variance scaled to the period length",
    summary="Rolling variance expressed for a week, month, quarter or year.", defaults={"period": "yearly", "window": 60},
    compute=lambda s, b, p: _lib.risk_model.get_rolling_variance(_periodic(s), _period(p["period"]), int(p["window"])).dropna())

rolling_period_volatility = metric(
    "metrics.rolling_period_volatility", piece_ids=("FT-052",), unit="ratio", method="rolling standard deviation scaled to the period length",
    summary="Rolling volatility expressed for a week, month, quarter or year.", defaults={"period": "yearly", "window": 60},
    compute=lambda s, b, p: _lib.risk_model.get_rolling_volatility(_periodic(s), _period(p["period"]), int(p["window"])).dropna())

rolling_tail_ratio = metric(
    "metrics.rolling_tail_ratio", piece_ids=("FT-054",), unit="ratio", method="|quantile(1 - alpha) / quantile(alpha)| over a rolling window",
    summary="Tail ratio over a rolling window.", defaults={"alpha": 0.05, "window": 60},
    compute=lambda s, b, p: _lib.risk_model.get_rolling_tail_ratio(s, float(p["alpha"]), int(p["window"])).dropna())

downside_deviation = metric(
    "metrics.downside_deviation", piece_ids=("FT-055",), unit="ratio", method="root mean square of the shortfall below the minimum acceptable return, per period",
    summary="Downside deviation per period below a minimum acceptable return.", defaults={"mar": 0.0},
    compute=lambda s, b, p: _lib.risk_model.get_downside_deviation(s, float(p["mar"])))


def _excess_volatility(s: pd.Series, b: object, p: dict) -> pd.Series:
    per_period_rf = pd.Series(float(p["rf"]) / int(p["periods_per_year"]), index=s.index)
    return _lib.risk_model.get_excess_volatility(_periodic(s), _periodic(per_period_rf), _period(p["period"]))


excess_volatility = metric(
    "metrics.excess_volatility", piece_ids=("FT-055",), unit="ratio", method="volatility of returns minus the per-period risk-free rate, per calendar period",
    summary="Volatility of excess returns per week, month, quarter or year.", rf=True, ppy=True, defaults={"period": "yearly"},
    compute=_excess_volatility)

mean_absolute_deviation = metric(
    "metrics.mean_absolute_deviation", piece_ids=("FT-055",), unit="ratio", method="mean absolute deviation from the mean inside each calendar period",
    summary="Mean absolute deviation per week, month, quarter or year.", defaults={"period": "yearly"},
    compute=lambda s, b, p: _lib.risk_model.get_mean_absolute_deviation(_periodic(s), _period(p["period"])))

coefficient_of_variation = metric(
    "metrics.coefficient_of_variation", piece_ids=("FT-055",), unit="ratio", method="standard deviation divided by the mean inside each calendar period",
    summary="Coefficient of variation per week, month, quarter or year.", defaults={"period": "yearly"},
    compute=lambda s, b, p: _lib.risk_model.get_coefficient_of_variation(_periodic(s), _period(p["period"])))

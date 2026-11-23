"""Shape of the return distribution: moments, best and worst periods, calendar tables (SPEC-017, metrics module)."""
from __future__ import annotations

import pandas as pd

from hokma.quant.errors import BadInput
from hokma.quant.metrics import _lib
from hokma.quant.metrics.returns import metric

# Calendar groups use year plus month or quarter: grouping by month number alone would mix every January together.
_AGGREGATES = {"day": None, "month": "ME", "quarter": "QE", "year": "YE"}


def _aggregate(label: object) -> str | None:
    text = "day" if label is None else str(label).lower()
    if text not in _AGGREGATES:
        raise BadInput(f"aggregate must be one of {sorted(_AGGREGATES)}, got {label!r}")
    return _AGGREGATES[text]


skew = metric(
    "metrics.skew", piece_ids=("QS-018", "FT-051"), unit="ratio", method="sample skewness (unbiased)",
    summary="Skewness of returns.", compute=lambda s, b, p: _lib.qstats.skew(s), min_obs=3)


def _kurtosis(s: pd.Series, b: object, p: dict) -> float:
    excess = float(_lib.qstats.kurtosis(s))
    return excess if bool(p["excess"]) else excess + 3.0


kurtosis = metric(
    "metrics.kurtosis", piece_ids=("QS-018", "FT-051"), unit="ratio", method="sample excess kurtosis (unbiased); raw kurtosis adds 3",
    summary="Kurtosis of returns, excess by default (raw = excess + 3, the convention probability formulas need).",
    defaults={"excess": True}, compute=_kurtosis, min_obs=4)

rolling_skew = metric(
    "metrics.rolling_skew", piece_ids=("FT-051",), unit="ratio", method="sample skewness over a rolling window",
    summary="Skewness over a rolling window.", defaults={"window": 60}, min_obs=3,
    compute=lambda s, b, p: _lib.risk_model.get_rolling_skewness(s, int(p["window"])).dropna())

rolling_kurtosis = metric(
    "metrics.rolling_kurtosis", piece_ids=("FT-051",), unit="ratio", method="excess kurtosis over a rolling window (raw when excess is false)",
    summary="Kurtosis over a rolling window.", defaults={"window": 60, "excess": True}, min_obs=4,
    compute=lambda s, b, p: _lib.risk_model.get_rolling_kurtosis(s, int(p["window"]), fisher=bool(p["excess"])).dropna())


def _autocorrelation(s: pd.Series, b: object, p: dict) -> pd.Series:
    return _lib.risk_model.get_autocorrelation(s, int(p["lags"]))


autocorrelation = metric(
    "metrics.autocorrelation", piece_ids=("FT-057",), unit="ratio", method="sum of lagged deviation products over the total sum of squares, lags 1 to n",
    summary="Serial correlation of returns at lags 1 to the requested number.", defaults={"lags": 10}, min_obs=3,
    compute=_autocorrelation)


def _distribution(s: pd.Series, b: object, p: dict) -> dict:
    raw = _lib.qstats.distribution(s, compounded=bool(p["compounded"]))
    return {period: {"values": data["values"], "outliers": data["outliers"]} for period, data in raw.items()}


distribution = metric(
    "metrics.distribution", piece_ids=("QS-002",), unit="ratio", method="returns per day, week, month, quarter and year; outliers beyond 1.5 times the interquartile range",
    summary="Distribution of returns by period with the outliers separated.", defaults={"compounded": True},
    compute=_distribution, min_obs=5)

outliers = metric(
    "metrics.outliers", piece_ids=("QS-004",), unit="ratio", method="returns above the given quantile of the series",
    summary="Returns above a high quantile (extreme gains).", defaults={"quantile": 0.95},
    compute=lambda s, b, p: _lib.qstats.outliers(s, float(p["quantile"])))

remove_outliers = metric(
    "metrics.remove_outliers", piece_ids=("QS-004",), unit="ratio", method="drop the returns above the given quantile",
    summary="Series without the returns above a high quantile.", defaults={"quantile": 0.95},
    compute=lambda s, b, p: _lib.qstats.remove_outliers(s, float(p["quantile"])))


def _best(s: pd.Series, b: object, p: dict) -> float:
    return float(_lib.qstats.best(s, aggregate=_aggregate(p["aggregate"]), compounded=bool(p["compounded"])))


def _worst(s: pd.Series, b: object, p: dict) -> float:
    return float(_lib.qstats.worst(s, aggregate=_aggregate(p["aggregate"]), compounded=bool(p["compounded"])))


best = metric(
    "metrics.best", piece_ids=("QS-005",), unit="ratio", method="maximum return of the chosen period (day, month, quarter or year)",
    summary="Best period return.", defaults={"aggregate": "day", "compounded": True}, compute=_best)

worst = metric(
    "metrics.worst", piece_ids=("QS-005",), unit="ratio", method="minimum return of the chosen period (day, month, quarter or year)",
    summary="Worst period return.", defaults={"aggregate": "day", "compounded": True}, compute=_worst)

_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def _monthly_table(s: pd.Series, b: object, p: dict) -> pd.DataFrame:
    table = _lib.qstats.monthly_returns(s, compounded=bool(p["compounded"]))
    present = {(ts.year, ts.month) for ts in pd.DatetimeIndex(s.index)}
    for year in table.index:
        for number, label in enumerate(_MONTHS, start=1):
            if label in table.columns and (int(year), number) not in present:
                table.loc[year, label] = float("nan")       # a month without observations is empty, not zero
    return table


monthly_returns = metric(
    "metrics.monthly_returns", piece_ids=("QS-035",), unit="ratio", method="compounded return of each calendar month, one row per year, with the year total",
    summary="Month by year table of returns with the end-of-year total; months without data are empty.", defaults={"compounded": True},
    compute=_monthly_table)


def _extremes(s: pd.Series, b: object, p: dict) -> dict:
    out: dict = {}
    for label in ("day", "month", "year"):
        agg = _aggregate(label)
        out[f"best_{label}"] = float(_lib.qstats.best(s, aggregate=agg, compounded=bool(p["compounded"])))
        out[f"worst_{label}"] = float(_lib.qstats.worst(s, aggregate=agg, compounded=bool(p["compounded"])))
    return out


extremes = metric(
    "metrics.extremes", piece_ids=("QS-047",), unit="ratio", method="best and worst return of the day, month and year",
    summary="Best and worst day, month and year in one block.", defaults={"compounded": True}, compute=_extremes)


def _winning_distribution(s: pd.Series, b: object, p: dict) -> dict:
    compounded = bool(p["compounded"])
    out: dict = {}
    for label, agg in (("days", None), ("months", "ME"), ("quarters", "QE"), ("years", "YE")):
        out[f"winning_{label}"] = float(_lib.qstats.win_rate(s, aggregate=agg, compounded=compounded))
    monthly = _lib.qutils.group_returns(s, pd.DatetimeIndex(s.index).to_period("M"), compounded=compounded)
    out["months_up"] = int((monthly > 0).sum())
    out["months_down"] = int((monthly < 0).sum())
    out["months_total"] = int(len(monthly))
    return out


winning_distribution = metric(
    "metrics.winning_distribution", piece_ids=("QS-049",), unit="ratio", method="share of days, months, quarters and years with a positive return; count of months up and down",
    summary="Winning share of days, months, quarters and years, with the number of months up and down.",
    defaults={"compounded": True}, compute=_winning_distribution)

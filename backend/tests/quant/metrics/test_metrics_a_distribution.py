import importlib
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.metrics import _lib

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _load(name: str) -> pd.Series:
    return pd.read_csv(FIXTURES / name, index_col=0, parse_dates=True)["return"]


R = _load("returns_daily.csv")
B = _load("benchmark_daily.csv")
MODULES = ("returns", "volatility", "drawdown", "distribution", "ratios", "locks", "accessor")
BAD_INPUTS = {
    "empty": [],
    "constant": [0.01] * 30,
    "nan": [0.01, -0.02, float("nan"), 0.03, -0.01, 0.02, 0.005, -0.004, 0.01, 0.02],
}


@pytest.fixture(autouse=True)
def registered():
    """Other test files reset the registry; load the modules again when that happened."""
    loaded = [importlib.import_module(f"hokma.quant.metrics.{name}") for name in MODULES]   # a first import registers
    if "metrics.sharpe" not in {s.name for s in registry.list("metrics")}:
        for module in loaded:
            importlib.reload(module)
    yield


def spec_of(name: str):
    return registry.describe(name)


def check_registered(name: str, piece_id: str) -> None:
    assert name in {s.name for s in registry.list("metrics")}
    assert piece_id in registry.describe(name).piece_ids


def as_array(value) -> np.ndarray:
    """Numbers of a scalar, list or date-keyed dict result, in order."""
    if isinstance(value, dict):
        return np.array([as_array(v).ravel()[0] if isinstance(v, (dict, list)) else v for v in value.values()], dtype=float)
    return np.atleast_1d(np.asarray(value, dtype=float))


def lib_series(series) -> np.ndarray:
    return np.asarray(series.dropna(), dtype=float)


def check_degenerate(fn, with_benchmark: bool = False, **kwargs) -> None:
    for label, bad in BAD_INPUTS.items():
        res = fn(bad, B.iloc[: len(bad)] if with_benchmark and bad else B, **kwargs) if with_benchmark else fn(bad, **kwargs)
        assert res.value is None, label
        assert res.warnings, label
        res = fn(R, bad, **kwargs) if with_benchmark else None
        if res is not None:
            assert res.value is None and res.warnings, label

MODULE = importlib.import_module("hokma.quant.metrics.distribution")
N = len(R)


def _q(fn, *args, **kwargs):
    with _lib.quiet():
        return fn(*args, **kwargs)


def _skew(x: pd.Series) -> float:
    n = len(x)
    z = (x - x.mean()) / x.std(ddof=1)
    return float(n / ((n - 1) * (n - 2)) * (z ** 3).sum())


def _kurt(x: pd.Series) -> float:
    n = len(x)
    z = (x - x.mean()) / x.std(ddof=1)
    return float(n * (n + 1) / ((n - 1) * (n - 2) * (n - 3)) * (z ** 4).sum() - 3.0 * (n - 1) ** 2 / ((n - 2) * (n - 3)))


def _acf(x: pd.Series, lags: int) -> list[float]:
    d = x.to_numpy() - x.mean()
    return [float((d[:-k] * d[k:]).sum() / (d ** 2).sum()) for k in range(1, lags + 1)]


def _grouped(freq: str) -> pd.Series:
    return (1.0 + R).groupby(R.index.to_period(freq)).prod() - 1.0


def test_skew_QS_018_and_FT_051():
    for row in ("QS-018", "FT-051"):
        check_registered("metrics.skew", row)
    res = MODULE.skew(R)
    assert res.value == pytest.approx(_skew(R), rel=1e-9), "hand value"
    assert res.value == pytest.approx(_q(_lib.qstats.skew, R), rel=1e-9)
    assert res.value == pytest.approx(float(_lib.risk_model.get_skewness(R)), rel=1e-9)


def test_kurtosis_QS_018_and_FT_051():
    for row in ("QS-018", "FT-051"):
        check_registered("metrics.kurtosis", row)
    res = MODULE.kurtosis(R)
    assert res.value == pytest.approx(_kurt(R), rel=1e-9), "hand value (excess)"
    assert res.value == pytest.approx(_q(_lib.qstats.kurtosis, R), rel=1e-9)
    assert res.value == pytest.approx(float(_lib.risk_model.get_kurtosis(R)), rel=1e-9)
    raw = MODULE.kurtosis(R, excess=False)
    assert raw.value == pytest.approx(_kurt(R) + 3.0, rel=1e-9)
    assert raw.value == pytest.approx(float(_lib.risk_model.get_kurtosis(R, fisher=False)), rel=1e-9)


def test_rolling_skew_and_kurtosis_FT_051():
    for name in ("rolling_skew", "rolling_kurtosis"):
        check_registered(f"metrics.{name}", "FT-051")
    skew = MODULE.rolling_skew(R, window=60)
    assert as_array(skew.value) == pytest.approx(lib_series(_lib.risk_model.get_rolling_skewness(R, 60)), rel=1e-9, abs=1e-12)
    assert as_array(skew.value)[-1] == pytest.approx(_skew(R.iloc[-60:]), rel=1e-9)
    kurt = MODULE.rolling_kurtosis(R, window=60)
    assert as_array(kurt.value) == pytest.approx(lib_series(_lib.risk_model.get_rolling_kurtosis(R, 60)), rel=1e-9, abs=1e-12)
    assert as_array(kurt.value)[-1] == pytest.approx(_kurt(R.iloc[-60:]), rel=1e-9)
    assert as_array(MODULE.rolling_kurtosis(R, window=60, excess=False).value)[-1] == pytest.approx(_kurt(R.iloc[-60:]) + 3.0, rel=1e-9)


def test_autocorrelation_FT_057():
    check_registered("metrics.autocorrelation", "FT-057")
    res = MODULE.autocorrelation(R, lags=5)
    assert as_array(res.value) == pytest.approx(_acf(R, 5), rel=1e-9, abs=1e-12)
    assert as_array(res.value) == pytest.approx(_lib.risk_model.get_autocorrelation(R, 5).to_numpy(), rel=1e-9, abs=1e-12)
    assert list(res.value) == ["1", "2", "3", "4", "5"]
    alternating = [0.01, -0.01] * 20
    assert MODULE.autocorrelation(alternating, lags=1).value["1"] < -0.9


def test_distribution_QS_002():
    check_registered("metrics.distribution", "QS-002")
    res = MODULE.distribution(R)
    assert list(res.value) == ["Daily", "Weekly", "Monthly", "Quarterly", "Yearly"]
    q1, q3 = R.quantile(0.25), R.quantile(0.75)
    fence = 1.5 * (q3 - q1)
    inside = R[(R >= q1 - fence) & (R <= q3 + fence)]
    assert res.value["Daily"]["values"] == pytest.approx(inside.tolist(), rel=1e-9)
    assert len(res.value["Daily"]["outliers"]) == N - len(inside)
    monthly = _grouped("M")
    assert len(res.value["Monthly"]["values"]) + len(res.value["Monthly"]["outliers"]) == len(monthly) == 35
    lib = _q(_lib.qstats.distribution, R)
    for period in lib:
        assert res.value[period]["values"] == pytest.approx(lib[period]["values"], rel=1e-9)
        assert res.value[period]["outliers"] == pytest.approx(lib[period]["outliers"], rel=1e-9)


def test_outliers_and_remove_outliers_QS_004():
    for name in ("outliers", "remove_outliers"):
        check_registered(f"metrics.{name}", "QS-004")
    cut = R.quantile(0.95)
    out = MODULE.outliers(R)
    kept = MODULE.remove_outliers(R)
    assert as_array(out.value) == pytest.approx(R[R >= cut].to_numpy(), rel=1e-9)
    assert as_array(kept.value) == pytest.approx(R[R < cut].to_numpy(), rel=1e-9)
    assert as_array(out.value) == pytest.approx(_q(_lib.qstats.outliers, R).to_numpy(), rel=1e-9)
    assert as_array(kept.value) == pytest.approx(_q(_lib.qstats.remove_outliers, R).to_numpy(), rel=1e-9)
    assert len(out.value) + len(kept.value) == N


@pytest.mark.parametrize("label,freq,agg", [("day", None, None), ("month", "M", "ME"), ("quarter", "Q", "QE"), ("year", "Y", "YE")])
def test_best_and_worst_QS_005(label, freq, agg):
    for name in ("best", "worst"):
        check_registered(f"metrics.{name}", "QS-005")
    series = R if freq is None else _grouped(freq)
    best, worst = MODULE.best(R, aggregate=label), MODULE.worst(R, aggregate=label)
    assert best.value == pytest.approx(float(series.max()), rel=1e-9) and worst.value == pytest.approx(float(series.min()), rel=1e-9)
    assert best.value == pytest.approx(_q(_lib.qstats.best, R, aggregate=agg), rel=1e-9)
    assert worst.value == pytest.approx(_q(_lib.qstats.worst, R, aggregate=agg), rel=1e-9)


def test_unknown_aggregate_is_bad_input():
    from hokma.quant.errors import BadInput
    with pytest.raises(BadInput):
        MODULE.best(R, aggregate="decade")


def test_monthly_returns_QS_035():
    check_registered("metrics.monthly_returns", "QS-035")
    res = MODULE.monthly_returns(R)
    monthly = _grouped("M")
    assert list(res.value) == ["2021", "2022", "2023"]
    assert res.value["2021"]["JAN"] == pytest.approx(float(monthly[pd.Period("2021-01")]), rel=1e-9)
    assert res.value["2022"]["MAR"] == pytest.approx(float(monthly[pd.Period("2022-03")]), rel=1e-9)
    assert res.value["2023"]["EOY"] == pytest.approx(float((1.0 + R["2023"]).prod() - 1.0), rel=1e-9)
    assert res.value["2023"]["NOV"] is not None and res.value["2023"]["DEC"] is None, "a month without data is empty, not zero"
    lib = _q(_lib.qstats.monthly_returns, R)
    present = {(ts.year, ts.month) for ts in R.index}
    for year in lib.index:
        for number, column in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1):
            if (int(year), number) in present:
                assert res.value[str(year)][column] == pytest.approx(float(lib.loc[year, column]), rel=1e-9)
        assert res.value[str(year)]["EOY"] == pytest.approx(float(lib.loc[year, "EOY"]), rel=1e-9)


def test_extremes_QS_047():
    check_registered("metrics.extremes", "QS-047")
    res = MODULE.extremes(R).value
    assert res["best_day"] == pytest.approx(float(R.max()), rel=1e-9) and res["worst_day"] == pytest.approx(float(R.min()), rel=1e-9)
    assert res["best_month"] == pytest.approx(float(_grouped("M").max()), rel=1e-9)
    assert res["worst_month"] == pytest.approx(float(_grouped("M").min()), rel=1e-9)
    assert res["best_year"] == pytest.approx(float(_grouped("Y").max()), rel=1e-9)
    assert res["worst_year"] == pytest.approx(float(_grouped("Y").min()), rel=1e-9)
    assert res["best_year"] == pytest.approx(_q(_lib.qstats.best, R, aggregate="YE"), rel=1e-9)


def test_winning_distribution_QS_049():
    check_registered("metrics.winning_distribution", "QS-049")
    res = MODULE.winning_distribution(R).value
    months, quarters, years = _grouped("M"), _grouped("Q"), _grouped("Y")
    assert res["winning_days"] == pytest.approx(float((R > 0).sum() / (R != 0).sum()), rel=1e-12)
    assert res["winning_months"] == pytest.approx(float((months > 0).sum() / len(months)), rel=1e-12)
    assert res["winning_quarters"] == pytest.approx(float((quarters > 0).sum() / len(quarters)), rel=1e-12)
    assert res["winning_years"] == pytest.approx(float((years > 0).sum() / len(years)), rel=1e-12)
    assert (res["months_up"], res["months_down"], res["months_total"]) == (int((months > 0).sum()), int((months < 0).sum()), 35)
    assert res["winning_months"] == pytest.approx(_q(_lib.qstats.win_rate, R, aggregate="ME"), rel=1e-9)
    assert res["winning_years"] == pytest.approx(_q(_lib.qstats.win_rate, R, aggregate="YE"), rel=1e-9)


@pytest.mark.parametrize("name", ["skew", "kurtosis", "rolling_skew", "rolling_kurtosis", "autocorrelation", "distribution", "outliers",
                                  "remove_outliers", "best", "worst", "monthly_returns", "extremes", "winning_distribution"])
def test_empty_constant_and_nan_inputs_return_none_with_warnings(name):
    check_degenerate(getattr(MODULE, name))

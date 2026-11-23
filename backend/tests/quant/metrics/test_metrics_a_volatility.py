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

MODULE = importlib.import_module("hokma.quant.metrics.volatility")
N = len(R)


def _q(fn, *args, **kwargs):
    with _lib.quiet():
        return fn(*args, **kwargs)


def _periodic(x: pd.Series) -> pd.Series:
    return _lib.to_period_index(x, "D")


def _by_year(fn) -> np.ndarray:
    return np.array([fn(R[str(year)]) for year in (2021, 2022, 2023)])


def _lib_by_period(fn, *args) -> np.ndarray:
    return np.asarray(fn(_periodic(R), *args), dtype=float)


def _tail(x: pd.Series) -> float:
    return abs(float(x.quantile(0.95) / x.quantile(0.05)))


# (row id, function, kwargs, array written in the test, array of the library)
CASES = [
    ("QS-009", "volatility", {}, lambda: [R.std(ddof=1) * math.sqrt(252)], lambda: [_q(_lib.qstats.volatility, R)]),
    ("QS-009", "volatility", {"annualize": False}, lambda: [R.std(ddof=1)], lambda: [_q(_lib.qstats.volatility, R, annualize=False)]),
    ("QS-009", "rolling_volatility", {}, lambda: [R.iloc[-126:].std(ddof=1) * math.sqrt(252)],
     lambda: lib_series(_q(_lib.qstats.rolling_volatility, R, rolling_period=126))[-1:]),
    ("QS-009", "implied_volatility", {}, lambda: [np.log1p(R).iloc[-252:].std(ddof=1) * math.sqrt(252)],
     lambda: lib_series(_q(_lib.qstats.implied_volatility, R))[-1:]),
    ("FT-052", "variance_by_period", {}, lambda: _by_year(lambda x: x.var(ddof=1) * 252), lambda: _lib_by_period(_lib.risk_model.get_variance, "yearly")),
    ("FT-052", "volatility_by_period", {}, lambda: _by_year(lambda x: x.std(ddof=1) * math.sqrt(252)), lambda: _lib_by_period(_lib.risk_model.get_volatility, "yearly")),
    ("FT-052", "rolling_period_variance", {"window": 60}, lambda: [R.iloc[-60:].var(ddof=1)],
     lambda: lib_series(_lib.risk_model.get_rolling_variance(_periodic(R), "yearly", 60))[-1:]),
    ("FT-052", "rolling_period_volatility", {"window": 60}, lambda: [R.iloc[-60:].std(ddof=1)],
     lambda: lib_series(_lib.risk_model.get_rolling_volatility(_periodic(R), "yearly", 60))[-1:]),
    ("FT-054", "rolling_tail_ratio", {"window": 60}, lambda: [_tail(R.iloc[-60:])],
     lambda: lib_series(_lib.risk_model.get_rolling_tail_ratio(R, 0.05, 60))[-1:]),
    ("FT-055", "downside_deviation", {}, lambda: [math.sqrt(float((np.minimum(R, 0.0) ** 2).mean()))], lambda: [_lib.risk_model.get_downside_deviation(R)]),
    ("FT-055", "downside_deviation", {"mar": 0.001}, lambda: [math.sqrt(float((np.minimum(R - 0.001, 0.0) ** 2).mean()))],
     lambda: [_lib.risk_model.get_downside_deviation(R, 0.001)]),
    ("FT-055", "excess_volatility", {}, lambda: _by_year(lambda x: x.std(ddof=1) * math.sqrt(252)),
     lambda: _lib_by_period(lambda r, *a: _lib.risk_model.get_excess_volatility(r, pd.Series(0.0, index=r.index), "yearly"))),
    ("FT-055", "mean_absolute_deviation", {}, lambda: _by_year(lambda x: float((x - x.mean()).abs().mean())),
     lambda: _lib_by_period(_lib.risk_model.get_mean_absolute_deviation, "yearly")),
    ("FT-055", "coefficient_of_variation", {}, lambda: _by_year(lambda x: float(x.std(ddof=1) / x.mean())),
     lambda: _lib_by_period(_lib.risk_model.get_coefficient_of_variation, "yearly")),
]


@pytest.mark.parametrize("row,name,kwargs,hand,lib", CASES, ids=[f"{c[0]}-{c[1]}-{i}" for i, c in enumerate(CASES)])
def test_matches_library_and_hand_value(row, name, kwargs, hand, lib):
    check_registered(f"metrics.{name}", row)
    res = getattr(MODULE, name)(R, **kwargs)
    assert res.ok, res.warnings
    values = as_array(res.value)
    last = values[-1:] if len(values) > 3 and len(hand()) == 1 else values
    assert last == pytest.approx(np.asarray(hand(), dtype=float), rel=1e-9, abs=1e-12), "hand value"
    assert last == pytest.approx(np.asarray(lib(), dtype=float), rel=1e-9, abs=1e-12), "library value"
    assert row in res.piece_ids


def test_excess_volatility_uses_the_risk_free_rate_per_period():
    base = MODULE.excess_volatility(R, rf=0.0).value
    shifted = MODULE.excess_volatility(R, rf=0.10).value
    assert as_array(base) == pytest.approx(as_array(shifted), rel=1e-12), "a constant rate shifts the mean, not the dispersion"


def test_bad_period_label_is_explained_not_raised():
    res = MODULE.variance_by_period(R, period="daily")
    assert res.value is None and "period must be" in res.warnings[0]


@pytest.mark.parametrize("name", sorted({c[1] for c in CASES}))
def test_empty_constant_and_nan_inputs_return_none_with_warnings(name):
    check_degenerate(getattr(MODULE, name))


def test_too_short_series_for_the_window_is_explained():
    res = MODULE.rolling_volatility(R.iloc[:50])
    assert res.value is None and res.warnings

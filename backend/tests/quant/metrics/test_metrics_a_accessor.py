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

import warnings
from importlib import metadata

MODULE = importlib.import_module("hokma.quant.metrics.accessor")
PX = 100.0 * (1.0 + R).cumprod()
N = len(R)


def _q(fn, *args, **kwargs):
    with _lib.quiet():
        return fn(*args, **kwargs)


def test_accessor_calls_the_registry_QS_060():
    check_registered("metrics.accessor_methods", "QS-060")
    sharpe = R.quant.sharpe()
    hand = float(R.mean() / R.std(ddof=1) * math.sqrt(252))
    assert sharpe.value == pytest.approx(hand, rel=1e-9)
    assert sharpe.value == pytest.approx(_q(_lib.qstats.sharpe, R), rel=1e-9)
    assert sharpe == registry.call("metrics.sharpe", returns=R)
    assert R.quant.sharpe(rf=0.02) == registry.call("metrics.sharpe", returns=R, rf=0.02)
    info = R.quant.information_ratio(benchmark=B)
    assert info.value == pytest.approx(float((R - B).mean() / (R - B).std(ddof=1)), rel=1e-9)
    assert info.value == pytest.approx(_q(_lib.qstats.information_ratio, R, B), rel=1e-9)


def test_accessor_lists_methods_and_blocks_defective_functions_QS_060():
    res = registry.call("metrics.accessor_methods")
    assert res.value["accessor"] == "quant"
    assert {"sharpe", "sortino", "max_drawdown", "calc_stats_prices", "skew"} <= set(res.value["methods"])
    assert set(res.value["blocked"]) == {"probabilistic_ratio", "probabilistic_sharpe_ratio",
                                         "probabilistic_sortino_ratio", "probabilistic_adjusted_sortino_ratio"}
    assert not set(res.value["blocked"]) & set(res.value["methods"])
    assert res.value["methods"] == MODULE.available() == R.quant.available()
    for blocked in res.value["blocked"]:
        with pytest.raises(AttributeError, match="blocked"):
            getattr(R.quant, blocked)
    with pytest.raises(AttributeError):
        R.quant.no_such_metric
    with pytest.raises(AttributeError):
        R.quant.annualize                      # takes numbers, not a series


def test_accessor_does_not_hang_the_blocked_library_functions_on_pandas():
    for name in ("probabilistic_sharpe_ratio", "probabilistic_ratio", "sharpe", "serenity_index", "monthly_returns"):
        assert not hasattr(pd.Series, name), name
        assert not hasattr(pd.DataFrame, name), name
    assert MODULE.install() is False, "already installed: a second call changes nothing"


def test_accessor_does_not_accept_the_series_twice():
    from hokma.quant.errors import BadInput
    with pytest.raises(BadInput):
        R.quant.sharpe(returns=R)


@pytest.mark.parametrize("label", sorted(BAD_INPUTS))
def test_accessor_on_bad_data_returns_none_with_warnings_QS_060(label):
    res = pd.Series(BAD_INPUTS[label], dtype=float).quant.sharpe()
    assert res.value is None and res.warnings


def test_calc_stats_prices_FF_024():
    check_registered("metrics.calc_stats_prices", "FF-024")
    res = MODULE.calc_stats_prices(PX)
    assert res.ok, res.warnings
    lib = _q(_lib.ffn.calc_stats, PX).stats
    assert res.value["total_return"] == pytest.approx(float(PX.iloc[-1] / PX.iloc[0] - 1.0), rel=1e-9), "hand value"
    assert res.value["max_drawdown"] == pytest.approx(float((PX / PX.cummax() - 1.0).min()), rel=1e-9)
    assert res.value["daily_sharpe"] == pytest.approx(float(R.iloc[1:].mean() / R.iloc[1:].std(ddof=1) * math.sqrt(252)), rel=1e-9)
    assert res.value["daily_vol"] == pytest.approx(float(R.iloc[1:].std(ddof=1) * math.sqrt(252)), rel=1e-9)
    assert res.value["best_day"] == pytest.approx(float(R.iloc[1:].max()), rel=1e-9)
    assert res.value["start"] == "2021-01-04" and res.value["end"] == "2023-11-27"
    for key, expected in lib.items():
        if key in ("start", "end") or pd.isna(expected):
            continue
        if math.isinf(float(expected)):
            assert res.value[key] is None, "an infinite statistic is reported as empty"
            continue
        assert res.value[key] == pytest.approx(float(expected), rel=1e-9), key
    assert R.quant.calc_stats_prices is not None and PX.quant.calc_stats_prices().value == res.value


@pytest.mark.parametrize("label", sorted(BAD_INPUTS))
def test_calc_stats_prices_bad_data_is_none_with_warnings_FF_024(label):
    res = MODULE.calc_stats_prices(BAD_INPUTS[label])
    assert res.value is None and res.warnings


def test_compat_layer_info_QS_061():
    check_registered("metrics.compat_info", "QS-061")
    res = registry.call("metrics.compat_info")
    names = ("numpy", "pandas", "scipy", "skfolio", "ffn", "quantstats", "financetoolkit")
    assert res.value["versions"] == {name: metadata.version(name) for name in names}
    assert res.value["resample_aliases"]["month"] == "ME" and res.value["resample_aliases"]["M"] == "ME"
    assert res.value["resample_aliases"]["year"] == "YE" and res.value["resample_aliases"]["quarter"] == "QE"
    assert res.value["period_labels"] == {"weekly": "W", "monthly": "M", "quarterly": "Q", "yearly": "Y"}
    assert registry.call("metrics.compat_info") == res


def test_compat_helpers_QS_061():
    assert _lib.resample_alias("monthly") == "ME" and _lib.resample_alias("D") == "D" and _lib.resample_alias("A") == "YE"
    with pytest.raises(ValueError, match="unknown period"):
        _lib.resample_alias("fortnight")
    assert len(R.resample(_lib.resample_alias("month")).size()) == 35
    indexed = _lib.to_period_index(R)
    assert isinstance(indexed.index, pd.PeriodIndex) and indexed.index[0] == pd.Period("2021-01-04", "D")
    assert isinstance(R.index, pd.DatetimeIndex), "the input is not modified"
    assert indexed.to_numpy() == pytest.approx(R.to_numpy(), rel=0, abs=0)
    assert _lib.normal_ppf(0.975) == pytest.approx(1.959963984540054, rel=1e-12)
    assert _lib.normal_pdf(0.0) == pytest.approx(1.0 / math.sqrt(2.0 * math.pi), rel=1e-12)
    assert _lib.prices_to_returns(pd.Series([100.0, 110.0, 99.0])).to_numpy() == pytest.approx([0.10, -0.10], rel=1e-12)
    assert _lib.prices_to_returns(pd.Series([100.0, 110.0]), log=True).to_numpy() == pytest.approx([math.log(1.1)], rel=1e-12)


def test_quiet_silences_library_warnings_and_numpy_errors():
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        with _lib.quiet():
            warnings.warn("library notice", UserWarning)
            np.log(np.array([0.0]))
        assert not seen
        warnings.warn("outside", UserWarning)
        assert len(seen) == 1

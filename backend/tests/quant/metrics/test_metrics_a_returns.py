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

MODULE = importlib.import_module("hokma.quant.metrics.returns")
PX = 100.0 * (1.0 + R).cumprod()                       # a price path with the dates of the fixture
IDX = _lib.ffn.to_price_index(R)                      # the same returns as an index with a starting row
N = len(R)


def _q(fn, *args, **kwargs):
    with _lib.quiet():
        return fn(*args, **kwargs)


def _tot(x: pd.Series) -> float:
    return float((1.0 + x).prod() - 1.0)


def _year_frac(start, end) -> float:
    return (end - start).total_seconds() / 31557600


# (row id, function, input, kwargs, array written in the test, array of the library)
SERIES_CASES = [
    ("SK-105", "prices_to_returns", PX, {}, lambda: R.iloc[1:].to_numpy(), lambda: _lib.prices_to_returns(PX).to_numpy()),
    ("SK-105", "prices_to_returns", PX, {"log": True}, lambda: np.log1p(R.iloc[1:].to_numpy()), lambda: _lib.prices_to_returns(PX, log=True).to_numpy()),
    ("QS-058", "prices_to_returns", PX, {}, lambda: R.iloc[1:].to_numpy(), lambda: _q(_lib.qutils.to_returns, PX).dropna().to_numpy()),
    ("FF-004", "to_returns", PX, {}, lambda: R.iloc[1:].to_numpy(), lambda: _q(_lib.ffn.to_returns, PX).dropna().to_numpy()),
    ("QS-058", "to_returns", PX, {}, lambda: R.iloc[1:].to_numpy(), lambda: _q(_lib.qutils.to_returns, PX).dropna().to_numpy()),
    ("FF-004", "to_log_returns", PX, {}, lambda: np.log1p(R.iloc[1:].to_numpy()), lambda: _q(_lib.ffn.to_log_returns, PX).dropna().to_numpy()),
    ("FF-004", "to_price_index", R, {}, lambda: np.concatenate([[100.0], 100.0 * np.cumprod(1.0 + R.to_numpy())]), lambda: IDX.to_numpy()),
    ("QS-058", "to_prices", R, {}, lambda: 100000.0 * np.cumprod(1.0 + R.to_numpy()), lambda: _q(_lib.qutils.to_prices, R).to_numpy()),
    ("QS-058", "log_returns", R, {}, lambda: np.log1p(R.to_numpy()), lambda: _q(_lib.qutils.log_returns, R).to_numpy()),
    ("FF-004", "rebase", PX, {}, lambda: (PX / PX.iloc[0] * 100.0).to_numpy(), lambda: _q(_lib.ffn.rebase, PX).to_numpy()),
    ("QS-058", "rebase", PX, {}, lambda: (PX / PX.iloc[0] * 100.0).to_numpy(), lambda: _q(_lib.qutils.rebase, PX).to_numpy()),
    ("QS-058", "group_returns", R, {}, lambda: ((1.0 + R).groupby(R.index.to_period("M")).prod() - 1.0).to_numpy(),
     lambda: _q(_lib.qutils.group_returns, R, R.index.to_period("M"), compounded=True).to_numpy()),
    ("QS-058", "make_portfolio", R, {}, lambda: np.concatenate([[100000.0], 100000.0 * np.cumprod(1.0 + R.to_numpy())]),
     lambda: _q(_lib.qutils.make_portfolio, R).to_numpy()),
    ("QS-001", "compsum", R, {}, lambda: np.cumprod(1.0 + R.to_numpy()) - 1.0, lambda: _q(_lib.qstats.compsum, R).to_numpy()),
    ("FF-021", "rolling_apply", R, {"window": 20}, lambda: R.rolling(20).mean().dropna().to_numpy(),
     lambda: _lib.ffn.rollapply(R, 20, np.mean).dropna().to_numpy()),
    ("FF-021", "rescale", R, {}, lambda: ((R - R.min()) / (R.max() - R.min())).to_numpy(), lambda: _lib.ffn.rescale(R).to_numpy()),
    ("FF-022", "to_monthly", PX, {}, lambda: PX.groupby(PX.index.to_period("M")).last().to_numpy()[:-1], lambda: _q(_lib.ffn.to_monthly, PX).to_numpy()),
    ("FF-022", "asfreq_actual", PX, {}, lambda: PX.groupby(PX.index.to_period("M")).last().to_numpy()[:-1], lambda: _q(_lib.ffn.asfreq_actual, PX, "ME").to_numpy()),
]

SCALAR_CASES = [
    ("QS-001", "total_return", R, {}, lambda: _tot(R), lambda: _q(_lib.qstats.comp, R)),
    ("FF-006", "total_return", R, {}, lambda: _tot(R), lambda: _q(_lib.ffn.calc_total_return, IDX)),
    ("QS-003", "expected_return", R, {}, lambda: (1.0 + _tot(R)) ** (1 / N) - 1.0, lambda: _q(_lib.qstats.expected_return, R)),
    ("QS-003", "geometric_mean", R, {}, lambda: (1.0 + _tot(R)) ** (1 / N) - 1.0, lambda: _q(_lib.qstats.geometric_mean, R)),
    ("QS-003", "ghpr", R, {}, lambda: (1.0 + _tot(R)) ** (1 / N) - 1.0, lambda: _q(_lib.qstats.ghpr, R)),
    ("QS-017", "cagr", R, {}, lambda: (1.0 + _tot(R)) ** (252 / N) - 1.0, lambda: _q(_lib.qstats.cagr, R)),
    ("QS-017", "rar", R, {}, lambda: (1.0 + _tot(R)) ** (252 / N) - 1.0, lambda: _q(_lib.qstats.rar, R)),
    ("FF-006", "cagr_from_prices", PX, {}, lambda: (PX.iloc[-1] / PX.iloc[0]) ** (1 / _year_frac(PX.index[0], PX.index[-1])) - 1.0,
     lambda: _q(_lib.ffn.calc_cagr, PX)),
    ("FF-006", "mtd", R, {}, lambda: _tot(R["2023-11"]), lambda: _q(_lib.ffn.calc_mtd, IDX, IDX.resample("ME").last())),
    ("FF-006", "ytd", R, {}, lambda: _tot(R["2023"]), lambda: _q(_lib.ffn.calc_ytd, IDX, IDX.resample("YE").last())),
    ("FF-022", "infer_nperiods", R, {}, 252, lambda: _q(_lib.ffn.infer_nperiods, R)),
]


def _run(name, data, kwargs):
    return getattr(MODULE, name)(data, **kwargs)


@pytest.mark.parametrize("row,name,data,kwargs,hand,lib", SERIES_CASES, ids=[f"{c[0]}-{c[1]}-{i}" for i, c in enumerate(SERIES_CASES)])
def test_series_matches_library_and_hand_value(row, name, data, kwargs, hand, lib):
    check_registered(f"metrics.{name}", row)
    res = _run(name, data, kwargs)
    assert res.ok, res.warnings
    values = as_array(res.value)
    assert values == pytest.approx(hand(), rel=1e-9, abs=1e-12), "hand value"
    assert values == pytest.approx(lib(), rel=1e-9, abs=1e-12), "library value"
    assert row in res.piece_ids and res.inputs_hash


@pytest.mark.parametrize("row,name,data,kwargs,hand,lib", SCALAR_CASES, ids=[f"{c[0]}-{c[1]}" for c in SCALAR_CASES])
def test_scalar_matches_library_and_hand_value(row, name, data, kwargs, hand, lib):
    check_registered(f"metrics.{name}", row)
    res = _run(name, data, kwargs)
    assert res.ok, res.warnings
    assert res.value == pytest.approx(hand() if callable(hand) else hand, rel=1e-9, abs=1e-12), "hand value"
    assert res.value == pytest.approx(float(lib()), rel=1e-9, abs=1e-12), "library value"


def test_pct_rank_QS_001():
    check_registered("metrics.pct_rank", "QS-001")
    res = MODULE.pct_rank(PX, window=60)
    window = PX.iloc[-60:].to_numpy()
    expected_last = (np.sum(window < window[-1]) + 1) / 60 * 100.0
    assert as_array(res.value)[-1] == pytest.approx(expected_last, rel=1e-9)
    assert as_array(res.value) == pytest.approx(lib_series(_q(_lib.qstats.pct_rank, PX, window=60)), rel=1e-9)


def test_winsorize_FF_021():
    check_registered("metrics.winsorize", "FF-021")
    res = MODULE.winsorize(R, limits=0.01)
    ordered = np.sort(R.to_numpy())
    k = int(0.01 * N)
    expected = np.clip(R.to_numpy(), ordered[k], ordered[N - 1 - k])
    assert as_array(res.value) == pytest.approx(expected, rel=1e-9, abs=1e-12)
    assert as_array(res.value) == pytest.approx(_lib.ffn.winsorize(R, limits=0.01).to_numpy(), rel=1e-9, abs=1e-12)


def test_resample_returns_FF_013():
    check_registered("metrics.resample_returns", "FF-013")
    res = MODULE.resample_returns(R, statistic="mean", seed=3, num_trials=5)
    expected = [float(R.iloc[np.random.RandomState(3 + i).randint(0, N, N)].mean()) for i in range(5)]
    assert res.value["values"] == pytest.approx(expected, rel=1e-9, abs=1e-12)
    lib = _lib.ffn.resample_returns(R, lambda x: float(x.mean()), seed=3, num_trials=5).astype(float)
    assert res.value["values"] == pytest.approx(lib.to_numpy(), rel=1e-9, abs=1e-12)
    assert MODULE.resample_returns(R, statistic="mean", seed=3, num_trials=5).value == res.value, "fixed seed is repeatable"


def test_annualize_and_deannualize_FF_021():
    check_registered("metrics.annualize", "FF-021")
    check_registered("metrics.deannualize", "FF-021")
    res = MODULE.annualize(total_return=0.10, duration_days=730)
    assert res.value == pytest.approx(1.10 ** (365 / 730) - 1.0, rel=1e-12)
    assert res.value == pytest.approx(float(_lib.ffn.annualize(0.10, 730)), rel=1e-12)
    res = MODULE.deannualize(annual_rate=0.10, periods_per_year=252)
    assert res.value == pytest.approx(1.10 ** (1 / 252) - 1.0, rel=1e-12)
    assert res.value == pytest.approx(float(_lib.ffn.deannualize(0.10, 252)), rel=1e-12)
    assert MODULE.annualize(total_return=0.1).value is None and MODULE.annualize(total_return=0.1).warnings
    assert MODULE.annualize(total_return=-1.5, duration_days=100).value is None


def test_infer_freq_and_num_days_required_FF_022():
    check_registered("metrics.infer_freq", "FF-022")
    res = MODULE.infer_freq(R)
    assert res.value["periods_per_year"] == 252
    assert res.value["freq"] == _q(_lib.ffn.infer_freq, R)
    assert res.value["first"] == "2021-01-04" and res.value["last"] == "2023-11-27"
    weekly = pd.Series(np.linspace(-0.01, 0.02, 30), index=pd.date_range("2022-01-02", periods=30, freq="W"))
    assert MODULE.infer_freq(weekly).value["periods_per_year"] == 52
    check_registered("metrics.num_days_required", "FF-022")
    res = MODULE.num_days_required(lookback_months=12)
    assert res.value == pytest.approx(365 * 0.69 * 0.9, rel=1e-12)
    assert res.value == pytest.approx(_lib.ffn.get_num_days_required(pd.DateOffset(months=12)), rel=1e-12)
    assert MODULE.num_days_required(lookback_months=12, period="m").value == pytest.approx(365 * 0.69 / 20 * 0.9, rel=1e-12)
    assert MODULE.num_days_required(lookback_months=12, period="x").value is None


def test_merge_series_drop_duplicates_and_year_frac_FF_023():
    for name in ("merge_series", "drop_duplicate_cols", "year_frac"):
        check_registered(f"metrics.{name}", "FF-023")
    left, right = R.iloc[:3], R.iloc[1:4]
    res = MODULE.merge_series(returns=left, other=right)
    assert res.ok
    assert list(res.value) == ["2021-01-04", "2021-01-05", "2021-01-06", "2021-01-07"]
    assert res.value["2021-01-05"]["returns"] == pytest.approx(float(R.iloc[1])) and res.value["2021-01-05"]["other"] == pytest.approx(float(R.iloc[1]))
    assert res.value["2021-01-04"]["other"] is None and res.value["2021-01-07"]["returns"] is None
    assert len(_lib.ffn.merge(left.rename("returns"), right.rename("other"))) == 4
    same = MODULE.drop_duplicate_cols(returns=R, other=R.iloc[:100])      # both are named "return"
    assert same.value == {"columns": ["return"], "observations": N}
    assert _lib.ffn.drop_duplicate_cols(pd.concat([R, R.iloc[:100]], axis=1)).shape == (N, 1)
    assert MODULE.drop_duplicate_cols(returns=R, other=B.rename("benchmark")).value["columns"] == ["return", "benchmark"]
    res = MODULE.year_frac(start="2021-01-01", end="2022-01-01")
    assert res.value == pytest.approx(365 * 86400 / 31557600, rel=1e-12)
    assert res.value == pytest.approx(_lib.ffn.year_frac(pd.Timestamp("2021-01-01"), pd.Timestamp("2022-01-01")), rel=1e-12)
    assert MODULE.year_frac(start="2022-01-01", end="2021-01-01").value is None
    assert MODULE.merge_series(returns=[], other=right).value is None and MODULE.merge_series(returns=[], other=right).warnings


NAMES = sorted({c[1] for c in SERIES_CASES + SCALAR_CASES} | {"pct_rank", "winsorize", "resample_returns", "infer_freq"})


@pytest.mark.parametrize("name", NAMES)
def test_empty_constant_and_nan_inputs_return_none_with_warnings(name):
    check_degenerate(getattr(MODULE, name))


def test_every_row_of_this_file_is_in_the_registry():
    rows = {"FF-004", "FF-006", "FF-013", "FF-021", "FF-022", "FF-023", "QS-001", "QS-003", "QS-017", "QS-058", "SK-105"}
    found = {p for s in registry.list("metrics") for p in s.piece_ids}
    assert rows <= found

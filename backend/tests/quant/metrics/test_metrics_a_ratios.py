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


def _mdd(x: pd.Series) -> float:
    wealth = (1.0 + x).cumprod()
    return float((wealth / np.maximum(wealth.cummax(), 1.0) - 1.0).min())


def _sharpe(x: pd.Series) -> float:
    return float(x.mean() / x.std(ddof=1) * math.sqrt(252))


def _downside(x: pd.Series) -> float:
    return math.sqrt(float((x[x < 0] ** 2).sum()) / len(x))


def _sortino(x: pd.Series) -> float:
    return float(x.mean()) / _downside(x) * math.sqrt(252)


def _penalty(x: pd.Series) -> float:
    n = len(x)
    c = abs(float(np.corrcoef(x.iloc[:-1], x.iloc[1:])[0, 1]))
    return math.sqrt(1.0 + 2.0 * sum(((n - k) / n) * c ** k for k in range(1, n)))


def _cagr(x: pd.Series) -> float:
    return float((1.0 + x).prod() ** (252 / len(x)) - 1.0)


def _profit_factor(x: pd.Series) -> float:
    return float(x[x > 0].sum() / abs(x[x < 0].sum()))


def _tail(x: pd.Series) -> float:
    return abs(float(x.quantile(0.95) / x.quantile(0.05)))


def _payoff(x: pd.Series) -> float:
    return float(x[x > 0].mean() / abs(x[x < 0].mean()))


def _beta(x: pd.Series, y: pd.Series) -> float:
    return float(np.cov(x, y)[0, 1] / np.cov(x, y)[1, 1])


def _dd_series(x: pd.Series) -> pd.Series:
    wealth = (1.0 + x).cumprod()
    return wealth / np.maximum(wealth.cummax(), 1.0) - 1.0


def _ulcer(x: pd.Series) -> float:
    return math.sqrt(float((_dd_series(x) ** 2).sum()) / (len(x) - 1))


def _q(fn, *args, **kwargs):
    with _lib.quiet():
        return fn(*args, **kwargs)


def _adjusted_sharpe() -> float:
    sr = float(R.mean() / R.std(ddof=1))
    skew, kurt = float(R.skew()), float(R.kurt()) + 3.0
    return sr * (1.0 + skew / 6.0 * sr - (kurt - 3.0) / 24.0 * sr ** 2)


def _kappa() -> float:
    return float(R.mean() / (np.maximum(-R, 0.0) ** 3).mean() ** (1 / 3))


def _prob_momentum() -> float:
    d = R - B
    n = len(d)
    return float(_lib.scipy_stats.t.cdf(d.mean() / d.std(ddof=1) * math.sqrt(n), n - 1))


def _m2() -> float:
    return float(R.mean() / R.std(ddof=1) * B.std(ddof=1) * 252)


def _lib_rolling(fn, **kw):
    return lib_series(_q(fn, R, **kw))


# (row id, function, takes benchmark, kwargs, value written in the test, value of the library)
SCALARS = [
    ("QS-010", "autocorr_penalty", False, {}, lambda: _penalty(R), lambda: _q(_lib.qstats.autocorr_penalty, R)),
    ("QS-011", "sharpe", False, {}, lambda: _sharpe(R), lambda: _q(_lib.qstats.sharpe, R)),
    ("QS-011", "smart_sharpe", False, {}, lambda: _sharpe(R) / _penalty(R), lambda: _q(_lib.qstats.smart_sharpe, R)),
    ("QS-012", "sortino", False, {}, lambda: _sortino(R), lambda: _q(_lib.qstats.sortino, R)),
    ("QS-012", "smart_sortino", False, {}, lambda: _sortino(R) / _penalty(R), lambda: _q(_lib.qstats.smart_sortino, R)),
    ("QS-012", "adjusted_sortino", False, {}, lambda: _sortino(R) / math.sqrt(2), lambda: _q(_lib.qstats.adjusted_sortino, R)),
    ("FF-007", "sharpe", False, {"rf": 0.05},
     lambda: float((R - ((1.05) ** (1 / 252) - 1)).mean() / R.std(ddof=1) * math.sqrt(252)), lambda: _q(_lib.ffn.calc_sharpe, R, rf=0.05, nperiods=252)),
    ("FF-007", "sortino", False, {}, lambda: _sortino(R), lambda: _q(_lib.ffn.calc_sortino_ratio, R, nperiods=252)),
    ("FF-007", "calmar_price_based", False, {}, 0.009849287357574087, lambda: _q(_lib.ffn.calc_calmar_ratio, _lib.ffn.to_price_index(R))),
    ("QS-029", "risk_return_ratio", False, {}, lambda: float(R.mean() / R.std(ddof=1)), lambda: _q(_lib.qstats.risk_return_ratio, R)),
    ("FF-007", "risk_return_ratio", False, {"annualize": True}, lambda: _sharpe(R), lambda: _q(_lib.ffn.calc_risk_return_ratio, R, nperiods=252)),
    ("FT-068", "adjusted_sharpe", False, {}, _adjusted_sharpe,
     lambda: _lib.perf_model.get_adjusted_sharpe_ratio(float(R.mean() / R.std(ddof=1)), float(R.skew()), float(R.kurt()) + 3.0)),
    ("QS-014", "treynor_ratio", True, {}, lambda: float((1.0 + R).prod() - 1.0) / _beta(R, B), lambda: _q(_lib.qstats.treynor_ratio, R, B)),
    ("FT-071", "treynor_ratio", True, {}, lambda: float((1.0 + R).prod() - 1.0) / _beta(R, B),
     lambda: _lib.perf_model.get_treynor_ratio(float((1.0 + R).prod() - 1.0), 0.0, _beta(R, B))),
    ("QS-032", "r_squared", True, {}, lambda: float(np.corrcoef(B, R)[0, 1] ** 2), lambda: _q(_lib.qstats.r_squared, R, B)),
    ("QS-032", "information_ratio", True, {}, lambda: float((R - B).mean() / (R - B).std(ddof=1)), lambda: _q(_lib.qstats.information_ratio, R, B)),
    ("FF-008", "information_ratio", True, {}, lambda: float((R - B).mean() / (R - B).std(ddof=1)), lambda: _q(_lib.ffn.calc_information_ratio, R, B)),
    ("FT-071", "information_ratio", True, {}, lambda: float((R - B).mean() / (R - B).std(ddof=1)), lambda: _q(_lib.perf_model.get_information_ratio, R, B)),
    ("FF-008", "prob_momentum", True, {}, _prob_momentum, lambda: _q(_lib.ffn.calc_prob_mom, R, B)),
    ("FT-071", "tracking_error", True, {}, lambda: float((R - B).std(ddof=1)), lambda: _q(_lib.perf_model.get_tracking_error, R, B)),
    ("FT-071", "m2_ratio", True, {}, _m2,
     lambda: _lib.perf_model.get_m2_ratio(float(R.mean()), 0.0, float(R.std(ddof=1)), float(B.std(ddof=1))) * 252),
    ("QS-019", "calmar", False, {}, lambda: _cagr(R) / abs(_mdd(R)), lambda: _q(_lib.qstats.calmar, R)),
    ("FT-072", "calmar", False, {}, lambda: _cagr(R) / abs(_mdd(R)),
     lambda: float(_lib.perf_model.get_calmar_ratio(pd.Series([_cagr(R)]), pd.Series([_mdd(R)])).iloc[0])),
    ("FT-072", "sterling_ratio", False, {}, 0.009536108869387052,
     lambda: float(_lib.perf_model.get_sterling_ratio(pd.Series([_cagr(R)]), pd.Series([float(_lib.perf_model.get_average_drawdown(R))])).iloc[0])),
    ("FT-072", "burke_ratio", False, {}, 0.0005263113678101447,
     lambda: float(_lib.perf_model.get_burke_ratio(pd.Series([_cagr(R)]), pd.Series([float(_lib.perf_model.get_burke_drawdown_measure(R))])).iloc[0])),
    ("QS-020", "ulcer_index", False, {}, lambda: _ulcer(R), lambda: _q(_lib.qstats.ulcer_index, R)),
    ("QS-020", "ulcer_performance_index", False, {}, lambda: float((1.0 + R).prod() - 1.0) / _ulcer(R), lambda: _q(_lib.qstats.ulcer_performance_index, R)),
    ("FF-009", "ulcer_index_pct", False, {}, 17.391910923189922, lambda: _q(_lib.ffn.to_ulcer_index, _lib.ffn.to_price_index(R))),
    ("FF-009", "ulcer_performance_index_pct", False, {}, 0.01496975640122167, lambda: _q(_lib.ffn.to_ulcer_performance_index, _lib.ffn.to_price_index(R), nperiods=252)),
    ("QS-021", "serenity_index", False, {}, 0.010971315609630742, lambda: _q(_lib.qstats.serenity_index, R)),
    ("QS-028", "recovery_factor", False, {}, lambda: abs(float(R.sum())) / abs(_mdd(R)), lambda: _q(_lib.qstats.recovery_factor, R)),
    ("QS-015", "omega", False, {}, lambda: float(R[R > 0].sum() / -R[R < 0].sum()), lambda: _q(_lib.qstats.omega, R)),
    ("FT-074", "omega", False, {}, lambda: float(R[R > 0].sum() / -R[R < 0].sum()), lambda: _lib.perf_model.get_omega_ratio(R)),
    ("QS-016", "gain_to_pain_ratio", False, {}, lambda: float(R.sum() / abs(R[R < 0].sum())), lambda: _q(_lib.qstats.gain_to_pain_ratio, R)),
    ("FT-074", "gain_to_pain_ratio", False, {}, lambda: float(R.sum() / abs(R[R < 0].sum())), lambda: _lib.perf_model.get_gain_to_pain_ratio(R)),
    ("QS-024", "tail_ratio", False, {}, lambda: _tail(R), lambda: _q(_lib.qstats.tail_ratio, R)),
    ("FT-054", "tail_ratio", False, {}, lambda: _tail(R), lambda: _lib.risk_model.get_tail_ratio(R, 0.05)),
    ("QS-026", "cpc_index", False, {}, lambda: _profit_factor(R) * float((R > 0).sum() / len(R)) * _payoff(R), lambda: _q(_lib.qstats.cpc_index, R)),
    ("QS-026", "common_sense_ratio", False, {}, lambda: _profit_factor(R) * _tail(R), lambda: _q(_lib.qstats.common_sense_ratio, R)),
    ("FT-074", "kappa_ratio", False, {}, _kappa, lambda: _lib.perf_model.get_kappa_ratio(R)),
    ("FT-074", "starr_ratio", False, {}, 0.0029156096721701342, lambda: _lib.perf_model.get_starr_ratio(float(R.mean()), R, 0.05)),
    ("FT-074", "rachev_ratio", False, {}, 0.9247611440313924, lambda: _lib.perf_model.get_rachev_ratio(R, 0.05)),
]

ROLLING = [
    ("QS-011", "rolling_sharpe", False, {"window": 126}, lambda: lib_series(_q(_lib.qstats.rolling_sharpe, R, rolling_period=126)),
     lambda: float(R.iloc[-126:].mean() / R.iloc[-126:].std(ddof=1) * math.sqrt(252))),
    ("QS-012", "rolling_sortino", False, {"window": 126}, lambda: lib_series(_q(_lib.qstats.rolling_sortino, R, rolling_period=126)),
     lambda: _sortino(R.iloc[-126:])),
    ("FT-071", "rolling_information_ratio", True, {"window": 60}, lambda: lib_series(_lib.perf_model.get_rolling_information_ratio(R, B, 60)),
     lambda: float((R - B).iloc[-60:].mean() / (R - B).iloc[-60:].std(ddof=1))),
    ("FT-071", "rolling_tracking_error", True, {"window": 60}, lambda: lib_series(_lib.perf_model.get_rolling_tracking_error(R, B, 60)),
     lambda: float((R - B).iloc[-60:].std(ddof=1))),
    ("FT-071", "rolling_m2_ratio", True, {"window": 60},
     lambda: lib_series(_lib.perf_model.get_rolling_m2_ratio(R, pd.Series(0.0, index=R.index), B, 60)), None),
]

MODULE = importlib.import_module("hokma.quant.metrics.ratios")


def _call(name: str, bench: bool, kwargs: dict):
    fn = getattr(MODULE, name)
    return fn(R, B, **kwargs) if bench else fn(R, **kwargs)


@pytest.mark.parametrize("row,name,bench,kwargs,hand,lib", SCALARS, ids=[f"{c[0]}-{c[1]}" for c in SCALARS])
def test_scalar_matches_library_and_hand_value(row, name, bench, kwargs, hand, lib):
    check_registered(f"metrics.{name}", row)
    res = _call(name, bench, kwargs)
    assert res.ok, res.warnings
    expected_hand = hand() if callable(hand) else hand
    assert res.value == pytest.approx(expected_hand, rel=1e-9, abs=1e-12), "hand value"
    assert res.value == pytest.approx(float(np.asarray(lib()).ravel()[0]), rel=1e-9, abs=1e-12), "library value"
    assert row in res.piece_ids and res.name == f"metrics.{name}" and res.inputs_hash


@pytest.mark.parametrize("row,name,bench,kwargs,lib,hand_last", ROLLING, ids=[f"{c[0]}-{c[1]}" for c in ROLLING])
def test_rolling_matches_library_and_hand_value(row, name, bench, kwargs, lib, hand_last):
    check_registered(f"metrics.{name}", row)
    res = _call(name, bench, kwargs)
    assert res.ok, res.warnings
    values = as_array(res.value)
    assert values == pytest.approx(lib(), rel=1e-9, abs=1e-12)
    if hand_last is not None:
        assert values[-1] == pytest.approx(hand_last(), rel=1e-9, abs=1e-12)


@pytest.mark.parametrize("name,bench", sorted({(c[1], c[2]) for c in SCALARS + ROLLING}), ids=lambda v: str(v))
def test_empty_constant_and_nan_inputs_return_none_with_warnings(name, bench):
    check_degenerate(getattr(MODULE, name), with_benchmark=bench)


def test_serenity_known_value_on_ten_fixed_returns_FM_08():
    """FM-08: the formula written out with the standard library on ten fixed returns."""
    from statistics import NormalDist
    x = [0.01, -0.02, 0.015, -0.005, 0.02, -0.03, 0.01, 0.005, -0.01, 0.025]
    wealth = peak = 1.0
    dd = []
    for v in x:
        wealth *= 1.0 + v
        peak = max(peak, wealth)
        dd.append(wealth / peak - 1.0)
    n = len(dd)
    mu = sum(dd) / n
    sd = math.sqrt(sum((d - mu) ** 2 for d in dd) / (n - 1))
    ulcer = math.sqrt(sum(d * d for d in dd) / (n - 1))
    nd = NormalDist()
    shortfall = mu - sd * nd.pdf(nd.inv_cdf(0.05)) / 0.05
    mean = sum(x) / n
    std = math.sqrt(sum((v - mean) ** 2 for v in x) / (n - 1))
    expected = sum(x) / (ulcer * (-shortfall / std))
    check_registered("metrics.serenity_index", "FM-08")
    res = MODULE.serenity_index(x)
    assert res.value == pytest.approx(expected, rel=1e-12)
    assert res.value == pytest.approx(0.5771882120405697, rel=1e-9)
    dated = pd.Series(x, index=pd.bdate_range("2024-01-01", periods=len(x)))
    assert res.value == pytest.approx(_q(_lib.qstats.serenity_index, dated), rel=1e-9)


def test_risk_free_rate_changes_the_sharpe_ratio():
    base = MODULE.sharpe(R).value
    assert MODULE.sharpe(R, rf=0.05).value == pytest.approx(float((R - (1.05 ** (1 / 252) - 1)).mean() / R.std(ddof=1) * math.sqrt(252)), rel=1e-9)
    assert MODULE.sharpe(R, rf=0.05).value < base


def test_unknown_parameter_is_bad_input():
    from hokma.quant.errors import BadInput
    with pytest.raises(BadInput):
        MODULE.sharpe(R, nope=1)
    with pytest.raises(BadInput):
        MODULE.sharpe("abc")

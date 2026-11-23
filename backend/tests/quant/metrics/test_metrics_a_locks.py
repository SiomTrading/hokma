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
    overfit = importlib.import_module("hokma.quant.validation.overfit")        # a first import registers
    if "validation.psr" not in {s.name for s in registry.list("validation")}:
        importlib.reload(overfit)
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

from statistics import NormalDist

from hokma.quant.errors import BadInput, BlockedFunction

MODULE = importlib.import_module("hokma.quant.metrics.locks")
BLOCKED = ["probabilistic_ratio", "probabilistic_sharpe_ratio", "probabilistic_sortino_ratio", "probabilistic_adjusted_sortino_ratio"]
N = len(R)


def _psr_reference(sr: float, skew: float, kurtosis_raw: float, n: int, sr_benchmark: float = 0.0) -> float:
    """Probability that the true Sharpe ratio is above the benchmark, written out with the standard library."""
    variance = 1.0 - skew * sr + (kurtosis_raw - 1.0) / 4.0 * sr ** 2
    return NormalDist().cdf((sr - sr_benchmark) * math.sqrt(n - 1) / math.sqrt(variance))


def _psr_with_kurtosis_subtracted_twice(sr: float, skew: float, kurtosis_raw: float, n: int) -> float:
    """The defective convention: 3 is subtracted from a kurtosis that is already an excess value."""
    excess = kurtosis_raw - 3.0
    variance = 1.0 - skew * sr + (excess - 3.0 - 1.0) / 4.0 * sr ** 2
    return NormalDist().cdf(sr * math.sqrt(n - 1) / math.sqrt(variance))


@pytest.mark.parametrize("name", BLOCKED)
def test_library_probabilistic_functions_are_blocked_QS_013a(name):
    full = f"metrics.{name}"
    assert full not in {s.name for s in registry.list("metrics")}
    spec = next(s for s in registry.list("metrics", include_blocked=True) if s.name == full)
    assert spec.blocked and spec.replacement == "validation.psr"
    assert {"QS-013a", "CC-02", "CC-03"} <= set(spec.piece_ids)
    with pytest.raises(BlockedFunction) as err:
        registry.call(full)
    assert err.value.replacement == "validation.psr"
    with pytest.raises(BlockedFunction):
        registry.call(full, returns=R)


def test_psr_is_exactly_the_validation_psr_QS_013a():
    check_registered("metrics.psr", "QS-013a")
    mine = MODULE.psr(R)
    theirs = registry.call("validation.psr", returns=R, sr_benchmark=0.0)
    assert mine == theirs and mine.name == "validation.psr" and mine.unit == "prob"
    assert registry.call("metrics.psr", returns=R) == theirs
    shifted = MODULE.psr(R, sr_benchmark=0.01)
    assert shifted == registry.call("validation.psr", returns=R, sr_benchmark=0.01)
    assert shifted.value < mine.value


def test_psr_fixed_case_sr_0_1_n_252_normal_moments_QS_013a():
    hand = NormalDist().cdf(0.1 * math.sqrt(251) / math.sqrt(1.0 + 0.1 ** 2 * (3.0 - 1.0) / 4.0))
    res = registry.call("validation.psr_moments", sr=0.1, skew=0.0, kurtosis_raw=3, n=252)
    assert res.value == pytest.approx(hand, rel=1e-12)
    assert res.value == pytest.approx(_psr_reference(0.1, 0.0, 3.0, 252), rel=1e-12)
    assert hand == pytest.approx(0.9429868610243624, abs=1e-9), "value written by hand"


def test_psr_of_the_fixture_matches_the_reference_formula_CC_02():
    res = MODULE.psr(R)
    sr = float(R.mean() / R.std(ddof=1))                      # per period, not annualized
    expected = _psr_reference(sr, float(R.skew()), float(R.kurt()) + 3.0, N)
    assert res.value == pytest.approx(expected, rel=1e-12)
    assert res.value == pytest.approx(0.5689823758536956, rel=1e-9)


def test_psr_with_skew_minus_half_and_raw_kurtosis_five_CC_02():
    res = registry.call("validation.psr_moments", sr=0.1, skew=-0.5, kurtosis_raw=5, n=252)
    assert res.value == pytest.approx(_psr_reference(0.1, -0.5, 5.0, 252), rel=1e-12)
    assert res.value == pytest.approx(0.9380741923755009, rel=1e-9), "value written by hand"
    defective = _psr_with_kurtosis_subtracted_twice(0.1, -0.5, 5.0, 252)
    assert abs(res.value - defective) > 1e-3, "the defective convention gives a different probability"
    assert defective > res.value, "and it overstates the probability"


def test_nothing_in_the_registry_can_reach_the_defective_functions_CC_02():
    reachable = {s.name for s in registry.list(include_blocked=False)}
    assert not {f"metrics.{n}" for n in BLOCKED} & reachable
    for name in BLOCKED:
        spec = registry.describe(f"metrics.{name}")
        assert spec.blocked and spec.fn is not None
        with pytest.raises(BlockedFunction):
            spec.fn()


def test_annualize_is_refused_on_probabilities_CC_03():
    with pytest.raises(ValueError, match="probabilities are not annualized"):
        MODULE.psr(R, annualize=True)
    with pytest.raises(ValueError, match="probabilities are not annualized"):
        MODULE.reject_annualize(annualize=True)
    MODULE.reject_annualize(annualize=False)
    MODULE.reject_annualize()
    assert MODULE.psr(R, annualize=False) == MODULE.psr(R)
    with pytest.raises(BadInput):
        registry.call("metrics.psr", returns=R, annualize=True)
    with pytest.raises(BadInput):
        MODULE.psr(R, nope=1)


def test_no_probability_function_accepts_annualize_CC_03():
    names = MODULE.probability_functions()
    assert {"metrics.psr", "metrics.prob_momentum"} <= set(names)
    specs = {s.name: s for s in registry.list(include_blocked=True)}
    for name in names:
        assert "annualize" not in specs[name].inputs, name
    for name in BLOCKED:
        assert "annualize" not in specs[f"metrics.{name}"].inputs
    for spec in specs.values():
        if spec.module == "metrics" and ("prob" in spec.name or spec.name.endswith(".psr")):
            assert "annualize" not in spec.inputs, spec.name


@pytest.mark.parametrize("label", sorted(BAD_INPUTS))
def test_psr_of_bad_data_is_none_with_warnings(label):
    res = MODULE.psr(BAD_INPUTS[label])
    assert res.value is None and res.warnings

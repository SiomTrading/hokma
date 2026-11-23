"""Overfitting controls: FM-01..FM-05, FF-010..FF-012, FT-067, CC-04."""
import math

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.validation import _lib, overfit
from hokma.quant import validation

from .conftest import assert_registered


def test_psr_known_case_FM_01():
    """SR annual 1.0, n=1260 daily, skew 0, raw kurtosis 3, benchmark 0 (value written by hand)."""
    sr_period = 1.0 / math.sqrt(252)
    result = overfit.psr_moments(sr=sr_period, skew=0.0, kurtosis_raw=3.0, n=1260)
    assert result.value == pytest.approx(0.9872244174653579, abs=1e-9)
    assert_registered("validation.psr_moments", "FM-01")
    assert_registered("validation.psr", "FM-01")


def test_psr_on_series_uses_raw_kurtosis_FM_01(returns):
    result = validation.psr(returns)
    r = returns
    sr = r.mean() / r.std(ddof=1)
    kurt_raw = r.kurt() + 3.0
    expected = overfit._NORMAL.cdf(sr * math.sqrt(len(r) - 1) / math.sqrt(1 - r.skew() * sr + (kurt_raw - 1) / 4 * sr * sr))
    assert result.value == pytest.approx(expected, abs=1e-12)
    assert result.params["kurtosis_raw"] == pytest.approx(kurt_raw)


def test_psr_matches_reference_FT_067(returns):
    sr, skew, kurt, n = overfit.sharpe_moments(returns)
    reference = _lib.ft_probabilistic_sharpe_ratio(sr, 0.0, skew, kurt, n)
    assert validation.psr(returns).value == pytest.approx(float(reference), abs=1e-9)


@pytest.mark.parametrize("bad", [pd.Series([], dtype=float), pd.Series([0.01] * 40), pd.Series([float("nan")] * 40)])
def test_psr_degenerate_inputs_give_none_FM_01(bad):
    result = validation.psr(bad)
    assert result.value is None and result.warnings


def test_psr_wrong_type_is_bad_input_FM_01():
    with pytest.raises(BadInput):
        validation.psr("not a series")


def test_expected_max_sharpe_known_case_FM_02():
    """N=100 trials, Sharpe variance 0.5 (std sqrt(0.5)): value written by hand."""
    result = registry.call("validation.expected_max_sharpe", n_trials=100, sr_std=math.sqrt(0.5))
    assert result.value == pytest.approx(1.7894064662732079, abs=1e-9)
    assert_registered("validation.expected_max_sharpe", "FM-02")
    assert_registered("validation.expected_max_sharpe", "FF-010")


@pytest.mark.parametrize("n_trials,sr_std", [(1, 0.3), (2, 0.3), (100, 0.5), (1000, 0.02)])
def test_expected_max_sharpe_equals_origin_FF_010(n_trials, sr_std):
    assert overfit.expected_max_sharpe(n_trials, sr_std) == pytest.approx(_lib.ffn_expected_max_sharpe(n_trials, sr_std), abs=1e-9)


def test_expected_max_sharpe_bad_values_FM_02():
    assert registry.call("validation.expected_max_sharpe", n_trials=0, sr_std=0.1).value is None
    assert registry.call("validation.expected_max_sharpe", n_trials=10, sr_std=-1).value is None


def test_dsr_is_psr_at_the_chance_hurdle_FM_03():
    """Known case: sr 0.1, skew -0.5, raw kurtosis 5, n 500, 100 trials, std 0.05 (hand value)."""
    sr0 = overfit.expected_max_sharpe(100, 0.05)
    assert sr0 == pytest.approx(0.12653014466008425, abs=1e-12)
    assert overfit.psr_from_moments(0.1, sr0, -0.5, 5.0, 500) == pytest.approx(0.28243554454446507, abs=1e-9)


def test_dsr_matches_both_references_FF_011_FT_067_CC_04(returns):
    for n_trials, sr_std in [(50, 0.03), (200, 0.05), (1, 0.05)]:
        result = registry.call("validation.dsr_crosscheck", returns=returns, n_trials=n_trials, sr_std=sr_std)
        values = result.value
        assert values["max_abs_gap"] < 1e-9
        assert values["own"] == pytest.approx(values["reference_a"], abs=1e-9)
        assert values["own"] == pytest.approx(values["reference_b"], abs=1e-9)
        assert values["own"] == pytest.approx(validation.dsr(returns, n_trials=n_trials, sr_std=sr_std).value, abs=1e-12)
    for pid in ("FF-011", "FT-067", "CC-04", "FM-03"):
        assert pid in registry.describe("validation.dsr_crosscheck").piece_ids + registry.describe("validation.dsr").piece_ids


def test_dsr_never_exceeds_psr_FM_03(returns):
    assert validation.dsr(returns, n_trials=100, sr_std=0.05).value <= validation.psr(returns).value


def test_dsr_bad_inputs_FM_03(returns):
    assert validation.dsr(returns, n_trials=0, sr_std=0.05).value is None
    assert validation.dsr(pd.Series([0.01] * 50), n_trials=10, sr_std=0.05).value is None
    assert validation.dsr(pd.Series([float("nan")] * 50), n_trials=10, sr_std=0.05).value is None


def _random_blocks(seed: int, rows_per_block: int = 30, n_blocks: int = 16, n_strategies: int = 50) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    return pd.DataFrame(rng.normal(0.0, 0.01, (rows_per_block * n_blocks, n_strategies)))


def test_pbo_random_strategies_is_about_one_half_FM_04():
    value = validation.pbo(_random_blocks(20261122), n_blocks=16).value
    assert value == pytest.approx(0.5, abs=0.1)


def test_pbo_with_a_dominant_strategy_is_small_FM_04():
    matrix = _random_blocks(20261122)
    matrix[0] = matrix[0] + 0.02
    assert validation.pbo(matrix, n_blocks=16).value < 0.1


def test_pbo_equals_the_origin_implementation_FF_012():
    matrix = _random_blocks(11, rows_per_block=20, n_blocks=8, n_strategies=12)
    assert validation.pbo(matrix, n_blocks=8).value == pytest.approx(_lib.ffn_prob_backtest_overfitting(matrix, n_blocks=8), abs=1e-9)


def test_pbo_exists_only_once_in_the_registry_CC_04():
    names = [s.name for s in registry.list(include_blocked=True) if "pbo" in s.name.lower() or "overfitting" in s.name.lower()]
    assert names == ["validation.pbo"]
    assert_registered("validation.pbo", "FF-012")
    assert_registered("validation.pbo", "CC-04")


def test_pbo_bad_inputs_FM_04():
    with pytest.raises(BadInput):
        validation.pbo([1, 2, 3])
    assert validation.pbo(pd.DataFrame({"a": [0.1] * 3}), n_blocks=16).value is None
    assert validation.pbo(_random_blocks(1), n_blocks=15).value is None
    assert validation.pbo(pd.DataFrame(np.nan, index=range(40), columns=["a", "b"]), n_blocks=4).value is None


def test_autocorrelation_penalty_is_one_without_autocorrelation_FM_05():
    assert overfit.autocorr_penalty_from_rho([0.0, 0.0, 0.0, 0.0]) == 1.0
    rng = np.random.default_rng(3)
    white = pd.Series(rng.normal(0, 0.01, 20000))
    assert registry.call("validation.autocorr_penalty", returns=white, lags=5).value == pytest.approx(1.0, abs=0.05)


def test_autocorrelation_penalty_ar1_matches_the_written_formula_FM_05():
    rho, n, lags = 0.3, 20000, 5
    rng = np.random.default_rng(5)
    noise = rng.normal(0, 0.01, n)
    x = np.zeros(n)
    for t in range(1, n):
        x[t] = rho * x[t - 1] + noise[t]
    series = pd.Series(x)
    centered = x - x.mean()
    acf = [float(centered[:-k] @ centered[k:] / (centered @ centered)) for k in range(1, lags + 1)]
    expected = math.sqrt(1 + 2 * sum((1 - k / (lags + 1)) * acf[k - 1] for k in range(1, lags + 1)))
    result = registry.call("validation.autocorr_penalty", returns=series, lags=lags)
    assert result.value == pytest.approx(expected, abs=1e-9)
    theoretical = math.sqrt(1 + 2 * sum((1 - k / (lags + 1)) * rho ** k for k in range(1, lags + 1)))
    assert result.value == pytest.approx(theoretical, abs=0.03)
    assert_registered("validation.autocorr_penalty", "FM-05")


@pytest.mark.parametrize("bad", [pd.Series([], dtype=float), pd.Series([0.02] * 30), pd.Series([float("nan")] * 30)])
def test_autocorrelation_penalty_degenerate_FM_05(bad):
    result = registry.call("validation.autocorr_penalty", returns=bad)
    assert result.value is None and result.warnings

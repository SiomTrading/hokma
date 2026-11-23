"""Stationary bootstrap and optimal block length: SK-113."""
import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.validation import _lib, bootstrap

from .conftest import assert_registered


def test_optimal_block_size_equals_origin_SK_113(returns):
    result = registry.call("validation.optimal_block_size", returns=returns)
    assert result.value == pytest.approx(float(_lib.optimal_block_size(returns.to_numpy())), abs=1e-12)
    assert 1.0 <= result.value <= 3 * np.sqrt(len(returns))
    assert_registered("validation.optimal_block_size", "SK-113")


def test_bootstrap_sharpe_equals_origin_with_fixed_seed_SK_113(returns):
    result = registry.call("validation.bootstrap_sharpe", returns=returns, n_samples=300, block_size=5.0, seed=11)
    samples = _lib.stationary_bootstrap(returns.to_numpy().reshape(-1, 1), 300, 5.0, 11)[:, :, 0]
    sharpes = samples.mean(axis=1) / samples.std(axis=1, ddof=1) * np.sqrt(252)
    assert result.value["mean"] == pytest.approx(float(sharpes.mean()), abs=1e-9)
    assert result.value["p05"] == pytest.approx(float(np.quantile(sharpes, 0.05)), abs=1e-9)
    assert result.value["prob_positive"] == pytest.approx(float((sharpes > 0).mean()), abs=1e-12)
    again = registry.call("validation.bootstrap_sharpe", returns=returns, n_samples=300, block_size=5.0, seed=11)
    assert again.value == result.value
    other = registry.call("validation.bootstrap_sharpe", returns=returns, n_samples=300, block_size=5.0, seed=12)
    assert other.value != result.value
    assert_registered("validation.bootstrap_sharpe", "SK-113")


def test_bootstrap_centres_on_the_sample_sharpe_SK_113(returns):
    sample = returns.mean() / returns.std(ddof=1) * np.sqrt(252)
    value = registry.call("validation.bootstrap_sharpe", returns=returns, n_samples=1000, seed=3).value
    assert value["p05"] < sample < value["p95"]
    assert bootstrap.stationary_bootstrap is _lib.stationary_bootstrap


@pytest.mark.parametrize("bad", [pd.Series([], dtype=float), pd.Series([0.01] * 60), pd.Series([float("nan")] * 60)])
@pytest.mark.parametrize("name", ["validation.bootstrap_sharpe", "validation.optimal_block_size"])
def test_degenerate_inputs_give_none_SK_113(name, bad):
    result = registry.call(name, returns=bad)
    assert result.value is None and result.warnings

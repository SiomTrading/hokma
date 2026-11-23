"""SK-115 (baseline weights) and SK-116 (cross-sectional correlation and rank)."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.portfolio import _lib

from .conftest import call


def test_sk115_random_weights_sum_to_one_have_the_zeros_asked_and_repeat_with_the_seed():
    one = call("portfolio.rand_weights", n_assets=6, zeros=2, seed=3).value["weights"]
    again = call("portfolio.rand_weights", n_assets=6, zeros=2, seed=3).value["weights"]
    assert one == again and sum(one) == pytest.approx(1.0, abs=1e-12) and sum(1 for w in one if w == 0.0) == 2
    assert np.allclose(one, _lib.sk_stats.rand_weights(6, zeros=2, seed=3), atol=1e-12, rtol=0)
    assert call("portfolio.rand_weights", n_assets=6, seed=4).value["weights"] != one
    assert registry.describe("portfolio.rand_weights").piece_ids == ("SK-115",)


def test_sk115_dirichlet_weights_are_seeded_and_do_not_touch_the_global_state():
    before = np.random.get_state()[1].copy()
    one = call("portfolio.rand_weights_dirichlet", n_assets=4, seed=11).value["weights"]
    two = call("portfolio.rand_weights_dirichlet", n_assets=4, seed=11).value["weights"]
    assert one == two and sum(one) == pytest.approx(1.0, abs=1e-12) and all(w > 0 for w in one)
    assert np.array_equal(before, np.random.get_state()[1])
    state = np.random.get_state()
    np.random.seed(11)
    expected = _lib.sk_stats.rand_weights_dirichlet(4)
    np.random.set_state(state)
    assert np.allclose(one, expected, atol=1e-15, rtol=0)


def test_sk115_inverse_volatility_by_hand():
    cov = pd.DataFrame([[0.04, 0.0], [0.0, 0.01]], columns=["a", "b"], index=["a", "b"])
    result = call("portfolio.inverse_volatility_weights", covariance=cov)
    # volatilities 0.2 and 0.1: 1/sigma is 5 and 10, normalized to 1/3 and 2/3
    assert result.value["weights"] == {"a": pytest.approx(1.0 / 3.0, abs=1e-12), "b": pytest.approx(2.0 / 3.0, abs=1e-12)}
    assert np.allclose(list(result.value["weights"].values()), _lib.sk_stats.inverse_volatility_weights(cov.to_numpy()), atol=1e-14, rtol=0)


def test_sk115_empty_nan_and_out_of_domain():
    assert call("portfolio.rand_weights", n_assets=3, zeros=3).value is None
    assert call("portfolio.rand_weights", n_assets=0).value is None
    assert call("portfolio.rand_weights", n_assets=3, zeros=-1).value is None
    with pytest.raises(BadInput):
        call("portfolio.rand_weights", n_assets=3, seed=-1)
    with pytest.raises(BadInput):
        call("portfolio.rand_weights_dirichlet", n_assets=3, seed=True)
    with pytest.raises(BadInput):
        call("portfolio.rand_weights", n_assets=10 ** 7)                      # above the cap of the registry
    flat = pd.DataFrame([[0.0, 0.0], [0.0, 0.0]], columns=["a", "b"])
    assert call("portfolio.inverse_volatility_weights", covariance=flat).value is None
    assert call("portfolio.inverse_volatility_weights", covariance=flat.iloc[:0]).value is None
    assert call("portfolio.inverse_volatility_weights", covariance=pd.DataFrame([[-1.0, 0.0], [0.0, 1.0]], columns=["a", "b"])).value is None
    assert call("portfolio.inverse_volatility_weights", covariance=pd.DataFrame([[np.nan, 0.0], [0.0, 1.0]], columns=["a", "b"])).value is None
    with pytest.raises(BadInput):
        call("portfolio.inverse_volatility_weights", covariance=pd.DataFrame([[1.0, 0.0]], columns=["a", "b"]))


def test_sk116_weighted_pearson_by_hand():
    result = call("portfolio.cs_pearson_correlation", a=[1, 2, 3], b=[1, 3, 2], weights=[1, 1, 2])
    # weighted means 2.25 and 2.0; covariance 1.0; variances 2.75 and 2.0: 1 / sqrt(5.5)
    assert result.value["ic"][0] == pytest.approx(1.0 / math.sqrt(5.5), abs=1e-12) and result.piece_ids == ("SK-116",)
    assert call("portfolio.cs_pearson_correlation", a=[1, 2, 3], b=[3, 2, 1]).value["ic"] == [pytest.approx(-1.0)]
    assert call("portfolio.cs_pearson_correlation", a=[1, 2, 3], b=[2, 4, 6]).value["mean"] == pytest.approx(1.0)


def test_sk116_spearman_uses_the_ranks_and_the_rank_has_ties_by_hand():
    # b is an increasing function of a, so the rank correlation is 1 even though the Pearson one is below 1
    a, b = [1, 2, 3, 4], [1, 2, 3, 100]
    assert call("portfolio.cs_spearman_correlation", a=a, b=b).value["mean"] == pytest.approx(1.0, abs=1e-12)
    assert call("portfolio.cs_pearson_correlation", a=a, b=b).value["mean"] < 0.99
    ranks = call("portfolio.cs_rank", a=[3, 1, 2, 2])
    assert ranks.value["ranks"] == [[4.0, 1.0, 2.5, 2.5]]


def test_sk116_a_panel_gives_one_coefficient_per_date_and_matches_the_library(panel):
    forecast, outcome = panel.iloc[:-1], panel.shift(-1).iloc[:-1]
    result = call("portfolio.cs_pearson_correlation", a=forecast, b=outcome)
    direct = _lib.sk_stats.cs_pearson_correlation(forecast.to_numpy(), outcome.to_numpy(), axis=1)
    assert np.allclose(result.value["ic"], direct, atol=1e-12, rtol=0) and len(result.value["ic"]) == len(forecast)
    assert result.value["mean"] == pytest.approx(float(np.mean(direct)), abs=1e-12)
    over_time = call("portfolio.cs_spearman_correlation", a=forecast, b=outcome, axis="time")
    assert np.allclose(over_time.value["ic"], _lib.sk_stats.cs_spearman_correlation(forecast.to_numpy(), outcome.to_numpy(), axis=0), atol=1e-12, rtol=0)
    assert over_time.value["labels"] == list(panel.columns)
    ranked = call("portfolio.cs_rank", a=forecast, axis="time")
    assert np.allclose(np.array(ranked.value["ranks"]), _lib.sk_stats.cs_rank(forecast.to_numpy(), axis=0), atol=0, rtol=0)


def test_sk116_empty_nan_and_wrong_shapes():
    assert call("portfolio.cs_pearson_correlation", a=[np.nan, 2, 3], b=[3, 2, 1]).value is None             # fewer than min_count pairs
    assert call("portfolio.cs_pearson_correlation", a=[1, 2, 3], b=[1, 2, 3], min_count=4).value is None
    assert call("portfolio.cs_pearson_correlation", a=[1, 1, 1], b=[1, 2, 3]).value is None                  # a flat series has no correlation
    assert call("portfolio.cs_spearman_correlation", a=pd.DataFrame(), b=pd.DataFrame()).value is None
    assert call("portfolio.cs_pearson_correlation", a=[1, 2, 3], b=[1, 2, 3], axis="diagonal").value is None
    assert call("portfolio.cs_rank", a=[np.nan, np.nan]).value is None
    assert call("portfolio.cs_rank", a=pd.DataFrame()).value is None
    with pytest.raises(BadInput):
        call("portfolio.cs_pearson_correlation", a=[1, 2, 3], b=[1, 2])
    with pytest.raises(BadInput):
        call("portfolio.cs_pearson_correlation", a=[1, 2, 3], b=[1, 2, 3], weights=[1, 1])
    with pytest.raises(BadInput):
        call("portfolio.cs_pearson_correlation", a="abc", b=[1, 2, 3])

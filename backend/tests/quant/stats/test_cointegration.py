"""Cointegration. Row FT-081 (and the pairs checks of ECT-015)."""
import math

import numpy as np
import pandas as pd
import pytest

from hokma.quant.errors import BadInput
from hokma.quant.stats import _econ as econ

from .conftest import call, piece_ids, rng


@pytest.fixture(scope="module")
def pair():
    g = rng(61)
    x = np.cumsum(g.standard_normal(600)) + 50.0
    y = 2.0 + 0.8 * x + g.standard_normal(600) * 0.5
    return pd.Series(y), pd.Series(x)


def test_engle_granger_FT_081_hand_values(pair):
    y, x = pair
    res = call("stats.engle_granger", y=y, x=x, n_tested=1)
    # hand values: the cointegrating regression by least squares, and the published residual based critical values
    design = np.column_stack([np.ones(600), x.to_numpy()])
    beta = np.linalg.lstsq(design, y.to_numpy(), rcond=None)[0]
    assert res.value["intercept"] == pytest.approx(beta[0], abs=1e-10) and res.value["hedge_ratio"] == pytest.approx(beta[1], abs=1e-10)
    assert res.value["critical_values"] == {"1%": -3.89644, "5%": -3.33613, "10%": -3.04445}
    spread = y.to_numpy() - design @ beta
    core = econ.adf_core(spread, "n", None)
    assert res.value["statistic"] == pytest.approx(core["statistic"], abs=1e-10)
    assert res.value["cointegrated_5pct"] is True
    assert res.value["hedge_ratio"] == pytest.approx(0.8, abs=0.02)
    phi = np.linalg.lstsq(np.column_stack([np.ones(599), spread[:-1]]), spread[1:], rcond=None)[0][1]
    assert res.value["spread_half_life"] == pytest.approx(math.log(2) / -math.log(phi), abs=1e-9)
    assert "FT-081" in piece_ids("stats.engle_granger")
    independent = pd.Series(rng(62).standard_normal(600).cumsum())
    assert call("stats.engle_granger", y=independent, x=x).value["cointegrated_5pct"] is False


def test_engle_granger_FT_081_traps_are_reported(pair):
    y, x = pair
    many = call("stats.engle_granger", y=y, x=x, n_tested=4850)
    assert any("4850 pairs tested" in w and "chance" in w for w in many.warnings)
    assert many.value["expected_false_positives_5pct"] == pytest.approx(242.5)
    logs = call("stats.engle_granger", y=np.log(y), x=np.log(x), ratio_basis="log")
    assert any("elasticity" in w for w in logs.warnings)
    three = call("stats.engle_granger", y=y, x=pd.DataFrame({"a": x, "b": rng(1).standard_normal(600).cumsum()}))
    assert three.value["critical_values"]["5%"] == pytest.approx(-3.74066)
    assert len(three.value["hedge_ratio"]) == 2


def test_engle_granger_FT_081_empty_nan_and_out_of_domain(pair):
    y, x = pair
    assert call("stats.engle_granger", y=pd.Series([], dtype=float), x=pd.Series([], dtype=float)).value is None
    assert call("stats.engle_granger", y=pd.Series(np.ones(100)), x=x.iloc[:100]).value is None
    assert call("stats.engle_granger", y=y.iloc[:10], x=x.iloc[:10]).value is None
    assert call("stats.engle_granger", y=y, x=x, ratio_basis="dollars").value is None
    assert call("stats.engle_granger", y=y, x=x, n_tested=0).value is None
    nan = y.copy()
    nan.iloc[3] = np.nan
    assert any("dropped" in w for w in call("stats.engle_granger", y=nan, x=x).warnings)
    with pytest.raises(BadInput):
        call("stats.engle_granger", y=y.iloc[:50], x=x)
    with pytest.raises(BadInput):
        call("stats.engle_granger", y=y, x=x, lags=1.5)


def test_johansen_FT_081_eigenvalues_rank_and_critical_values(pair):
    y, x = pair
    data = pd.DataFrame({"y": y, "x": x})
    res = call("stats.johansen", data=data, k_ar_diff=1).value
    # hand values: the eigenvalues of S11^-1 S10 S00^-1 S01 from plain least squares residuals
    arr = data.to_numpy()
    dy = np.diff(arr, axis=0)
    t = len(dy) - 1
    z = np.column_stack([np.ones(t), dy[:-1]])
    r0 = dy[1:] - z @ np.linalg.lstsq(z, dy[1:], rcond=None)[0]
    r1 = arr[1:-1] - z @ np.linalg.lstsq(z, arr[1:-1], rcond=None)[0]
    s00, s11, s01 = r0.T @ r0 / t, r1.T @ r1 / t, r0.T @ r1 / t
    eig = np.sort(np.linalg.eigvals(np.linalg.inv(s11) @ s01.T @ np.linalg.inv(s00) @ s01).real)[::-1]
    assert res["eigenvalues"] == pytest.approx(list(eig), abs=1e-10)
    assert res["trace"][0] == pytest.approx(-t * float(np.sum(np.log(1 - eig))), abs=1e-8)
    assert res["max_eigenvalue"][1] == pytest.approx(-t * math.log(1 - eig[1]), abs=1e-8)
    assert res["rank_5pct"] == 1
    assert res["cointegrating_vectors"][0][1] == pytest.approx(-0.8, abs=0.05)
    unrelated = pd.DataFrame({"y": rng(71).standard_normal(600).cumsum(), "x": rng(72).standard_normal(600).cumsum()})
    assert call("stats.johansen", data=unrelated).value["rank_5pct"] == 0
    again = call("stats.johansen", data=data, k_ar_diff=1).value
    assert again["trace_critical"] == res["trace_critical"]                       # fixed seed: the critical values repeat
    other = call("stats.johansen", data=data, k_ar_diff=1, seed=5).value
    assert other["trace_critical"] != res["trace_critical"]
    assert "FT-081" in piece_ids("stats.johansen")


def test_johansen_FT_081_simulated_critical_values_match_known_limits():
    # one common trend with a strong drift: the trace statistic is close to a chi-square with one degree of freedom (5% value 3.84)
    drifting = econ.johansen_critical_values(1, 600, 1, 500, 0, 2.0)
    assert drifting["trace"][0, 1] == pytest.approx(3.84, abs=0.7)
    # without drift the constant is estimated out of the lagged levels and the values are larger
    flat = econ.johansen_critical_values(1, 600, 1, 500, 0, 0.0)
    assert flat["trace"][0, 1] > 6.0
    assert flat["trace"][0, 0] < flat["trace"][0, 1] < flat["trace"][0, 2]
    assert econ.johansen_critical_values(2, 400, 1, 200, 0, 0.0)["trace"][0, 1] > econ.johansen_critical_values(1, 400, 1, 200, 0, 0.0)["trace"][0, 1]


def test_johansen_FT_081_empty_nan_and_out_of_domain(pair):
    y, x = pair
    data = pd.DataFrame({"y": y, "x": x})
    assert call("stats.johansen", data=data.iloc[:10]).value is None
    assert call("stats.johansen", data=pd.DataFrame({"y": np.ones(100), "x": x.iloc[:100]})).value is None
    assert call("stats.johansen", data=data, k_ar_diff=-1).value is None
    assert call("stats.johansen", data=data, n_simulations=10).value is None
    assert call("stats.johansen", data=data, drift="up").value is None
    assert call("stats.johansen", data=pd.DataFrame(np.random.default_rng(1).standard_normal((100, 7)).cumsum(axis=0))).value is None
    nan = data.copy()
    nan.iloc[5, 0] = np.nan
    assert any("dropped" in w for w in call("stats.johansen", data=nan).warnings)
    with pytest.raises(BadInput):
        call("stats.johansen", data=data["y"])
    with pytest.raises(BadInput):
        call("stats.johansen", data=data, n_simulations=500000)
    with pytest.raises(BadInput):
        call("stats.johansen", data=data, k_ar_diff=1.5)
    with pytest.raises(BadInput):
        call("stats.johansen", data=data, k_ar_diff=11)


def test_hedge_amounts_FT_081_log_ratio_is_not_a_share_ratio():
    # hand values: 1,000 shares of A at 100 is 100,000 dollars
    log_basis = call("stats.hedge_amounts", price_a=100.0, price_b=50.0, shares_a=1000, ratio=0.89, ratio_basis="log").value
    assert log_basis["dollars_b"] == pytest.approx(89000.0) and log_basis["shares_b"] == pytest.approx(1780.0)
    assert log_basis["dollar_neutral"] is False and log_basis["dollar_imbalance"] == pytest.approx(11000.0)
    level = call("stats.hedge_amounts", price_a=100.0, price_b=50.0, shares_a=1000, ratio=0.89, ratio_basis="level").value
    assert level["shares_b"] == pytest.approx(890.0) and level["dollars_b"] == pytest.approx(44500.0)
    neutral = call("stats.hedge_amounts", price_a=100.0, price_b=50.0, shares_a=1000, ratio=1.0, ratio_basis="log").value
    assert neutral["dollar_neutral"] is True
    assert call("stats.hedge_amounts", price_a=0.0, price_b=50.0, shares_a=10, ratio=1.0).value is None
    assert call("stats.hedge_amounts", price_a=100.0, price_b=50.0, shares_a=10, ratio=1.0, ratio_basis="x").value is None
    assert call("stats.hedge_amounts", price_a=100.0, price_b=50.0, shares_a=10, ratio=float("nan")).value is None
    assert call("stats.hedge_amounts", price_a=-5.0, price_b=50.0, shares_a=10, ratio=1.0).value is None
    with pytest.raises(BadInput):
        call("stats.hedge_amounts", price_a="100", price_b=50.0, shares_a=10, ratio=1.0)

"""SK-051, SK-053, SK-055, SK-056: variance, covariance from implied volatility, Black-Litterman and entropy pooling."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.portfolio import _lib

from .conftest import call

SMALL = pd.DataFrame({"a": [0.01, -0.01, 0.01, -0.01], "b": [0.02, 0.02, -0.02, -0.02]})


def test_sk053_empirical_variance_by_hand():
    result = call("portfolio.variance", returns=SMALL)
    # sum of squares / (n - 1): 4e-4 / 3 and 16e-4 / 3
    assert result.value["variance"]["a"] == pytest.approx(4e-4 / 3, abs=1e-15)
    assert result.value["variance"]["b"] == pytest.approx(16e-4 / 3, abs=1e-15)
    assert result.value["standard_deviation"]["a"] == pytest.approx((4e-4 / 3) ** 0.5, abs=1e-15)
    assert result.piece_ids == ("SK-053",)


def test_sk053_empirical_and_exponentially_weighted_match_the_library(panel):
    emp = call("portfolio.variance", returns=panel)
    assert np.allclose(list(emp.value["variance"].values()), _lib.sk_moments.EmpiricalVariance().fit(panel).variance_, atol=1e-8, rtol=0)
    ew = call("portfolio.variance", returns=panel, method="ew", half_life=20)
    assert np.allclose(list(ew.value["variance"].values()), _lib.sk_moments.EWVariance(half_life=20).fit(panel).variance_, atol=1e-8, rtol=0)
    windowed = call("portfolio.variance", returns=panel, window_size=100)
    assert np.allclose(list(windowed.value["variance"].values()), panel.iloc[-100:].var(ddof=1).to_numpy(), atol=1e-12, rtol=0)


def test_sk053_empty_short_and_out_of_domain_give_none(panel):
    assert call("portfolio.variance", returns=panel.iloc[:0]).value is None
    short = call("portfolio.variance", returns=SMALL, method="ew", half_life=20)
    assert short.value is None and "not enough observations" in short.warnings[0]
    assert call("portfolio.variance", returns=panel, method="magic").value is None
    assert call("portfolio.variance", returns=panel, method="ew", half_life=0).value is None
    nan = panel.copy()
    nan.iloc[:, 0] = np.nan
    assert call("portfolio.variance", returns=nan).value is None
    with pytest.raises(BadInput):
        call("portfolio.variance", returns="x")


def _implied(panel: pd.DataFrame, level: float = 0.2) -> pd.DataFrame:
    return pd.DataFrame({c: np.full(len(panel), level) for c in panel.columns}, index=panel.index)


def test_sk051_correlation_is_the_realized_one_of_the_whole_history(panel):
    result = call("portfolio.implied_covariance", returns=panel, implied_vol=_implied(panel), window_size=20)
    realized = np.corrcoef(panel.to_numpy().T)
    names = list(panel.columns)
    got = np.array([[result.value["correlation"][a][b] for b in names] for a in names])
    assert np.allclose(got, realized, atol=1e-6, rtol=0)
    assert result.piece_ids == ("SK-051",)


def test_sk051_matches_the_library_and_the_volatility_is_the_square_root_of_the_diagonal(panel):
    result = call("portfolio.implied_covariance", returns=panel, implied_vol=_implied(panel), window_size=20)
    direct = _lib.sk_moments.ImpliedCovariance(window_size=20).fit(panel, implied_vol=_implied(panel)).covariance_
    names = list(panel.columns)
    got = np.array([[result.value["covariance"][a][b] for b in names] for a in names])
    assert np.allclose(got, direct, atol=1e-8, rtol=0)
    assert np.allclose(list(result.value["volatility"].values()), np.sqrt(np.diag(direct)), atol=1e-12, rtol=0)


def test_sk051_bad_input_and_short_history(panel):
    other = _implied(panel).rename(columns={"AAA": "ZZZ"})
    with pytest.raises(BadInput):
        call("portfolio.implied_covariance", returns=panel, implied_vol=other)
    with pytest.raises(BadInput):
        call("portfolio.implied_covariance", returns=panel, implied_vol=_implied(panel).iloc[:10])
    assert call("portfolio.implied_covariance", returns=panel.iloc[:10], implied_vol=_implied(panel.iloc[:10])).value is None
    assert call("portfolio.implied_covariance", returns=panel, implied_vol=_implied(panel), window_size=1).value is None
    assert call("portfolio.implied_covariance", returns=panel, implied_vol=_implied(panel) * np.nan).value is None


def test_sk055_a_certain_view_sets_the_mean_exactly(panel):
    one = panel[["AAA"]]
    result = call("portfolio.black_litterman", returns=one, views=[{"assets": {"AAA": 1}, "value": 0.002}], view_confidences=[1.0])
    assert result.value["mu"]["AAA"] == pytest.approx(0.002, abs=1e-12)
    assert result.piece_ids == ("SK-055",) and result.value["views"] == ["1.0 * AAA == 0.002"]


def test_sk055_matches_the_library_for_a_spread_view_and_partial_confidence(panel):
    views = [{"assets": {"AAA": 1, "BBB": -1}, "value": 0.001}]
    result = call("portfolio.black_litterman", returns=panel, views=views, view_confidences=[0.9], tau=0.05)
    direct = _lib.sk_prior.BlackLitterman(views=["1.0 * AAA - 1.0 * BBB == 0.001"], view_confidences=[0.9], tau=0.05).fit(panel).return_distribution_
    assert np.allclose(list(result.value["mu"].values()), direct.mu, atol=1e-8, rtol=0)
    cov = np.array([[result.value["covariance"][a][b] for b in panel.columns] for a in panel.columns])
    assert np.allclose(cov, direct.covariance, atol=1e-8, rtol=0)
    half = call("portfolio.black_litterman", returns=panel[["AAA"]], views=[{"assets": {"AAA": 1}, "value": 0.002}], view_confidences=[0.5])
    assert panel["AAA"].mean() < half.value["mu"]["AAA"] < 0.002


def test_sk055_out_of_domain_views_give_none_and_wrong_shape_raises(panel):
    assert call("portfolio.black_litterman", returns=panel, views=[]).value is None
    assert call("portfolio.black_litterman", returns=panel, views=[{"assets": {"AAA": 1}, "relation": ">=", "value": 0.1}]).value is None
    assert call("portfolio.black_litterman", returns=panel, views=[{"assets": {"AAA": 0}, "value": 0.1}]).value is None
    assert call("portfolio.black_litterman", returns=panel, views=[{"assets": {"AAA": 1}, "value": float("nan")}]).value is None
    assert call("portfolio.black_litterman", returns=panel.iloc[:0], views=[{"assets": {"AAA": 1}, "value": 0.1}]).value is None
    with pytest.raises(BadInput):
        call("portfolio.black_litterman", returns=panel, views=[{"assets": {"ZZZ": 1}, "value": 0.1}])
    with pytest.raises(BadInput):
        call("portfolio.black_litterman", returns=panel, views=[{"assets": {"AAA": 1}, "value": 0.1}], view_confidences=[0.5, 0.5])
    with pytest.raises(BadInput):
        call("portfolio.black_litterman", returns=panel, views=["AAA == 1"])


def test_sk056_a_mean_view_is_met_exactly_and_costs_entropy(panel):
    view = {"assets": {"AAA": 0.5, "BBB": 0.5}, "value": 0.001}
    result = call("portfolio.entropy_pooling", returns=panel, mean_views=[view])
    mu = result.value["mu"]
    assert 0.5 * mu["AAA"] + 0.5 * mu["BBB"] == pytest.approx(0.001, abs=1e-8)       # the view, by hand
    assert result.value["relative_entropy"] > 0 and result.value["effective_number_of_scenarios"] < len(panel)
    direct = _lib.sk_prior.EntropyPooling(mean_views=["0.5 * AAA + 0.5 * BBB == 0.001"]).fit(panel)
    assert np.allclose(list(mu.values()), direct.return_distribution_.mu, atol=1e-8, rtol=0)
    assert result.piece_ids == ("SK-056",)


def test_sk056_an_inequality_that_binds_and_a_variance_view(panel):
    result = call("portfolio.entropy_pooling", returns=panel, mean_views=[{"assets": {"AAA": 1}, "relation": ">=", "value": 0.001}])
    assert result.value["mu"]["AAA"] == pytest.approx(0.001, abs=1e-6)
    slack = call("portfolio.entropy_pooling", returns=panel, mean_views=[{"assets": {"AAA": 1}, "relation": "<=", "value": 0.5}])
    assert slack.value["relative_entropy"] == pytest.approx(0.0, abs=1e-6)
    both = call("portfolio.entropy_pooling", returns=panel, mean_views=[{"assets": {"AAA": 1}, "value": 0.001}],
                variance_views=[{"assets": {"BBB": 1}, "value": 1e-4}])
    assert both.value["covariance"]["BBB"]["BBB"] == pytest.approx(1e-4, abs=3e-6)
    assert both.value["mu"]["AAA"] == pytest.approx(0.001, abs=1e-8)


def test_sk056_a_view_the_solver_cannot_meet_gives_none_with_the_reason(panel):
    result = call("portfolio.entropy_pooling", returns=panel, variance_views=[{"assets": {"BBB": 1}, "value": 1e-4}])
    assert result.value is None and "SolverError" in result.warnings[0]


def test_sk056_empty_and_out_of_domain_give_none(panel):
    assert call("portfolio.entropy_pooling", returns=panel).value is None
    assert call("portfolio.entropy_pooling", returns=panel, mean_views=[{"assets": {"AAA": 1}, "relation": "!=", "value": 0.1}]).value is None
    assert call("portfolio.entropy_pooling", returns=panel.iloc[:0], mean_views=[{"assets": {"AAA": 1}, "value": 0.1}]).value is None
    with pytest.raises(BadInput):
        call("portfolio.entropy_pooling", returns=panel, mean_views="AAA == 1")
    assert {"portfolio.variance", "portfolio.implied_covariance", "portfolio.black_litterman", "portfolio.entropy_pooling"} <= {
        s.name for s in registry.list("portfolio")}

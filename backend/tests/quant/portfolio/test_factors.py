"""SK-058, SK-059, SK-060: the factor model container, factors by regression of series and factors from characteristics."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.portfolio import _lib
from hokma.quant.portfolio.factors import FactorModel

from .conftest import call


def _noiseless(rows: int = 200) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(1)
    f = pd.DataFrame(rng.normal(0, 0.01, (rows, 2)), columns=["f1", "f2"])
    x = pd.DataFrame({"a": 0.001 + 1.0 * f.f1 + 0.5 * f.f2, "b": -0.002 + 2.0 * f.f1 - 0.3 * f.f2})
    return x, f


def test_sk058_the_container_gives_the_implied_mean_and_covariance_by_hand():
    result = call("portfolio.factor_model", loadings={"a": {"f": 1.0}, "b": {"f": 2.0}}, factor_mu={"f": 0.01},
                  factor_covariance={"f": {"f": 0.04}}, idio_variances={"a": 0.01, "b": 0.02})
    cov = result.value["implied_covariance"]
    # B F B' + D: a-a 1 * 0.04 * 1 + 0.01, a-b 1 * 0.04 * 2, b-b 2 * 0.04 * 2 + 0.02
    assert cov["a"]["a"] == pytest.approx(0.05) and cov["a"]["b"] == pytest.approx(0.08) and cov["b"]["b"] == pytest.approx(0.18)
    assert result.value["implied_mu"] == {"a": pytest.approx(0.01), "b": pytest.approx(0.02)}
    assert result.value["loadings"]["b"]["f"] == 2.0 and result.piece_ids == ("SK-058",)
    with_alpha = call("portfolio.factor_model", loadings={"a": {"f": 1.0}, "b": {"f": 2.0}}, factor_mu={"f": 0.01},
                      factor_covariance={"f": {"f": 0.04}}, intercepts={"a": 0.001, "b": -0.002})
    assert with_alpha.value["implied_mu"] == {"a": pytest.approx(0.011), "b": pytest.approx(0.018)} and with_alpha.value["idio_variances"] is None


def test_sk058_the_container_is_the_library_container_underneath():
    model = FactorModel(("a", "b"), ("f",), np.array([[1.0], [2.0]]), np.array([0.01]), np.array([[0.04]]), np.array([0.01, 0.02]))
    assert model._library.loading_matrix.tolist() == [[1.0], [2.0]]      # type: ignore[attr-defined]
    assert np.allclose(model.implied_covariance(), [[0.05, 0.08], [0.08, 0.18]])


def test_sk058_out_of_domain_parts_give_none_and_wrong_shapes_raise():
    base = {"loadings": {"a": {"f": 1.0}}, "factor_mu": {"f": 0.01}}
    assert call("portfolio.factor_model", **base, factor_covariance={"f": {"f": -0.04}}).value is None          # not positive semidefinite
    assert call("portfolio.factor_model", **base, factor_covariance={"f": {"f": 0.04}}, idio_variances={"a": -1.0}).value is None
    assert call("portfolio.factor_model", loadings={}, factor_mu={"f": 0.01}, factor_covariance={"f": {"f": 0.04}}).value is None
    assert call("portfolio.factor_model", **base, factor_covariance={"f": {"f": float("nan")}}).value is None
    with pytest.raises(BadInput):
        call("portfolio.factor_model", **base, factor_covariance={"g": {"g": 0.04}})
    with pytest.raises(BadInput):
        call("portfolio.factor_model", **base, factor_covariance={"f": {"f": 0.04}}, idio_variances={"zzz": 0.1})


def test_sk059_exact_loadings_are_recovered_when_there_is_no_noise():
    x, f = _noiseless()
    result = call("portfolio.time_series_factor_model", returns=x, factors=f)
    load = result.value["loadings"]
    assert load["a"]["f1"] == pytest.approx(1.0, abs=1e-10) and load["a"]["f2"] == pytest.approx(0.5, abs=1e-10)
    assert load["b"]["f1"] == pytest.approx(2.0, abs=1e-10) and load["b"]["f2"] == pytest.approx(-0.3, abs=1e-10)
    # the implied mean (loadings x premia + intercept) is the sample mean of each asset
    assert result.value["implied_mu"]["a"] == pytest.approx(x["a"].mean(), abs=1e-12)
    assert result.value["implied_mu"]["b"] == pytest.approx(x["b"].mean(), abs=1e-12)
    assert result.piece_ids == ("SK-059",)


def test_sk059_matches_the_library_and_the_implied_covariance_is_its_covariance(panel):
    x, f = panel[["AAA", "BBB", "CCC"]], panel[["DDD", "EEE"]]
    result = call("portfolio.time_series_factor_model", returns=x, factors=f)
    est = _lib.sk_prior.TimeSeriesFactorModel(loading_matrix_estimator=_lib.sk_prior.LoadingMatrixRegression(
        linear_regressor=_lib.sk_linear_model.LinearRegression())).fit(x, factors=f)
    rd = est.return_distribution_
    got = np.array([[result.value["loadings"][a][k] for k in f.columns] for a in x.columns])
    assert np.allclose(got, rd.factor_model.loading_matrix, atol=1e-8, rtol=0)
    cov = np.array([[result.value["implied_covariance"][a][b] for b in x.columns] for a in x.columns])
    assert np.allclose(cov, rd.covariance, atol=1e-8, rtol=0)
    assert np.allclose(list(result.value["mu"].values()), rd.mu, atol=1e-12, rtol=0)


def test_sk059_ridge_shrinks_the_loadings_and_unknown_regressor_gives_none(panel):
    x, f = panel[["AAA", "BBB"]], panel[["DDD", "EEE"]]
    ols = call("portfolio.time_series_factor_model", returns=x, factors=f).value["loadings"]
    ridge = call("portfolio.time_series_factor_model", returns=x, factors=f, regressor="ridge", alpha=1e-3).value["loadings"]
    assert sum(abs(v) for row in ridge.values() for v in row.values()) < sum(abs(v) for row in ols.values() for v in row.values())
    assert call("portfolio.time_series_factor_model", returns=x, factors=f, regressor="magic").value is None
    assert call("portfolio.time_series_factor_model", returns=x, factors=f, regressor="ridge", alpha=-1.0).value is None


def test_sk059_empty_nan_and_wrong_shape(panel):
    x, f = panel[["AAA", "BBB"]], panel[["DDD", "EEE"]]
    assert call("portfolio.time_series_factor_model", returns=x.iloc[:5], factors=f.iloc[:5]).value is None
    dirty = x.copy()
    dirty.iloc[10, 0] = np.nan
    result = call("portfolio.time_series_factor_model", returns=dirty, factors=f)
    assert result.value["n_observations"] == len(x) - 1 and "dropped 1 row(s)" in result.warnings[0]
    with pytest.raises(BadInput):
        call("portfolio.time_series_factor_model", returns=x.reset_index(drop=True), factors=f.iloc[:50].reset_index(drop=True))
    with pytest.raises(BadInput):
        call("portfolio.time_series_factor_model", returns=[1, 2], factors=f)


def _characteristics(rows: int = 300, assets: int = 40) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(0)
    idx = pd.bdate_range("2022-01-03", periods=rows)
    cols = [f"A{i}" for i in range(assets)]
    mcap = np.exp(rng.normal(10, 1, (rows, assets)))
    raw = {"returns": rng.normal(0, 0.01, (rows, assets)), "market_cap": mcap, "book_equity": mcap * np.exp(rng.normal(-0.5, 0.3, (rows, assets))),
           "score": rng.normal(0, 1, (rows, assets))}
    return {k: pd.DataFrame(v, index=idx, columns=cols) for k, v in raw.items()}


FACTORS = [{"name": "value", "descriptors": [{"kind": "book_to_price"}]},
           {"name": "mom", "descriptors": [{"kind": "ew_momentum", "params": {"half_life": 20, "skip": 2}}]}]


def test_sk060_matches_the_library_and_a_scored_factor_has_zero_mean_and_unit_spread():
    fields = _characteristics()
    result = call("portfolio.characteristics_factor_model", fields=fields, factors=FACTORS)
    assert result.piece_ids == ("SK-060",) and result.value["loadings"]
    est = _lib.sk_prior.CharacteristicsFactorModel(factors=[
        ("value", _lib.sk_factor_exposure.FixedWeightedFactor(descriptors=[("book_to_price", _lib.sk_descriptor.BookToPrice())])),
        ("mom", _lib.sk_factor_exposure.FixedWeightedFactor(descriptors=[("ew_momentum", _lib.sk_descriptor.EWMomentum(half_life=20, skip=2))]))])
    est.fit(None, characteristics=_lib.sk_panel({k: v for k, v in fields.items()}))
    direct = est.factor_model_
    assert np.allclose(list(result.value["factor_mu"].values()), direct.factor_mu, atol=1e-8, rtol=0)
    got = np.array([[result.value["loadings"][a][k] for k in ("value", "mom")] for a in fields["returns"].columns])
    assert np.allclose(got, direct.loading_matrix, atol=1e-8, rtol=0)
    # a z-scored style factor is centered on the market capitalization weighted universe: the weighted mean of its exposures is zero
    weights = fields["market_cap"].iloc[-1].to_numpy()
    assert abs(float((weights / weights.sum()) @ got[:, 0])) < 1e-12


def test_sk060_a_passthrough_descriptor_on_a_named_field_and_weights():
    fields = _characteristics()
    spec = [{"name": "quality", "descriptors": [{"kind": "passthrough", "name": "s", "params": {"field": "score"}},
                                                 {"kind": "book_to_price"}], "weights": [0.7, 0.3]}]
    result = call("portfolio.characteristics_factor_model", fields=fields, factors=spec)
    assert result.value["factor_mu"].keys() == {"quality"}
    for bad in ([1.0, -0.5], [1.0, 0.5]):                  # factor weights are not negative and sum to 1
        spec[0]["weights"] = bad
        assert call("portfolio.characteristics_factor_model", fields=fields, factors=spec).value is None
    assert registry.describe("portfolio.characteristics_factor_model").piece_ids == ("SK-060",)


def test_sk060_missing_fields_empty_and_unknown_descriptors_give_none():
    fields = _characteristics()
    no_returns = {k: v for k, v in fields.items() if k != "returns"}
    out = call("portfolio.characteristics_factor_model", fields=no_returns, factors=FACTORS)
    assert out.value is None and "data_missing:returns" in out.warnings[0]
    out = call("portfolio.characteristics_factor_model", fields={k: v for k, v in fields.items() if k != "book_equity"}, factors=FACTORS)
    assert out.value is None and "data_missing:book_equity" in out.warnings[0]
    assert call("portfolio.characteristics_factor_model", fields={}, factors=FACTORS).value is None
    assert call("portfolio.characteristics_factor_model", fields=fields, factors=[{"name": "x", "descriptors": [{"kind": "nonsense"}]}]).value is None
    short = {k: v.iloc[:5] for k, v in fields.items()}
    assert call("portfolio.characteristics_factor_model", fields=short, factors=FACTORS).value is None
    with pytest.raises(BadInput):
        call("portfolio.characteristics_factor_model", fields=fields, factors=[])
    with pytest.raises(BadInput):
        call("portfolio.characteristics_factor_model", fields=fields, factors=[{"name": "x", "descriptors": [{"kind": "book_to_price", "params": {"bad": 1}}]}])
    with pytest.raises(BadInput):
        call("portfolio.characteristics_factor_model", fields="x", factors=FACTORS)

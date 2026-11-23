"""Metrics part B: attribution by asset, factor and family (SK-081, SK-082, SK-083, SK-084)."""
import numpy as np
import pytest

from hokma.quant.errors import BadInput
from hokma.quant.metrics import _lib, attribution

from .metrics_b_qa import assert_close, assert_registered, benchmark, call

W = np.array([0.5, 0.3, 0.2])
LOADINGS = np.array([[1.0, 0.2], [0.8, -0.1], [0.5, 0.4]])
FCOV = np.array([[0.04, 0.01], [0.01, 0.02]]) / 252
ICOV = np.diag([0.01, 0.02, 0.015]) / 252
ASSETS, FACTORS, FAMILIES = ["a", "b", "c"], ["mkt", "val"], ["market", "style"]
MU_F = np.array([0.0004, 0.0001])


def predicted(**extra):
    kwargs = dict(weights=W, loading_matrix=LOADINGS, factor_covariance=FCOV, idio_covariance=ICOV, asset_names=ASSETS, factor_names=FACTORS,
                  factor_families=FAMILIES, factor_mu=MU_F)
    kwargs.update(extra)
    return kwargs


def reference(**extra):
    kwargs = predicted(**extra)
    return _lib.attribution_lib().predicted_factor_attribution(annualization_factor=252.0, **kwargs)


def test_predicted_by_hand_SK_082():
    assert_registered("metrics.attribution_predicted", "SK-082")
    res = call("metrics.attribution_predicted", **predicted())
    b = LOADINGS.T @ W                                   # portfolio exposure to each factor
    systematic_var = b @ (FCOV * 252) @ b                # 0.031194
    idio_var = W @ (ICOV * 252) @ W                      # 0.0049
    total_vol = np.sqrt(systematic_var + idio_var)
    assert_close(res.value["total"]["vol"], total_vol)
    assert_close(res.value["systematic"]["vol_contrib"], systematic_var / total_vol)
    assert_close(res.value["idio"]["vol_contrib"], idio_var / total_vol)
    assert_close(res.value["systematic"]["pct_total_variance"] + res.value["idio"]["pct_total_variance"], 1.0)
    assert_close(res.value["factors"]["mu_contrib"], (b * MU_F * 252).tolist())
    assert_close(res.value["systematic"]["mu_contrib"], float(b @ MU_F * 252))
    assert abs(systematic_var - 0.031194) < 1e-12 and abs(idio_var - 0.0049) < 1e-12


def test_predicted_matches_reference_SK_082():
    res = call("metrics.attribution_predicted", **predicted())
    ref = reference()
    assert_close(res.value["total"]["vol"], ref.total.vol)
    assert_close(res.value["factors"]["vol_contrib"], ref.factors.vol_contrib.tolist())
    assert_close(res.value["assets"]["vol_contrib"], ref.assets.vol_contrib.tolist())
    assert_close(sum(res.value["factors"]["vol_contrib"]), res.value["systematic"]["vol_contrib"])
    assert_close(sum(res.value["assets"]["vol_contrib"]), res.value["total"]["vol"])
    assert res.value["factors"]["names"] == FACTORS and res.value["families"]["names"] == FAMILIES
    assert res.inputs_hash and res.params["annualization_factor"] == 252.0


def test_predicted_bad_data_SK_082():
    nan_w = W.copy()
    nan_w[0] = np.nan
    res = call("metrics.attribution_predicted", **predicted(weights=nan_w))
    assert res.value is None and res.warnings
    empty = call("metrics.attribution_predicted", weights=[], loading_matrix=np.zeros((0, 2)), factor_covariance=FCOV, idio_covariance=np.zeros((0, 0)),
                 asset_names=[], factor_names=FACTORS)
    assert empty.value is None and empty.warnings
    with pytest.raises(BadInput):
        call("metrics.attribution_predicted", **predicted(weights=[0.5, 0.5]))
    with pytest.raises(BadInput):
        call("metrics.attribution_predicted", **predicted(weights="abc"))
    with pytest.raises(BadInput):
        call("metrics.attribution_predicted", **predicted(asset_names=["a"]))
    flat = call("metrics.attribution_predicted", **predicted(loading_matrix=np.zeros((3, 2)), factor_covariance=np.zeros((2, 2)), idio_covariance=np.zeros((3, 3))))
    assert flat.value is None and flat.warnings


@pytest.mark.parametrize("by,key", [("asset", "assets"), ("factor", "factors"), ("family", "families")])
def test_breakdown_SK_081(by, key):
    assert_registered("metrics.attribution_breakdown", "SK-081")
    res = call("metrics.attribution_breakdown", **predicted(), by=by)
    ref = getattr(reference(), key)
    assert res.value["names"] == list(ref.names)
    assert_close(res.value["vol_contrib"], ref.vol_contrib.tolist())
    assert_close(res.value["mu_contrib"], ref.mu_contrib.tolist())
    assert_close(sum(res.value["pct_total_variance"]), sum(ref.pct_total_variance))
    if by == "asset":
        assert_close(res.value["weight"], W.tolist())
        assert_close(sum(res.value["mu_contrib"]), float(W @ (LOADINGS @ MU_F) * 252))      # hand-written total
    if by == "factor":
        b = LOADINGS.T @ W
        assert_close(res.value["exposure"], b.tolist())


def test_breakdown_bad_arguments_SK_081():
    with pytest.raises(BadInput):
        call("metrics.attribution_breakdown", **predicted(), by="sector")
    with pytest.raises(BadInput):
        call("metrics.attribution_breakdown", **predicted(factor_families=None), by="family")
    nan = predicted()
    nan["factor_covariance"] = np.full((2, 2), np.nan)
    assert call("metrics.attribution_breakdown", **nan, by="factor").value is None


@pytest.mark.parametrize("part", ["systematic", "idio", "total"])
def test_components_SK_084(part):
    assert_registered("metrics.attribution_components", "SK-084")
    res = call("metrics.attribution_components", **predicted(), part=part)
    ref = getattr(reference(), part)
    assert_close(res.value["vol_contrib"], float(ref.vol_contrib))
    assert_close(res.value["vol"], float(ref.vol))
    assert_close(res.value["corr_with_ptf"], float(ref.corr_with_ptf))
    assert_close(res.value["mu_contrib"], float(ref.mu_contrib))


def test_components_asset_by_factor_SK_084():
    res = call("metrics.attribution_components", **predicted(), part="asset_by_factor")
    ref = reference().asset_by_factor_contrib
    assert res.value["asset_names"] == ASSETS and res.value["factor_names"] == FACTORS
    assert_close(res.value["vol_contrib"], ref.vol_contrib.tolist())
    by_factor = np.array(res.value["mu_contrib"]).sum(axis=0)
    assert_close(by_factor.tolist(), ((LOADINGS * W[:, None]).sum(axis=0) * MU_F * 252).tolist())     # hand-written column totals
    with pytest.raises(BadInput):
        call("metrics.attribution_components", **predicted(), part="residual")


def realized_inputs():
    returns_b = benchmark()
    n = len(returns_b)
    rng = np.random.default_rng(5)
    factors = np.column_stack([returns_b.to_numpy(), rng.normal(0, 0.004, n)])
    idio = rng.normal(0, 0.005, (n, 3))
    assets = factors @ LOADINGS.T + idio
    return dict(asset_names=ASSETS, factor_names=FACTORS, weights=W, factor_returns=factors, portfolio_returns=assets @ W, exposures=LOADINGS, idio_returns=idio)


def test_realized_by_hand_SK_083():
    assert_registered("metrics.attribution_realized", "SK-083")
    data = realized_inputs()
    res = call("metrics.attribution_realized", **data)
    pr, f = data["portfolio_returns"], data["factor_returns"]
    x = LOADINGS.T @ W
    systematic = f @ x
    assert_close(res.value["total"]["vol"], pr.std(ddof=1) * np.sqrt(252))
    assert_close(res.value["total"]["mu_contrib"], pr.mean() * 252)
    assert_close(res.value["systematic"]["vol_contrib"], np.cov(systematic, pr, ddof=1)[0, 1] / pr.std(ddof=1) * np.sqrt(252))
    assert_close(res.value["factors"]["mu_contrib"], (x * f.mean(axis=0) * 252).tolist())
    parts = sum(res.value[k]["mu_contrib"] for k in ("systematic", "idio", "unattributed"))
    assert_close(parts, res.value["total"]["mu_contrib"])
    assert_close(sum(res.value["factors"]["vol_contrib"]), res.value["systematic"]["vol_contrib"])
    ref = _lib.attribution_lib().realized_factor_attribution(**data)
    assert_close(res.value["factors"]["vol_contrib"], ref.factors.vol_contrib.tolist())


def test_realized_bad_data_SK_083():
    data = realized_inputs()
    nan = dict(data)
    nan["portfolio_returns"] = np.full_like(data["portfolio_returns"], np.nan)
    assert call("metrics.attribution_realized", **nan).value is None
    short = dict(data)
    short["portfolio_returns"] = data["portfolio_returns"][:10]
    with pytest.raises(BadInput):
        call("metrics.attribution_realized", **short)
    with pytest.raises(BadInput):
        call("metrics.attribution_realized", **data, exposure_lag=-1)
    zero = dict(data)
    zero["factor_returns"] = np.zeros((0, 2))
    zero["portfolio_returns"] = np.zeros(0)
    zero["idio_returns"] = np.zeros((0, 3))
    assert call("metrics.attribution_realized", **zero).value is None


def test_rolling_SK_083():
    data = realized_inputs()
    res = call("metrics.attribution_rolling", **data, window_size=120, step=60)
    ref = _lib.attribution_lib().rolling_realized_factor_attribution(observations=np.arange(len(data["portfolio_returns"])), window_size=120, step=60, **data)
    assert_close(res.value["systematic"]["vol_contrib"], ref.systematic.vol_contrib.tolist())
    assert len(res.value["total"]["vol"]) == 11
    last = data["portfolio_returns"][600:720]          # windows end at 119, 179, ..., 719
    assert_close(res.value["total"]["vol"][-1], last.std(ddof=1) * np.sqrt(252))
    short = call("metrics.attribution_rolling", **data, window_size=5000)
    assert short.value is None and short.warnings
    with pytest.raises(BadInput):
        call("metrics.attribution_rolling", **data, window_size=0)

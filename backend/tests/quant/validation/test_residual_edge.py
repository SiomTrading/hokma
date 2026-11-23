"""Residual edge: CTS-038."""
import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.validation import residual_edge as re_

from .conftest import assert_registered

DECLARED = {"baseline_selection": "predeclared", "strategy_return_basis": "net", "baseline_return_basis": "net",
            "analysis_scope": "out_of_sample", "universe_data": "not_applicable"}


def _build(benchmark, alpha: float, noise_sd: float = 0.004, beta: float = 1.2, seed: int = 41):
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, noise_sd, len(benchmark))
    strategy = pd.Series(alpha + beta * benchmark.to_numpy() + noise, index=benchmark.index)
    baselines = pd.DataFrame({"market": benchmark, "alt": rng.normal(0, 0.01, len(benchmark))}, index=benchmark.index)
    return strategy, baselines


def test_beta_times_market_has_no_significant_alpha_CTS_038(benchmark):
    strategy, baselines = _build(benchmark, alpha=0.0)
    out = registry.call("validation.residual_edge", strategy=strategy, baselines=baselines, primary=["market"], declarations=DECLARED).value
    assert out["primary"]["alpha"]["p_value"] > 0.05
    assert out["primary"]["betas"]["market"] == pytest.approx(1.2, abs=0.05)
    assert out["status"] != "RESIDUAL_EDGE"


def test_injected_alpha_of_five_basis_points_is_significant_CTS_038(benchmark):
    strategy, baselines = _build(benchmark, alpha=0.0005)
    out = registry.call("validation.residual_edge", strategy=strategy, baselines=baselines, primary=["market"], declarations=DECLARED).value
    assert out["primary"]["alpha"]["p_value"] < 0.01
    assert out["primary"]["alpha"]["per_period"] == pytest.approx(0.0005, abs=0.0004)
    assert out["primary"]["alpha"]["annualized"] == pytest.approx(out["primary"]["alpha"]["per_period"] * 252)
    assert_registered("validation.residual_edge", "CTS-038")


def test_exact_recovery_without_noise_CTS_038(benchmark):
    y = 0.0003 + 1.0 * benchmark.to_numpy()
    fitted = re_.fit(y, benchmark.to_numpy().reshape(-1, 1))
    assert fitted["beta"][0] == pytest.approx(0.0003, abs=1e-12) and fitted["beta"][1] == pytest.approx(1.0, abs=1e-12)


def test_hac_without_lags_equals_the_white_estimator_CTS_038(benchmark):
    strategy, baselines = _build(benchmark, alpha=0.0002)
    x = baselines[["market"]].to_numpy()
    fitted = re_.fit(strategy.to_numpy(), x, hac_lags=0)
    design = np.column_stack([np.ones(len(x)), x])
    resid = fitted["resid"]
    inv = np.linalg.inv(design.T @ design)
    meat = (design * resid[:, None] ** 2).T @ design * len(x) / (len(x) - 2)
    expected = np.sqrt(np.diag(inv @ meat @ inv))
    assert fitted["se"] == pytest.approx(expected, abs=1e-14)
    assert re_.default_hac_lags(756) == 4 * 0 + int(np.floor(4 * (756 / 100) ** (2 / 9)))


def test_strong_edge_needs_an_alternate_baseline_to_stand_CTS_038(benchmark):
    strategy, baselines = _build(benchmark, alpha=0.002)
    alone = registry.call("validation.residual_edge", strategy=strategy, baselines=baselines, primary=["market"], declarations=DECLARED).value
    assert alone["primary"]["status"] == "RESIDUAL_EDGE"
    assert alone["status"] == "RESIDUAL_FRAGILE" and alone["decision_eligibility"] == "REVIEW_REQUIRED"
    full = registry.call("validation.residual_edge", strategy=strategy, baselines=baselines, primary=["market"],
                         sensitivity=[["market", "alt"]], declarations=DECLARED).value
    assert full["status"] == "RESIDUAL_EDGE" and full["decision_eligibility"] == "ELIGIBLE"
    assert full["primary"]["residual"]["residual_edge_ratio"] > 0.75


def test_baseline_explained_CTS_038(benchmark):
    strategy, baselines = _build(benchmark, alpha=0.0, noise_sd=0.0005)
    out = registry.call("validation.residual_edge", strategy=strategy, baselines=baselines, primary=["market"],
                        sensitivity=[["market", "alt"]], declarations=DECLARED).value
    assert out["primary"]["r_squared"] > 0.95
    assert out["status"] == "BASELINE_EXPLAINED"


def test_missing_declarations_lower_eligibility_CTS_038(benchmark):
    strategy, baselines = _build(benchmark, alpha=0.002)
    out = registry.call("validation.residual_edge", strategy=strategy, baselines=baselines, primary=["market"], sensitivity=[["market", "alt"]],
                        declarations={"baseline_selection": "post_hoc"})
    assert out.value["decision_eligibility"] == "REVIEW_REQUIRED"
    assert any("baseline_selection" in p for p in out.value["declaration_problems"])
    assert out.warnings


def test_regimes_are_reported_CTS_038(benchmark):
    strategy, baselines = _build(benchmark, alpha=0.001)
    labels = pd.Series(np.where(np.arange(len(benchmark)) < 500, "calm", "stress"), index=benchmark.index)
    out = registry.call("validation.residual_edge", strategy=strategy, baselines=baselines, primary=["market"], regimes={"vol": labels},
                        declarations=DECLARED).value
    assert out["regimes"]["vol"]["calm"]["n"] == 500 and out["regimes"]["vol"]["stress"]["n"] == len(benchmark) - 500


def test_short_sample_is_insufficient_evidence_CTS_038(benchmark):
    strategy, baselines = _build(benchmark, alpha=0.001)
    out = registry.call("validation.residual_edge", strategy=strategy.iloc[:30], baselines=baselines.iloc[:30], primary=["market"]).value
    assert out["status"] == "INSUFFICIENT_EVIDENCE" and out["decision_eligibility"] == "REVIEW_REQUIRED"


def test_empty_constant_nan_and_collinear_give_none_CTS_038(benchmark):
    _, baselines = _build(benchmark, alpha=0.0)
    for bad in (pd.Series([], dtype=float), pd.Series(0.001, index=benchmark.index), pd.Series(np.nan, index=benchmark.index)):
        result = registry.call("validation.residual_edge", strategy=bad, baselines=baselines, primary=["market"])
        assert result.value is None and result.warnings
    collinear = baselines.assign(twin=baselines["market"] * 2)
    strategy, _ = _build(benchmark, alpha=0.0)
    result = registry.call("validation.residual_edge", strategy=strategy, baselines=collinear, primary=["market", "twin"])
    assert result.value is None and result.warnings
    below = registry.call("validation.residual_edge", strategy=pd.Series(-1.5, index=benchmark.index) + np.arange(len(benchmark)) * 1e-9,
                          baselines=baselines, primary=["market"])
    assert below.value is None


def test_bad_inputs_CTS_038(benchmark):
    strategy, baselines = _build(benchmark, alpha=0.0)
    with pytest.raises(BadInput):
        registry.call("validation.residual_edge", strategy=strategy, baselines=baselines, primary=["missing"])
    with pytest.raises(BadInput):
        registry.call("validation.residual_edge", strategy=strategy, baselines=[1, 2], primary=["market"])
    with pytest.raises(BadInput):
        registry.call("validation.residual_edge", strategy=strategy, baselines=baselines, primary=["market"], frequency="hourly")


def test_vif_of_an_exactly_explained_column_is_finite_not_infinite():
    base = np.random.default_rng(3).normal(0, 0.01, 60)
    factors = np.column_stack([base, 2.0 * base])                   # the second column is exactly the first one: R squared of 1
    vif = re_._vif(factors, ["a", "b"])
    assert all(np.isfinite(v) and v > re_.VIF_LIMIT for v in vif.values())

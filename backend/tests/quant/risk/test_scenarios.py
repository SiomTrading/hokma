"""Scenarios and stress. Rows SK-061 and SK-063; stress is the entry point of section 4.4."""
import numpy as np
import pandas as pd
import pytest

from hokma.quant import risk
from hokma.quant.errors import BadInput
from hokma.quant.risk import _lib

from .conftest import call, piece_ids

TOL = 1e-9


def test_synthetic_scenarios_match_origin_SK_061(panel, weights):
    res = call("risk.synthetic_scenarios", panel=panel, n_samples=400, weights=weights, seed=3)
    vine = _lib.sk_dist.VineCopula(random_state=3)
    sims = _lib.sk_prior.SyntheticData(distribution_estimator=vine, n_samples=400).fit(panel).return_distribution_.returns
    assert res.value["mean"] == pytest.approx(list(sims.mean(axis=0)), abs=TOL)
    assert res.value["std"] == pytest.approx(list(sims.std(axis=0, ddof=1)), abs=TOL)
    port = sims @ np.full(5, 0.2)
    assert res.value["portfolio"]["var"] == pytest.approx(_lib.sk_measures.value_at_risk(port, beta=0.95), abs=TOL)
    assert res.value["portfolio"]["cvar"] == pytest.approx(_lib.sk_measures.cvar(port, beta=0.95), abs=TOL)
    # hand value: the scenario count is the requested one and the same seed repeats the draw
    assert res.value["n_samples"] == 400
    assert call("risk.synthetic_scenarios", panel=panel, n_samples=400, weights=weights, seed=3).value == res.value
    assert "SK-061" in piece_ids("risk.synthetic_scenarios")


def test_synthetic_scenarios_conditioned_on_an_asset_SK_061(panel):
    res = call("risk.synthetic_scenarios", panel=panel, n_samples=300, conditioning={"AAA": (-0.05, -0.02)}, seed=1)
    assert res.value["min"][0] >= -0.05 - 1e-12 and res.value["max"][0] <= -0.02 + 1e-12
    assert res.params["conditioning"] == {"AAA": (-0.05, -0.02)}
    assert "portfolio" not in res.value
    with pytest.raises(BadInput):
        call("risk.synthetic_scenarios", panel=panel, conditioning={"ZZZ": 0.0})
    with pytest.raises(BadInput):
        call("risk.synthetic_scenarios", panel=panel, conditioning=[("AAA", 0.0)])
    with pytest.raises(BadInput):
        call("risk.synthetic_scenarios", panel=panel, n_samples="many")


def test_synthetic_scenarios_bad_data_SK_061(panel, weights):
    assert call("risk.synthetic_scenarios", panel=panel.iloc[:10]).value is None
    assert call("risk.synthetic_scenarios", panel=panel, n_samples=3).value is None
    assert call("risk.synthetic_scenarios", panel=panel, level=1.2).value is None
    constant = panel.copy()
    constant["BBB"] = 0.0
    res = call("risk.synthetic_scenarios", panel=constant, weights=weights)
    assert res.value is None and res.warnings


def test_family_constraint_basis_SK_063():
    exposures = [[[1.0, 0.0], [0.0, 1.0]]]            # one date, two assets, two factors of the same family
    bench = [[0.75, 0.25]]
    res = call("risk.family_constraint_basis", constrained_families=[("industry", None)], factor_exposures=exposures,
               benchmark_weights=bench, factor_names=["tech", "energy"], factor_families=["industry", "industry"])
    # hand value: weighted exposures are 0.75 and 0.25; the larger one is dropped and the ratio is 0.25 / 0.75
    assert res.value["resolved_constraints"] == [["industry", "tech"]]
    assert res.value["dropped_factors"] == ["tech"]
    assert res.value["constraint_ratios"] == [[pytest.approx(1.0 / 3.0, abs=TOL)]]
    assert res.value["n_observations"] == 1 and res.value["n_constraints"] == 1 and res.value["n_reduced_factors"] == 1
    assert res.value["reduced_factor_names"] == ["energy"]
    origin, resolved = _lib.compute_family_constraint_basis(
        constrained_families=[("industry", None)], factor_exposures=np.array(exposures), benchmark_weights=np.array(bench),
        factor_names=np.array(["tech", "energy"]), factor_families=np.array(["industry", "industry"]))
    assert resolved == [("industry", "tech")]
    assert origin.constraint_ratios[0, 0] == pytest.approx(res.value["constraint_ratios"][0][0], abs=TOL)
    assert "SK-063" in piece_ids("risk.family_constraint_basis")


def test_family_constraint_basis_bad_input_SK_063():
    base = dict(constrained_families=[("industry", "tech")], factor_exposures=[[[1.0, 0.0], [0.0, 1.0]]], factor_names=["tech", "energy"],
                factor_families=["industry", "industry"])
    assert call("risk.family_constraint_basis", benchmark_weights=[[0.0, 0.0]], **base).value is None
    unknown = dict(base, constrained_families=[("sector", None)])
    res = call("risk.family_constraint_basis", benchmark_weights=[[0.5, 0.5]], **unknown)
    assert res.value is None and res.warnings
    with pytest.raises(BadInput):
        call("risk.family_constraint_basis", benchmark_weights=[0.5, 0.5], **base)


def test_stress_shock_and_volatility_SK_061(panel, weights):
    w = pd.Series(weights)
    base = risk.stress(panel, weights, scenario={"vol_multiplier": 1.0})
    cov = np.cov(panel.to_numpy(), rowvar=False)
    sigma_p = float(np.sqrt(w.to_numpy() @ cov @ w.to_numpy()))
    assert base.value["base_volatility"] == pytest.approx(sigma_p, abs=TOL)
    assert base.value["stressed_volatility"] == pytest.approx(sigma_p, abs=TOL)
    from statistics import NormalDist
    assert base.value["base_var"] == pytest.approx(-float(w @ panel.mean()) + NormalDist().inv_cdf(0.95) * sigma_p, abs=TOL)
    doubled = risk.stress(panel, weights, scenario={"vol_multiplier": 2.0})
    assert doubled.value["stressed_volatility"] == pytest.approx(2 * sigma_p, abs=TOL)
    assert doubled.value["stressed_var"] > doubled.value["base_var"]
    shocked = risk.stress(panel, {"AAA": 0.6, "BBB": 0.4}, scenario={"shocks": {"AAA": -0.10, "BBB": -0.05}})
    assert shocked.value["shock_return"] == pytest.approx(-0.08, abs=TOL)
    assert call("risk.stress", panel=panel, weights=weights, scenario={"vol_multiplier": 3}).value["level"] == 0.95
    assert "SK-061" in piece_ids("risk.stress")


def test_stress_correlation_shift_SK_061(panel):
    two = panel[["AAA", "BBB"]]
    w = {"AAA": 0.5, "BBB": 0.5}
    res = risk.stress(two, w, scenario={"corr_shift": 0.2})
    s1, s2 = two.std(ddof=1)
    rho = float(np.corrcoef(two["AAA"], two["BBB"])[0, 1])
    hand = float(np.sqrt(0.25 * s1**2 + 0.25 * s2**2 + 2 * 0.25 * s1 * s2 * (rho + 0.2)))
    assert res.value["stressed_volatility"] == pytest.approx(hand, abs=1e-9)
    assert res.value["stressed_volatility"] > res.value["base_volatility"]


def test_stress_bad_input_SK_061(panel, weights):
    assert risk.stress(panel, weights, scenario={}).value is None
    assert risk.stress(panel, weights, scenario={"volatility": 2}).value is None
    assert risk.stress(panel, weights, scenario={"vol_multiplier": -1}).value is None
    assert risk.stress(panel, weights, scenario={"vol_multiplier": 2, "level": 1.5}).value is None
    assert risk.stress(panel.iloc[:5], weights, scenario={"vol_multiplier": 2}).value is None
    with pytest.raises(BadInput):
        risk.stress(panel, weights, scenario={"shocks": {"ZZZ": -0.1}})
    with pytest.raises(BadInput):
        risk.stress(panel, weights, scenario=[("shocks", 1)])
    with pytest.raises(BadInput):
        risk.stress(panel, {"ZZZ": 1.0}, scenario={"vol_multiplier": 2})
    with pytest.raises(BadInput):
        risk.stress([1, 2, 3], weights, scenario={"vol_multiplier": 2})

"""SK-001, SK-002, SK-003, SK-006, SK-014, SK-015: the optimizers, their constraints, the failure path and the baselines."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.portfolio import _lib

from .conftest import call


def _w(result) -> np.ndarray:
    return np.array(list(result.value["weights"].values()))


def test_sk001_minimum_variance_matches_the_hand_solution(orthogonal):
    # variances 1e-4 and 4e-4, no covariance: the weights are proportional to 1 / variance, that is 0.8 and 0.2
    result = call("portfolio.mean_risk", returns=orthogonal)
    assert result.value["status"] == "optimal" and result.piece_ids == ("SK-001",)
    # the solver stops at its default tolerance, about 1e-4 on weights at this scale
    assert result.value["weights"]["a"] == pytest.approx(0.8, abs=1e-3)
    assert result.value["weights"]["b"] == pytest.approx(0.2, abs=1e-3)
    # sample variances (n - 1 = 7) are 8 / 7 times the population ones
    assert result.value["risk"] == pytest.approx((0.8 ** 2 * 1e-4 + 0.2 ** 2 * 4e-4) * 8.0 / 7.0, rel=1e-3)


def test_sk001_matches_the_library_at_1e8(panel):
    result = call("portfolio.mean_risk", returns=panel)
    direct = _lib.sk_optimization.MeanRisk().fit(panel)
    assert np.allclose(_w(result), direct.weights_, atol=1e-8, rtol=0)
    assert _w(result).sum() == pytest.approx(1.0, abs=1e-8)


@pytest.mark.parametrize("measure", ["variance", "standard_deviation", "semi_variance", "mean_absolute_deviation", "cvar", "worst_realization",
                                     "first_lower_partial_moment", "gini_mean_difference", "max_drawdown", "average_drawdown", "cdar", "ulcer_index"])
def test_sk001_every_tested_risk_measure_gives_weights_that_sum_to_one(panel, measure):
    result = call("portfolio.mean_risk", returns=panel.iloc[:150], risk_measure=measure)
    assert result.value["status"] == "optimal", result.warnings
    assert _w(result).sum() == pytest.approx(1.0, abs=1e-6)
    direct = _lib.sk_optimization.MeanRisk(risk_measure=_lib.RiskMeasure(measure)).fit(panel.iloc[:150])
    assert np.allclose(_w(result), direct.weights_, atol=1e-8, rtol=0)


@pytest.mark.parametrize("objective", ["max_return", "max_utility", "max_ratio"])
def test_sk001_the_other_objectives_run(panel, objective):
    result = call("portfolio.mean_risk", returns=panel, objective=objective, max_weights=0.6)
    assert result.value["status"] == "optimal", result.warnings
    assert max(_w(result)) <= 0.6 + 1e-6
    direct = _lib.sk_optimization.MeanRisk(objective_function=_lib.OBJECTIVES[objective], max_weights=0.6).fit(panel)
    assert np.allclose(_w(result), direct.weights_, atol=1e-6, rtol=0)


def test_sk001_bounds_per_asset_and_budget(panel):
    result = call("portfolio.mean_risk", returns=panel, min_weights={"AAA": 0.3}, max_weights={"AAA": 0.35, "BBB": 0.1}, budget=1.0)
    w = result.value["weights"]
    assert 0.3 - 1e-6 <= w["AAA"] <= 0.35 + 1e-6 and w["BBB"] <= 0.1 + 1e-6
    assert sum(w.values()) == pytest.approx(1.0, abs=1e-6)


def test_sk001_an_infeasible_problem_returns_the_reason_and_a_labelled_baseline_never_invented_weights(panel):
    result = call("portfolio.mean_risk", returns=panel, min_weights=0.3)       # 5 x 0.3 is above a budget of 1
    assert result.value["status"] == "failed" and result.value["weights"] is None
    assert "infeasible" in result.value["reason"]
    fallback = result.value["fallback"]
    assert fallback["method"] == "inverse_volatility"
    expected = call("portfolio.baselines", returns=panel).value["weights"]
    assert fallback["weights"] == pytest.approx(expected, abs=1e-12)
    assert any("optimization failed" in w for w in result.warnings)


def test_sk001_empty_nan_and_out_of_domain_give_none(panel):
    assert call("portfolio.mean_risk", returns=panel.iloc[:0]).value is None
    all_nan = panel.copy()
    all_nan["AAA"] = np.nan
    result = call("portfolio.mean_risk", returns=all_nan)
    assert result.value is None and "complete row" in result.warnings[0]
    partial = panel.copy()
    partial.iloc[3, 2] = np.nan
    assert call("portfolio.mean_risk", returns=partial).warnings == ("dropped 1 row(s) with NaN or infinite values",)
    for bad in ({"objective": "best"}, {"risk_measure": "fear"}, {"solver": "MAGIC"}, {"risk_aversion": -1.0}, {"cvar_beta": 1.5}, {"l1_coef": float("nan")}):
        out = call("portfolio.mean_risk", returns=panel, **bad)
        assert out.value is None and out.warnings, bad
    with pytest.raises(BadInput):
        call("portfolio.mean_risk", returns=[1, 2, 3])
    with pytest.raises(BadInput):
        call("portfolio.mean_risk", returns=panel, min_weights={"ZZZ": 0.1})


def test_sk002_cardinality_keeps_exactly_the_number_of_assets_asked(panel):
    result = call("portfolio.mean_risk_constrained", returns=panel, risk_measure="mean_absolute_deviation", cardinality=2, solver="SCIPY")
    assert result.piece_ids == ("SK-002",) and result.value["status"] == "optimal", result.warnings
    assert int((_w(result) > 1e-6).sum()) == 2 and _w(result).sum() == pytest.approx(1.0, abs=1e-6)


def test_sk002_a_cap_by_risk_measure_binds_and_the_minimum_position_holds(panel):
    capped = call("portfolio.mean_risk_constrained", returns=panel, objective="max_return", max_risk={"variance": 1e-4})
    assert capped.value["status"] == "optimal" and capped.value["risk"] <= 1e-4 + 1e-8
    direct = _lib.sk_optimization.MeanRisk(objective_function=_lib.OBJECTIVES["max_return"], max_variance=1e-4).fit(panel)
    assert np.allclose(_w(capped), direct.weights_, atol=1e-6, rtol=0)
    held = call("portfolio.mean_risk_constrained", returns=panel, risk_measure="mean_absolute_deviation", solver="SCIPY", threshold_long=0.1)
    assert held.value["status"] == "optimal"
    assert all(v < 1e-6 or v >= 0.1 - 1e-6 for v in _w(held))


def test_sk002_turnover_against_previous_weights_and_costs(panel):
    previous = {"AAA": 1.0}
    result = call("portfolio.mean_risk_constrained", returns=panel, previous_weights=previous, max_turnover=0.2, transaction_costs=0.001)
    assert result.value["status"] == "optimal", result.warnings
    w = result.value["weights"]
    # turnover is one way: half of the sum of the absolute changes; the portfolio moved 0.2 out of AAA
    assert 0.5 * sum(abs(w[a] - previous.get(a, 0.0)) for a in w) <= 0.2 + 1e-5
    assert w["AAA"] == pytest.approx(0.8, abs=1e-4)
    assert call("portfolio.mean_risk_constrained", returns=panel, max_turnover=0.2).value is None      # needs previous_weights


def test_sk002_out_of_domain_constraints_give_none(panel):
    assert call("portfolio.mean_risk_constrained", returns=panel, cardinality=9).value is None
    assert call("portfolio.mean_risk_constrained", returns=panel, max_risk={"charm": 1.0}).value is None
    assert call("portfolio.mean_risk_constrained", returns=panel, max_risk={"variance": -1.0}).value is None
    with pytest.raises(BadInput):
        call("portfolio.mean_risk_constrained", returns=panel, max_risk=[1])
    with pytest.raises(BadInput):
        call("portfolio.mean_risk_constrained", returns=panel, previous_weights={"ZZZ": 1.0})


def test_sk003_objectives_lists_what_an_optimizer_accepts():
    out = call("portfolio.objectives").value
    assert set(out["objectives"]) == {"min_risk", "max_return", "max_utility", "max_ratio"}
    assert len(out["risk_measures"]) == 19 and "cvar" in out["risk_measures"] and "CLARABEL" in out["solvers"]
    assert set(out["capped_measures"]) <= set(out["risk_measures"])
    assert registry.describe("portfolio.objectives").piece_ids == ("SK-003",)


def test_sk006_robust_cvar_matches_the_library_and_respects_bounds(panel):
    result = call("portfolio.robust_cvar", returns=panel, wasserstein_ball_radius=0.01, max_weights=0.5)
    assert result.value["status"] == "optimal" and result.value["risk_measure"] == "cvar" and result.piece_ids == ("SK-006",)
    assert max(_w(result)) <= 0.5 + 1e-6 and _w(result).sum() == pytest.approx(1.0, abs=1e-6)
    direct = _lib.sk_optimization.DistributionallyRobustCVaR(wasserstein_ball_radius=0.01, max_weights=0.5).fit(panel)
    assert np.allclose(_w(result), direct.weights_, atol=1e-8, rtol=0)


def test_sk006_a_larger_radius_never_concentrates_more(panel):
    small = _w(call("portfolio.robust_cvar", returns=panel, wasserstein_ball_radius=0.001))
    large = _w(call("portfolio.robust_cvar", returns=panel, wasserstein_ball_radius=0.2))
    assert (large ** 2).sum() <= (small ** 2).sum() + 1e-6


def test_sk006_infeasible_and_bad_input(panel):
    out = call("portfolio.robust_cvar", returns=panel, min_weights=0.3)
    assert out.value["status"] == "failed" and out.value["weights"] is None
    assert call("portfolio.robust_cvar", returns=panel.iloc[:0]).value is None
    assert call("portfolio.robust_cvar", returns=panel, wasserstein_ball_radius=-1.0).value is None
    assert call("portfolio.robust_cvar", returns=panel, cvar_beta=0.0).value is None


def test_sk014_inverse_volatility_and_equal_weights_by_hand(orthogonal):
    inv = call("portfolio.baselines", returns=orthogonal, kind="inverse_volatility")
    assert inv.value["weights"]["a"] == pytest.approx(2.0 / 3.0, abs=1e-12) and inv.value["weights"]["b"] == pytest.approx(1.0 / 3.0, abs=1e-12)
    eq = call("portfolio.baselines", returns=orthogonal, kind="equal_weighted")
    assert eq.value["weights"] == {"a": 0.5, "b": 0.5} and eq.piece_ids == ("SK-014",)
    # variance of the equal portfolio: (0.25 * 1e-4 + 0.25 * 4e-4) * 8 / 7 with the sample estimator
    assert eq.value["risk"] == pytest.approx(0.25 * 5e-4 * 8.0 / 7.0, abs=1e-12)


def test_sk014_inverse_volatility_matches_the_library(panel):
    result = call("portfolio.baselines", returns=panel)
    direct = _lib.sk_optimization.InverseVolatility().fit(panel)
    assert np.allclose(_w(result), direct.weights_, atol=1e-8, rtol=0)


def test_sk014_random_is_seeded_and_leaves_the_global_state_alone(panel):
    before = np.random.get_state()[1].copy()
    one = call("portfolio.baselines", returns=panel, kind="random", seed=7).value["weights"]
    two = call("portfolio.baselines", returns=panel, kind="random", seed=7).value["weights"]
    other = call("portfolio.baselines", returns=panel, kind="random", seed=8).value["weights"]
    assert one == two and one != other and sum(one.values()) == pytest.approx(1.0)
    assert np.array_equal(before, np.random.get_state()[1])


def test_sk014_bad_input(panel):
    assert call("portfolio.baselines", returns=panel, kind="fancy").value is None
    assert call("portfolio.baselines", returns=panel.iloc[:0]).value is None
    with pytest.raises(BadInput):
        call("portfolio.baselines", returns=panel, kind="random", seed=-1)
    with pytest.raises(BadInput):
        call("portfolio.baselines", returns=panel, kind="random", seed=True)


def test_sk015_every_optimizer_shares_the_base_and_has_a_function():
    out = call("portfolio.optimizers").value
    assert out["base"] == "BaseOptimization" and registry.describe("portfolio.optimizers").piece_ids == ("SK-015",)
    assert all(row["shares_base"] for row in out["optimizers"])
    for row in out["optimizers"]:
        assert registry.describe(row["function"]).module == "portfolio"

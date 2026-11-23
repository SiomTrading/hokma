"""SK-106, SK-108, SK-109, SK-111, SK-112: portfolio container, charts, risk contribution, controlled failure and population."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.portfolio import _lib
from hokma.quant.portfolio.container import Population, Portfolio

from .conftest import call

TWO = pd.DataFrame({"a": [0.01, -0.02, 0.03, 0.0], "b": [0.03, 0.0, -0.01, 0.02]}, index=pd.bdate_range("2024-01-01", periods=4))
HALF = {"a": 0.5, "b": 0.5}


def test_sk106_pnl_nav_and_mean_by_hand():
    pf, _ = Portfolio.build(TWO, HALF)
    # P&L of each period: 0.5 a + 0.5 b = 0.02, -0.01, 0.01, 0.01
    assert pf.pnl().tolist() == pytest.approx([0.02, -0.01, 0.01, 0.01], abs=1e-15)
    assert float(pf.nav().iloc[-1]) == pytest.approx(1.02 * 0.99 * 1.01 * 1.01, abs=1e-12)
    result = call("portfolio.portfolio_summary", returns=TWO, weights=HALF)
    assert result.value["measures"]["mean"] == pytest.approx(0.0075, abs=1e-15)
    assert result.value["nav_last"] == pytest.approx(1.03009698, abs=1e-12) and result.value["total_return"] == pytest.approx(0.03009698, abs=1e-12)
    assert result.piece_ids == ("SK-106",) and result.value["weights"] == HALF


def test_sk106_every_measure_matches_the_library(panel):
    weights = {"AAA": 0.4, "BBB": 0.3, "CCC": 0.3}
    names = ["variance", "standard_deviation", "cvar", "max_drawdown", "sharpe_ratio", "sortino_ratio", "calmar_ratio", "annualized_mean",
             "effective_number_assets", "value_at_risk", "skew", "kurtosis", "ulcer_index"]
    result = call("portfolio.portfolio_summary", returns=panel, weights=weights, measures=names)
    direct = _lib.SkPortfolio(X=panel, weights=np.array([0.4, 0.3, 0.3, 0.0, 0.0]), compounded=True)
    for name in names:
        expected = direct.effective_number_assets if name == "effective_number_assets" else getattr(direct, name)
        assert result.value["measures"][name] == pytest.approx(float(expected), abs=1e-8), name


def test_sk106_empty_nan_unknown_measure_and_wrong_shapes(panel):
    assert call("portfolio.portfolio_summary", returns=panel.iloc[:2], weights={"AAA": 1.0}).value is None
    assert call("portfolio.portfolio_summary", returns=panel, weights={"AAA": float("nan")}).value is None
    assert call("portfolio.portfolio_summary", returns=panel, weights={"AAA": 1.0}, measures=["luck"]).value is None
    assert call("portfolio.portfolio_summary", returns=panel, weights={"AAA": 1.0}, risk_free_rate=float("nan")).value is None
    all_nan = panel.copy()
    all_nan["AAA"] = np.nan
    assert call("portfolio.portfolio_summary", returns=all_nan, weights={"AAA": 1.0}).value is None
    with pytest.raises(BadInput):
        call("portfolio.portfolio_summary", returns=panel, weights={"ZZZ": 1.0})
    with pytest.raises(BadInput):
        call("portfolio.portfolio_summary", returns=panel, weights=[1.0, 2.0])
    with pytest.raises(BadInput):
        call("portfolio.portfolio_summary", returns=panel, weights={"AAA": 1.0}, measures=[])


def test_sk108_the_traces_carry_the_data_of_each_chart():
    drawdowns = call("portfolio.charts", returns=TWO, weights=HALF, kind="drawdowns")
    assert drawdowns.piece_ids == ("SK-108",) and len(drawdowns.value["traces"]) == 1
    nav = [1.02, 1.02 * 0.99, 1.02 * 0.99 * 1.01, 1.02 * 0.99 * 1.01 * 1.01]
    expected = [nav[i] / max(nav[: i + 1]) - 1.0 for i in range(4)]            # drawdown: the NAV against its running maximum
    assert drawdowns.value["traces"][0]["y"] == pytest.approx(expected, abs=1e-12)
    assert drawdowns.value["traces"][0]["x"][0].startswith("2024-01-01")
    comp = call("portfolio.charts", returns=TWO, weights={"a": 0.25, "b": 0.75}, kind="composition")
    by_name = {t["name"]: t["y"] for t in comp.value["traces"]}
    assert by_name == {"a": [0.25], "b": [0.75]}


def test_sk108_the_other_kinds_and_bad_input(panel):
    for kind in ("cumulative_returns", "returns", "returns_distribution"):
        out = call("portfolio.charts", returns=panel.iloc[:100], weights={"AAA": 0.5, "BBB": 0.5}, kind=kind)
        assert out.value["kind"] == kind and out.value["traces"], kind
    assert call("portfolio.charts", returns=panel, weights={"AAA": 1.0}, kind="pie").value is None
    assert call("portfolio.charts", returns=panel.iloc[:1], weights={"AAA": 1.0}).value is None
    with pytest.raises(BadInput):
        call("portfolio.charts", returns=panel, weights={"ZZZ": 1.0})


def test_sk109_variance_share_of_two_uncorrelated_assets_by_hand(orthogonal):
    result = call("portfolio.contribution", returns=orthogonal, weights={"a": 0.5, "b": 0.5})
    # contribution_i is proportional to w_i^2 variance_i: shares 1e-4 / 5e-4 and 4e-4 / 5e-4
    assert result.value["share"] == {"a": pytest.approx(0.2, abs=1e-9), "b": pytest.approx(0.8, abs=1e-9)}
    assert result.piece_ids == ("SK-109",)
    # the contributions add up to the gradient scaled total, twice the variance for a quadratic measure
    pf = Portfolio.build(orthogonal, {"a": 0.5, "b": 0.5})[0]
    assert result.value["total"] == pytest.approx(2.0 * pf.measure("variance"), abs=1e-12)


def test_sk109_matches_the_library_for_other_measures(panel):
    weights = {"AAA": 0.5, "BBB": 0.3, "CCC": 0.2}
    for measure in ("standard_deviation", "cvar"):
        result = call("portfolio.contribution", returns=panel, weights=weights, measure=measure)
        direct = _lib.SkPortfolio(X=panel, weights=np.array([0.5, 0.3, 0.2, 0.0, 0.0]), compounded=True).contribution(measure=_lib.RiskMeasure(measure))
        assert np.allclose(list(result.value["contribution"].values()), direct, atol=1e-8, rtol=0), measure
        assert sum(result.value["share"].values()) == pytest.approx(1.0, abs=1e-12)


def test_sk109_out_of_domain(panel):
    assert call("portfolio.contribution", returns=panel, weights={"AAA": 1.0}, measure="sharpe_ratio").value is None
    assert call("portfolio.contribution", returns=panel.iloc[:1], weights={"AAA": 1.0}).value is None
    assert call("portfolio.contribution", returns=panel, weights={"AAA": 0.0}).value is None            # nothing at risk
    with pytest.raises(BadInput):
        call("portfolio.contribution", returns=panel, weights={"ZZZ": 1.0})


def test_sk111_a_failed_solver_gives_the_reason_and_a_labelled_baseline_never_weights(orthogonal):
    result = call("portfolio.failed", returns=orthogonal, reason="the solver did not converge")
    assert result.value["status"] == "failed" and result.value["weights"] is None and result.value["reason"] == "the solver did not converge"
    assert result.value["fallback"]["method"] == "inverse_volatility"
    assert result.value["fallback"]["weights"] == {"a": pytest.approx(2.0 / 3.0), "b": pytest.approx(1.0 / 3.0)}      # 1 / sigma, by hand
    assert result.piece_ids == ("SK-111",) and any("optimization failed" in w for w in result.warnings)
    equal = call("portfolio.failed", returns=orthogonal, reason="x", fallback="equal_weighted")
    assert equal.value["fallback"]["weights"] == {"a": 0.5, "b": 0.5}
    none = call("portfolio.failed", returns=orthogonal, reason="x", fallback="none")
    assert none.value["fallback"] is None and none.value["weights"] is None


def test_sk111_the_same_failure_path_serves_the_optimizers(panel):
    failed = call("portfolio.mean_risk", returns=panel, min_weights=0.3)
    assert failed.value["status"] == "failed" and failed.value["weights"] is None
    assert set(failed.value) == {"status", "reason", "weights", "fallback"}
    marker = _lib.SkFailedPortfolio(X=panel, optimization_error="infeasible")
    assert marker.optimization_error == "infeasible"


def test_sk111_empty_and_out_of_domain(orthogonal):
    assert call("portfolio.failed", returns=orthogonal.iloc[:0], reason="x").value is None
    assert call("portfolio.failed", returns=orthogonal, reason="  ").value is None
    assert call("portfolio.failed", returns=orthogonal, reason="x", fallback="magic").value is None
    with pytest.raises(BadInput):
        call("portfolio.failed", returns="nope", reason="x")


def test_sk112_candidates_are_ranked_and_dominance_is_found_by_hand():
    returns = pd.DataFrame({"a": [0.01, 0.02, 0.03, 0.02], "b": [0.0, 0.01, 0.0, 0.01], "c": [-0.01, 0.0, 0.01, 0.0]})
    candidates = {"A": {"a": 1.0}, "B": {"b": 1.0}, "C": {"c": 1.0}}
    by_mean = call("portfolio.population", returns=returns, candidates=candidates, measures=["mean", "variance"], rank_by="mean")
    # means 0.02, 0.005, 0.0: higher is better
    assert by_mean.value["ranking"] == ["A", "B", "C"] and by_mean.piece_ids == ("SK-112",)
    assert by_mean.value["measures"]["A"]["mean"] == pytest.approx(0.02, abs=1e-15)
    # b moves 0.005 around its mean against 0.01 for a and c, so b has the lowest variance and ranks first
    by_risk = call("portfolio.population", returns=returns, candidates=candidates, measures=["variance"], rank_by="variance")
    assert by_risk.value["ranking"][0] == "B"
    # A has the best mean and B the lowest risk, so neither dominates the other; B dominates C only if it is at least as good in everything
    assert {"dominates": "A", "dominated": "B"} not in by_mean.value["dominance"]


def test_sk112_population_container_matches_the_library(panel):
    pop, _ = Population.build(panel, {"eq": [0.2] * 5, "one": {"AAA": 1.0}})
    direct = _lib.SkPopulation([p.sk() for p in pop.candidates.values()])
    assert pop.ranking("sharpe_ratio") == [p.name for p in direct.sort_measure(_lib.sk_measures.RatioMeasure.SHARPE_RATIO, reverse=True)]
    assert pop.table(("mean",))["one"]["mean"] == pytest.approx(float(panel["AAA"].mean()), abs=1e-12)


def test_sk112_empty_nan_and_wrong_shapes(panel):
    assert call("portfolio.population", returns=panel.iloc[:1], candidates={"x": {"AAA": 1.0}}).value is None
    assert call("portfolio.population", returns=panel, candidates={"x": {"AAA": float("nan")}}).value is None
    assert call("portfolio.population", returns=panel, candidates={"x": {"AAA": 1.0}}, rank_by="luck").value is None
    with pytest.raises(BadInput):
        call("portfolio.population", returns=panel, candidates={})
    with pytest.raises(BadInput):
        call("portfolio.population", returns=panel, candidates={"x": {"ZZZ": 1.0}})
    with pytest.raises(BadInput):
        call("portfolio.population", returns=panel, candidates={f"c{i}": {"AAA": 1.0} for i in range(201)})
    assert registry.describe("portfolio.population").piece_ids == ("SK-112",)

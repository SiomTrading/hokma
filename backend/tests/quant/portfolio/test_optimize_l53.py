"""L-53: the optimizers of the portfolio module are the only ones: risk parity, mean variance, minimum variance, maximum Sharpe,
Black-Litterman and equal weights, all reachable through hokma.quant.portfolio.optimize; the backtest and the factory write none."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.portfolio import _lib

from .conftest import call

HOKMA = Path(__file__).resolve().parents[3] / "hokma"
OPTIMIZER_NAME = re.compile(r"risk_parity|min_variance|minimum_variance|max_sharpe|maximum_sharpe|mean_variance|black_litterman|equal_weight|inverse_vol|risk_budget")
LIBRARIES = {"skfolio", "cvxpy", "scipy", "sklearn", "cvxopt", "pypfopt", "riskfolio"}


def _w(result) -> np.ndarray:
    return np.array(list(result.value["weights"].values()))


def test_l53_risk_parity_weights_are_inverse_to_the_volatility_when_assets_are_uncorrelated(orthogonal):
    out = call("portfolio.risk_parity", returns=orthogonal)                  # volatilities in a ratio of 1 to 2: weights 2/3 and 1/3
    assert out.piece_ids == ("L-53",) and out.value["status"] == "optimal"
    assert out.value["weights"]["a"] == pytest.approx(2 / 3, abs=1e-3) and out.value["weights"]["b"] == pytest.approx(1 / 3, abs=1e-3)


def test_l53_a_risk_budget_moves_the_weights_and_equal_budgets_are_the_default(orthogonal):
    default = call("portfolio.risk_parity", returns=orthogonal)
    equal = call("portfolio.risk_parity", returns=orthogonal, risk_budget={"a": 0.5, "b": 0.5})
    assert _w(equal) == pytest.approx(_w(default), abs=1e-3)               # the solver stops at its default tolerance
    # with a budget of 0.75 and 0.25 the share of the variance that each asset contributes is those numbers
    skewed = call("portfolio.risk_parity", returns=orthogonal, risk_budget={"a": 0.75, "b": 0.25}).value["weights"]
    cov = orthogonal.cov().to_numpy()
    w = np.array([skewed["a"], skewed["b"]])
    contributions = w * (cov @ w)
    assert contributions[0] / contributions.sum() == pytest.approx(0.75, abs=1e-3)


def test_l53_risk_parity_matches_the_library_on_the_fixture(panel):
    out = call("portfolio.risk_parity", returns=panel)
    expected = _lib.sk_optimization.RiskBudgeting().fit(panel).weights_
    assert _w(out) == pytest.approx(expected, abs=1e-8) and _w(out).sum() == pytest.approx(1.0)


def test_l53_risk_parity_bounds_and_bad_input(panel, orthogonal):
    capped = call("portfolio.risk_parity", returns=orthogonal, max_weights=0.6)
    assert max(_w(capped)) <= 0.6 + 1e-6
    assert call("portfolio.risk_parity", returns=orthogonal, risk_budget={"a": 0.0, "b": 1.0}).value is None
    assert call("portfolio.risk_parity", returns=orthogonal, risk_measure="nonsense").value is None
    with pytest.raises(BadInput):
        call("portfolio.risk_parity", returns=orthogonal, risk_budget={"zzz": 1.0})
    with pytest.raises(BadInput):
        call("portfolio.risk_parity", returns=orthogonal, risk_budget=[0.5, 0.5])
    with pytest.raises(BadInput):
        call("portfolio.risk_parity", returns=orthogonal, solver_path="/tmp/x")


def test_l53_black_litterman_weights_follow_a_bullish_view(panel):
    columns = list(panel.columns)
    plain = call("portfolio.baselines", returns=panel, kind="equal_weighted")
    view = [{"assets": {columns[0]: 1}, "value": 0.01}]                       # a large daily mean for the first asset
    out = call("portfolio.black_litterman_weights", returns=panel, views=view, view_confidences=[0.9])
    assert out.piece_ids == ("L-53",) and out.value["status"] == "optimal" and sum(out.value["weights"].values()) == pytest.approx(1.0)
    assert out.value["weights"][columns[0]] > plain.value["weights"][columns[0]]
    none = call("portfolio.black_litterman_weights", returns=panel, views=view, view_confidences=[0.01])
    assert out.value["weights"][columns[0]] >= none.value["weights"][columns[0]] - 1e-9


def test_l53_black_litterman_weights_match_the_library(panel):
    columns = list(panel.columns)
    out = call("portfolio.black_litterman_weights", returns=panel, views=[{"assets": {columns[1]: 1, columns[2]: -1}, "value": 0.001}],
               view_confidences=[0.5], tau=0.05)
    prior = _lib.sk_prior.BlackLitterman(views=[f"1.0 * {columns[1]} - 1.0 * {columns[2]} == 0.001"], view_confidences=[0.5], tau=0.05)
    expected = _lib.sk_optimization.MeanRisk(objective_function=_lib.OBJECTIVES["max_utility"], prior_estimator=prior).fit(panel).weights_
    assert _w(out) == pytest.approx(expected, abs=1e-8)


def test_l53_black_litterman_weights_bad_input(panel):
    columns = list(panel.columns)
    ok = [{"assets": {columns[0]: 1}, "value": 0.001}]
    assert call("portfolio.black_litterman_weights", returns=panel, views=[]).value is None
    assert call("portfolio.black_litterman_weights", returns=panel, views=ok, objective="nonsense").value is None
    assert call("portfolio.black_litterman_weights", returns=panel, views=ok, tau=0).value is None
    with pytest.raises(BadInput):
        call("portfolio.black_litterman_weights", returns=panel, views="all")
    with pytest.raises(BadInput):
        call("portfolio.black_litterman_weights", returns=panel, views=ok, view_confidences=[0.5, 0.5])
    with pytest.raises(BadInput):
        call("portfolio.black_litterman_weights", returns=panel, views=[{"assets": {"ZZZ": 1}, "value": 0.1}])


def test_l53_all_six_optimizers_are_reachable_in_the_module(panel):
    """Paths from the row: risk parity, mean variance (max utility), minimum variance, maximum Sharpe, Black-Litterman, equal weights."""
    rows = {r["function"] for r in call("portfolio.optimizers").value["optimizers"]}
    assert {"portfolio.risk_parity", "portfolio.mean_risk", "portfolio.baselines", "portfolio.black_litterman_weights"} <= rows
    for objective in ("min_risk", "max_utility", "max_ratio"):
        assert call("portfolio.mean_risk", returns=panel, objective=objective).value["status"] == "optimal"
    assert call("portfolio.baselines", returns=panel, kind="equal_weighted").value["weights"] == {c: 0.2 for c in panel.columns}


def test_l53_no_optimizer_is_written_in_the_backtest_or_the_factory():
    """Scan: those packages call quant.portfolio.optimize through the registry; they define no optimizer and import no solver library."""
    for folder in ("backtest", "factory"):
        for path in sorted((HOKMA / folder).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    assert not OPTIMIZER_NAME.search(node.name.lower()), f"{path.relative_to(HOKMA)} defines {node.name}"
                if isinstance(node, ast.Import):
                    assert not {a.name.split(".")[0] for a in node.names} & LIBRARIES, path.name
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    assert node.module.split(".")[0] not in LIBRARIES, path.name

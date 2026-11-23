"""Metrics part B: Monte Carlo views (QS-037a, QS-037b, QS-059a, QS-059b, CC-01)."""
import math

import numpy as np
import pandas as pd
import pytest

from hokma.quant.errors import BadInput
from hokma.quant.metrics import _lib, montecarlo

from .metrics_b_qa import assert_bad_data_gives_none, assert_close, assert_registered, call, returns

R = returns()
V = R.to_numpy()


def test_drawdown_matches_reference_QS_037a():
    assert_registered("metrics.montecarlo_drawdown", "QS-037a")
    res = call("metrics.montecarlo_drawdown", returns=R, runs=200, seed=11, bust=-0.3)
    ref = _lib.returns_stats().montecarlo(R, sims=200, bust=-0.3, seed=11)
    for key, val in ref.maxdd.items():
        assert_close(res.value[key], val)
    assert_close(res.value["bust_probability"], ref.bust_probability)
    assert res.value["runs"] == 200 and res.value["seed"] == 11
    assert res.params["seed"] == 11 and res.params["runs"] == 200
    again = call("metrics.montecarlo_drawdown", returns=R, runs=200, seed=11, bust=-0.3)
    assert again.value == res.value and again.inputs_hash == res.inputs_hash
    other = call("metrics.montecarlo_drawdown", returns=R, runs=200, seed=12, bust=-0.3)
    assert other.value["mean"] != res.value["mean"]
    assert call("metrics.montecarlo_drawdown", returns=R, runs=50, seed=1).value["bust_probability"] is None


def test_drawdown_by_hand_QS_037a():
    values = np.array([-0.1, 0.2])
    default = montecarlo.permutation_max_drawdowns(values, 2, 0)
    with_start = montecarlo.permutation_max_drawdowns(values, 2, 0, include_start_capital=True)
    assert default[0] == pytest.approx(0.0)               # original order: wealth 0.9, 1.08, the peak starts at 0.9
    assert with_start[0] == pytest.approx(-0.1)           # the starting capital 1.0 is the first peak
    assert montecarlo.permutation_max_drawdowns(np.array([0.2, -0.1]), 2, 0)[0] == pytest.approx(1.08 / 1.2 - 1)
    for bad in (0.1, -1.0, -1.5):
        with pytest.raises(BadInput):
            call("metrics.montecarlo_drawdown", returns=R, bust=bad)
    for runs in (1, 0, 10 ** 9, 3.5):
        with pytest.raises(BadInput):
            call("metrics.montecarlo_drawdown", returns=R, runs=runs)
    with pytest.raises(BadInput):
        call("metrics.montecarlo_drawdown", returns=R, seed="x")
    assert_bad_data_gives_none(lambda s: call("metrics.montecarlo_drawdown", returns=s))


def test_drawdown_function_refuses_sharpe_and_cagr_CC_01():
    assert_registered("metrics.montecarlo_drawdown", "CC-01")
    for statistic in ("sharpe", "cagr", "goal_probability"):
        with pytest.raises(BadInput) as err:
            call("metrics.montecarlo_drawdown", returns=R, statistic=statistic)
        assert "montecarlo_sharpe" in str(err.value) and "montecarlo_cagr" in str(err.value)
    with pytest.raises(BadInput):
        montecarlo.drawdown(R, statistic="sharpe")
    only = call("metrics.montecarlo_drawdown", returns=R, runs=30, seed=1, bust=-0.2)
    assert "sharpe" not in only.value and "cagr" not in only.value


def test_permutation_cannot_change_the_totals_CC_01():
    rng = np.random.default_rng(3)
    paths = np.vstack([rng.permutation(V) for _ in range(20)])
    sharpe = paths.mean(axis=1) / paths.std(axis=1, ddof=1)
    growth = np.prod(1 + paths, axis=1)
    assert sharpe.std() < 1e-12 and growth.std() < 1e-10         # permutation leaves Sharpe and CAGR unchanged
    boot = call("metrics.montecarlo_sharpe", returns=R, runs=300, seed=3)
    assert boot.value["std"] > 0.1


def test_bootstrap_paths_use_only_observed_values_and_blocks_QS_037b():
    index_like = np.arange(500, dtype=float)
    paths = montecarlo.bootstrap_paths(index_like, 400, 5, mean_block=10)
    assert paths.shape == (400, 500) and np.isin(paths, index_like).all()
    step = np.diff(paths, axis=1)
    continuing = ((step == 1) | (step == -499)).mean()
    assert continuing == pytest.approx(0.9, abs=0.03)             # blocks last 10 periods on average
    iid = montecarlo.bootstrap_paths(index_like, 400, 5, mean_block=1)
    assert (((np.diff(iid, axis=1) == 1) | (np.diff(iid, axis=1) == -499)).mean()) < 0.02
    assert np.array_equal(paths, montecarlo.bootstrap_paths(index_like, 400, 5, mean_block=10))


def test_sharpe_distribution_QS_037b():
    assert_registered("metrics.montecarlo_sharpe", "QS-037b")
    res = call("metrics.montecarlo_sharpe", returns=R, runs=400, seed=9, mean_block=10)
    paths = montecarlo.bootstrap_paths(V, 400, 9, 10)
    sharpes = paths.mean(axis=1) / paths.std(axis=1, ddof=1) * math.sqrt(252)
    assert_close(res.value["percentile_5"], np.quantile(sharpes, 0.05))
    assert_close(res.value["percentile_50"], np.quantile(sharpes, 0.50))
    assert_close(res.value["percentile_95"], np.quantile(sharpes, 0.95))
    assert res.value["percentile_5"] < res.value["percentile_50"] < res.value["percentile_95"]
    assert res.value["runs"] == 400 and res.value["seed"] == 9 and res.params["mean_block"] == 10
    sample = V.mean() / V.std(ddof=1) * math.sqrt(252)
    assert res.value["percentile_5"] < sample < res.value["percentile_95"]
    assert_bad_data_gives_none(lambda s: call("metrics.montecarlo_sharpe", returns=s))
    with pytest.raises(BadInput):
        call("metrics.montecarlo_sharpe", returns=R, mean_block=0)


def test_cagr_distribution_QS_037b():
    assert_registered("metrics.montecarlo_cagr", "QS-037b")
    res = call("metrics.montecarlo_cagr", returns=R, runs=400, seed=9)
    paths = montecarlo.bootstrap_paths(V, 400, 9, 10)
    growth = np.prod(1 + paths, axis=1) ** (252 / paths.shape[1]) - 1
    for p, key in ((5, "percentile_5"), (50, "percentile_50"), (95, "percentile_95")):
        assert_close(res.value[key], np.quantile(growth, p / 100))
    sample = float(np.prod(1 + V)) ** (252 / len(V)) - 1
    assert res.value["percentile_5"] < sample < res.value["percentile_95"]
    assert res.value["seed"] == 9 and res.value["runs"] == 400
    assert_bad_data_gives_none(lambda s: call("metrics.montecarlo_cagr", returns=s))


def test_goal_probability_QS_037b():
    assert_registered("metrics.montecarlo_goal_probability", "QS-037b")
    reachable = call("metrics.montecarlo_goal_probability", returns=R, goal=-0.999, runs=200, seed=2)
    unreachable = call("metrics.montecarlo_goal_probability", returns=R, goal=100.0, runs=200, seed=2)
    assert reachable.value["goal_probability"] == 1.0 and unreachable.value["goal_probability"] == 0.0
    paths = montecarlo.bootstrap_paths(V, 200, 2, 10)
    final = np.prod(1 + paths, axis=1) - 1
    mid = call("metrics.montecarlo_goal_probability", returns=R, goal=0.0, runs=200, seed=2)
    assert_close(mid.value["goal_probability"], float((final >= 0.0).mean()))
    lower = call("metrics.montecarlo_goal_probability", returns=R, goal=0.2, runs=200, seed=2).value["goal_probability"]
    assert lower <= mid.value["goal_probability"]
    for bad in ("x", float("nan"), None):
        with pytest.raises(BadInput):
            call("metrics.montecarlo_goal_probability", returns=R, goal=bad)
    assert_bad_data_gives_none(lambda s: call("metrics.montecarlo_goal_probability", returns=s, goal=0.1))


def test_summary_container_QS_059a_QS_059b():
    assert_registered("metrics.montecarlo_summary", "QS-059a")
    assert_registered("metrics.montecarlo_summary", "QS-059b")
    res = call("metrics.montecarlo_summary", returns=R, runs=150, seed=4, bust=-0.3, goal=0.05)
    dd = call("metrics.montecarlo_drawdown", returns=R, runs=150, seed=4, bust=-0.3)
    sh = call("metrics.montecarlo_sharpe", returns=R, runs=150, seed=4)
    gp = call("metrics.montecarlo_goal_probability", returns=R, goal=0.05, runs=150, seed=4)
    assert_close(res.value["bust_probability"], dd.value["bust_probability"])
    assert_close(res.value["max_drawdown"]["percentile_5"], dd.value["percentile_5"])
    assert_close(res.value["sharpe"]["percentile_50"], sh.value["percentile_50"])
    assert_close(res.value["goal_probability"], gp.value["goal_probability"])
    assert res.value["runs"] == 150 and res.value["seed"] == 4
    result, reasons = montecarlo.run(R, 150, 4, -0.3, 0.05)
    assert isinstance(result, montecarlo.MonteCarloResult) and not reasons
    assert result.to_dict() == res.value
    no_goal = call("metrics.montecarlo_summary", returns=R, runs=50, seed=1)
    assert no_goal.value["goal_probability"] is None and no_goal.value["bust_probability"] is None
    assert_bad_data_gives_none(lambda s: call("metrics.montecarlo_summary", returns=s))

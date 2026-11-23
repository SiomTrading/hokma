"""Speed checks for every function of metrics part B (FF-027): each runs on 5000 daily observations within a generous limit."""
import time

import numpy as np
import pandas as pd
import pytest

from ..metrics.metrics_b_qa import EXPECTED, call, ensure_registered
from ..metrics.test_metrics_b_attribution import predicted, realized_inputs

N = 5000
RNG = np.random.default_rng(31337)
INDEX = pd.bdate_range("2004-01-05", periods=N)
BENCH = pd.Series(RNG.normal(0.0004, 0.010, N), index=INDEX)
RET = pd.Series(0.0002 + 0.7 * BENCH.to_numpy() + RNG.normal(0.0, 0.007, N), index=INDEX)
FACTORS = pd.DataFrame({"Mkt-RF": BENCH.to_numpy(), "SMB": RNG.normal(0, 0.004, N), "HML": RNG.normal(0, 0.004, N), "Mom": RNG.normal(0, 0.005, N)}, index=INDEX)
NAV = 1000.0 * (1 + RET).cumprod()

CASES: dict[str, tuple[dict, float]] = {}


def _case(name, limit=5.0, **kwargs):
    CASES[name] = (kwargs, limit)


for _n in ("summary_header", "summary_returns", "summary_ratios", "summary_psr_row", "summary_drawdown", "summary_volatility", "summary_operations",
           "summary_expectations", "summary_ruin_row", "summary_streaks", "summary_periods", "summary_quality", "consecutive_streaks", "exposure",
           "win_rate_stats", "risk_of_ruin", "payoff_ratios", "outlier_ratios", "kelly_cap", "tail_view", "performance_stats"):
    _case("metrics." + _n, returns=RET)
for _n in ("compare_periods", "alpha_beta", "versus_benchmark", "capm", "capture_ratios", "fama_decomposition", "market_timing"):
    _case("metrics." + _n, returns=RET, benchmark=BENCH)
_case("metrics.alpha_beta_rolling", returns=RET, benchmark=BENCH, window=126)
_case("metrics.factor_model", returns=RET, factors=FACTORS, model="carhart")
_case("metrics.fund_metrics", nav=NAV, capital=1000.0, benchmark_nav=NAV * 0.9)
_case("metrics.group_stats", limit=15.0, returns_frame=pd.DataFrame({"a": RET, "b": BENCH}))
_case("metrics.calc_stats", series=RET)
_case("metrics.risk_of_ruin_params", win_rate=0.55, avg_win=0.01, avg_loss=0.009)
_case("metrics.montecarlo_drawdown", limit=20.0, returns=RET, runs=500, seed=1, bust=-0.3)
_case("metrics.montecarlo_sharpe", limit=20.0, returns=RET, runs=500, seed=1)
_case("metrics.montecarlo_cagr", limit=20.0, returns=RET, runs=500, seed=1)
_case("metrics.montecarlo_goal_probability", limit=20.0, returns=RET, goal=0.5, runs=500, seed=1)
_case("metrics.montecarlo_summary", limit=40.0, returns=RET, runs=500, seed=1, bust=-0.3, goal=0.5)
_case("metrics.attribution_predicted", **predicted())
_case("metrics.attribution_breakdown", by="asset", **predicted())
_case("metrics.attribution_components", part="idio", **predicted())
_case("metrics.attribution_realized", **realized_inputs())
_case("metrics.attribution_rolling", limit=10.0, window_size=120, step=60, **realized_inputs())
_case("metrics.report_html", limit=30.0, returns=RET, benchmark=BENCH)
_case("metrics.report_example", limit=30.0)
_case("metrics.report_metrics", limit=15.0, returns=RET, benchmark=BENCH)
_case("metrics.report_full", limit=15.0, returns=RET, benchmark=BENCH)
_case("metrics.report_basic", limit=15.0, returns=RET, benchmark=BENCH)
_case("metrics.report_plots", limit=20.0, returns=RET, benchmark=BENCH)
_case("metrics.report_heatmap", returns=pd.DataFrame({"a": RET, "b": BENCH}))


def test_every_function_of_part_b_has_a_speed_case_FF_027():
    assert set(CASES) == set(EXPECTED)


@pytest.mark.parametrize("name", sorted(CASES))
def test_function_runs_within_its_limit_FF_027(name):
    ensure_registered()
    kwargs, limit = CASES[name]
    started = time.perf_counter()
    res = call(name, **kwargs)
    elapsed = time.perf_counter() - started
    assert res.value is not None, (name, res.warnings)
    assert elapsed < limit, f"{name} took {elapsed:.2f}s, limit {limit}s"

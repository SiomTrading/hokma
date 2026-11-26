"""QA pattern for one metric (FF-026): the same checks for every function of metrics part B.

Each metric gets the same four checks: it is registered with its piece id, it matches the reference value of its library on the
fixed fixtures (tolerance 1e-9), it matches a value written by hand in the test, and bad data (empty, constant, NaN) gives value
None with warnings instead of an exception.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from hokma.quant import registry
from hokma.quant.types import QuantResult

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
TOL = 1e-9
PART_B_MODULES = ("summary", "trades", "tail_view", "montecarlo", "attribution", "benchmark", "report")

# registry name -> matrix rows it carries
EXPECTED: dict[str, tuple[str, ...]] = {
    "metrics.summary_header": ("QS-038",), "metrics.summary_returns": ("QS-039",), "metrics.summary_ratios": ("QS-040a",),
    "metrics.summary_psr_row": ("QS-040b",), "metrics.summary_drawdown": ("QS-041",), "metrics.summary_volatility": ("QS-042",),
    "metrics.summary_operations": ("QS-043",), "metrics.summary_expectations": ("QS-044a",), "metrics.summary_ruin_row": ("QS-044b",),
    "metrics.summary_streaks": ("QS-045",), "metrics.summary_periods": ("QS-046",), "metrics.summary_quality": ("QS-048",),
    "metrics.compare_periods": ("QS-034",), "metrics.fund_metrics": ("AH-36",), "metrics.performance_stats": ("FF-001",),
    "metrics.group_stats": ("FF-002",), "metrics.calc_stats": ("FF-003",),
    "metrics.consecutive_streaks": ("QS-006",), "metrics.exposure": ("QS-007",), "metrics.win_rate_stats": ("QS-008",),
    "metrics.risk_of_ruin": ("QS-022a",), "metrics.risk_of_ruin_params": ("QS-022a",), "metrics.payoff_ratios": ("QS-025",),
    "metrics.outlier_ratios": ("QS-027",), "metrics.kelly_cap": ("QS-031",), "metrics.tail_view": ("QS-023",),
    "metrics.montecarlo_drawdown": ("QS-037a", "QS-059a", "CC-01"), "metrics.montecarlo_sharpe": ("QS-037b", "QS-059b"),
    "metrics.montecarlo_cagr": ("QS-037b", "QS-059b"), "metrics.montecarlo_goal_probability": ("QS-037b", "QS-059b"),
    "metrics.montecarlo_summary": ("QS-059a", "QS-059b"),
    "metrics.attribution_predicted": ("SK-082",), "metrics.attribution_breakdown": ("SK-081",), "metrics.attribution_components": ("SK-084",),
    "metrics.attribution_realized": ("SK-083",), "metrics.attribution_rolling": ("SK-083",),
    "metrics.alpha_beta": ("QS-033",), "metrics.alpha_beta_rolling": ("QS-033",), "metrics.versus_benchmark": ("QS-050",), "metrics.capm": ("FT-069",),
    "metrics.factor_model": ("FT-070",), "metrics.capture_ratios": ("FT-073",), "metrics.fama_decomposition": ("FT-075",), "metrics.market_timing": ("FT-076",),
    "metrics.report_html": ("QS-051", "QS-056", "CC-15", "QS-057"), "metrics.report_example": ("QS-062",), "metrics.report_metrics": ("QS-054",),
    "metrics.report_full": ("QS-052",), "metrics.report_basic": ("QS-053",), "metrics.report_plots": ("QS-055", "QS-057", "FF-020"),
    "metrics.report_heatmap": ("FF-020",),
}


# Functions of trades.py added for the backtest result (SPEC-020): reloading that module registers them again, so they are cleared with the rest.
EXTRA_IN_TRADES = ("metrics.trade_stats", "metrics.sqn", "metrics.trade_streaks")


def ensure_registered() -> None:
    """The registry can be cleared by other test files; load the part B modules again when that happened."""
    if all(any(s.name == name for s in registry.list("metrics")) for name in EXPECTED):
        return
    with registry._LOCK:
        for name in (*EXPECTED, *EXTRA_IN_TRADES):
            registry._FUNCTIONS.pop(name, None)
    for mod in PART_B_MODULES:
        full = f"hokma.quant.metrics.{mod}"
        if full in sys.modules:
            importlib.reload(sys.modules[full])
        else:
            importlib.import_module(full)


def returns() -> pd.Series:
    return pd.read_csv(FIXTURES / "returns_daily.csv", parse_dates=["date"], index_col="date")["return"]


def benchmark() -> pd.Series:
    return pd.read_csv(FIXTURES / "benchmark_daily.csv", parse_dates=["date"], index_col="date")["return"]


def bad_series() -> dict[str, pd.Series]:
    idx = pd.bdate_range("2022-01-03", periods=60)
    return {"empty": pd.Series([], dtype=float), "constant": pd.Series(0.01, index=idx), "nan": pd.Series(np.nan, index=idx)}


def call(name: str, **kwargs: Any) -> QuantResult:
    ensure_registered()
    return registry.call(name, **kwargs)


def assert_registered(name: str, row_id: str) -> None:
    ensure_registered()
    spec = registry.describe(name)
    assert row_id in spec.piece_ids, f"{row_id} missing in {spec.piece_ids}"
    assert any(s.name == name and row_id in s.piece_ids for s in registry.list("metrics"))


def assert_bad_data_gives_none(make: Callable[[pd.Series], QuantResult]) -> None:
    """make(series) calls the function with a bad series in the main slot."""
    ensure_registered()
    for label, series in bad_series().items():
        res = make(series)
        assert isinstance(res, QuantResult), label
        assert res.value is None, f"{label}: value should be None, got {res.value!r}"
        assert res.warnings, f"{label}: warnings should explain the problem"


def assert_close(actual: Any, expected: Any, tol: float = TOL, path: str = "") -> None:
    """Recursive comparison of floats, lists and dicts with absolute-or-relative tolerance."""
    if isinstance(expected, dict):
        assert isinstance(actual, dict), path
        for key, val in expected.items():
            assert key in actual, f"{path}/{key} missing"
            assert_close(actual[key], val, tol, f"{path}/{key}")
    elif isinstance(expected, (list, tuple, np.ndarray)):
        assert len(actual) == len(expected), path
        for i, (a, e) in enumerate(zip(actual, expected)):
            assert_close(a, e, tol, f"{path}[{i}]")
    elif expected is None:
        assert actual is None, f"{path}: {actual!r} is not None"
    elif isinstance(expected, str):
        assert actual == expected, path
    else:
        e = float(expected)
        if np.isnan(e):
            assert actual is None, f"{path}: expected undefined, got {actual!r}"
        else:
            assert actual is not None, f"{path}: got None, expected {e}"
            assert abs(float(actual) - e) <= tol * max(1.0, abs(e)), f"{path}: {actual} != {e}"

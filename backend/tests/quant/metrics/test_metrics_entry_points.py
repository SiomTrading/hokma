"""Public entry points of the metrics module (SPEC-017, section 4.4) and the blocked probabilistic functions."""
import numpy as np
import pandas as pd
import pytest

from hokma.quant import metrics, registry
from hokma.quant.errors import BadInput, BlockedFunction

R = pd.read_csv(__import__("pathlib").Path(__file__).resolve().parents[1] / "fixtures" / "returns_daily.csv", index_col=0, parse_dates=True)["return"]
B = pd.read_csv(__import__("pathlib").Path(__file__).resolve().parents[1] / "fixtures" / "benchmark_daily.csv", index_col=0, parse_dates=True)["return"]


def test_compute_runs_named_metrics():
    out = metrics.compute(R, ["sharpe", "sortino"], rf=0.0)
    assert set(out) == {"sharpe", "sortino"}
    expected = R.mean() / R.std(ddof=1) * np.sqrt(252)
    assert out["sharpe"].value == pytest.approx(expected, abs=1e-9)


def test_compute_refuses_blocked_functions():
    registry.load_all()
    with pytest.raises(BlockedFunction) as err:
        metrics.compute(R, ["probabilistic_sharpe_ratio"])
    assert err.value.replacement == "validation.psr"


def test_psr_is_the_validation_one():
    from hokma.quant import validation
    assert metrics.psr(R).value == validation.psr(R).value


def test_summary_and_report():
    table = metrics.summary(R, benchmark=B)
    assert {"header", "returns", "ratios", "psr"} <= set(table)
    html = metrics.report_html(R, benchmark=B, title="Fixture report")
    assert html.startswith("<") and "Fixture report" in html and "http://" not in html and "https://" not in html
    with pytest.raises(BadInput):
        metrics.report_html(pd.Series([], dtype=float), title="x")

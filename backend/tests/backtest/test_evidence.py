"""Evidence for the judge (SPEC-020, 4.2): the H1 bundle with the number of trials of the ledger, the cost in units of Sharpe, the ambiguous bars and
the warnings. The backtest delivers numbers and approves nothing."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import numpy as np
import pytest

from backtest import search_support as ss
from backtest.search_support import FakeLedger
from hokma.backtest import optimize, placebo, results, sweep, walkforward
from hokma.backtest.results import BacktestResult, EquityCurve, Trade
from hokma.backtest.walkforward import WalkForwardError, assemble as evidence, returns_matrix
from hokma.quant import metrics
from hokma.quant.validation.evidence import EvidenceBundle, bundle

UTC = timezone.utc
GRID = [{"fast": f, "slow": s} for f in (5, 10, 20) for s in (30, 60)]


def tried():
    """The per bar result of six combinations of the cross on a long series made of real fixture moves, as returns of 1,000,000."""
    bars = ss.chained_bars(1500)
    out = sweep.signal_sweep(bars, ss.builder, GRID, ss.costs_of(bars), per_bar=True)
    return returns_matrix(out.pnl, GRID, capital_usd=1_000_000.0, index=[b.ts for b in bars]), out


def run_result(daily_returns, *, ambiguous=0, closed=10, costs=("300", "200")) -> BacktestResult:
    """A finished run: `closed` trades, 500 USD of costs, and an equity curve that follows `daily_returns` from 1,000,000 on consecutive days."""
    start = datetime(2026, 1, 5, 21, 0, tzinfo=UTC)
    stamps = tuple(start + timedelta(days=k) for k in range(len(daily_returns)))
    equity, value = [], 1_000_000.0
    for r in daily_returns:
        value *= 1 + r
        equity.append(D(repr(round(value, 6))))
    trades = tuple(Trade("NQ", "long", D(1), start + timedelta(days=k), D(1), start + timedelta(days=k, hours=1), D(2), D(1), D(0)) for k in range(closed))
    return BacktestResult(1, "done", "h", "d", "engine-1.0", 7, trades, EquityCurve(stamps, tuple(equity)), D(0), D(0),
                          {"commission": D(costs[0]), "slippage": D(costs[1]), "swap": D(0), "impact": D(0)}, ambiguous, {}, {}, (),
                          tuple(start for _ in range(ambiguous)))


FAST = {"n_blocks": 8}                                                              # 70 splits for PBO instead of 12870: the same code, a test that runs in seconds
WINNING = [0.002, -0.001] * 50
LOSING = [-0.002, 0.001] * 50


def ledger_with(strategy_id, rows, failed):
    ledger = FakeLedger()
    for k in range(rows):
        ledger.record(strategy_id, "h", None, {"k": k}, "failed" if k < failed else "done", None if k < failed else 1.0)
    return ledger


def test_the_dsr_uses_the_trials_of_the_ledger_failed_ones_included_never_a_number_from_the_caller():
    matrix, _ = tried()
    sub = matrix.iloc[:, :5]
    ledger = ledger_with("alpha", 40, 3)                                          # 40 runs, 3 of them failed: 40 trials
    got = evidence("alpha", 1, matrix=sub, result=run_result(WINNING), ledger=ledger, **FAST)
    assert isinstance(got, EvidenceBundle) and got.n_trials == 40 and got.parameters["ledger_trials"] == 40
    assert got.dsr == bundle(sub, n_trials=40, n_blocks=8).dsr and got.sr0 == bundle(sub, n_trials=40, n_blocks=8).sr0
    assert got.sr0 > bundle(sub, n_trials=5, n_blocks=8).sr0                      # the bar to beat is the one of 40 trials, not of 5 columns (the DSR itself saturates at 1)
    assert got.pbo == bundle(sub, n_trials=40, n_blocks=8).pbo and got.leakage_free is True
    assert evidence("other", 1, matrix=sub, result=run_result(WINNING), ledger=ledger, **FAST).n_trials == 5       # another strategy: no runs counted, the 5 columns are the floor


def test_a_ledger_with_fewer_trials_than_columns_never_under_counts():
    matrix, _ = tried()
    sub = matrix.iloc[:, :5]
    got = evidence("alpha", 1, matrix=sub, result=run_result(WINNING), ledger=ledger_with("alpha", 2, 0), **FAST)
    assert got.n_trials == 5 and "trials_below_columns:2<5" in got.warnings


def test_the_cost_in_units_of_sharpe_is_trips_per_year_times_cost_per_trip_over_the_annual_volatility():
    matrix, _ = tried()
    result = run_result(WINNING)
    got = evidence("alpha", 1, matrix=matrix.iloc[:, :5], result=result, ledger=ledger_with("alpha", 6, 0), **FAST)
    returns = results.equity_returns(result.equity)
    years = (result.equity.timestamps[-1] - result.equity.timestamps[0]).total_seconds() / (365.25 * 86400)       # 99 days
    per_trip = 500 / 10 / float(result.equity.equity_usd[0])                                                          # 500 USD of costs over 10 trades, as a share of capital
    expected = (10 / years) * per_trip / (float(np.std(returns.to_numpy(), ddof=1)) * 252 ** 0.5)
    cost = got.parameters["cost_in_sharpe"]
    assert cost["cost_sharpe"] == pytest.approx(expected, rel=1e-12) and cost["round_trips_per_year"] == pytest.approx(10 / years)
    assert cost["net_sharpe"] == pytest.approx(metrics.compute(returns, ["sharpe"])["sharpe"].value) and "costs_kill_edge" not in got.warnings
    losing = evidence("alpha", 1, matrix=matrix.iloc[:, :5], result=run_result(LOSING), ledger=ledger_with("alpha", 6, 0), **FAST)
    assert losing.parameters["cost_in_sharpe"]["net_sharpe"] < 0 and "costs_kill_edge" in losing.warnings


def test_a_run_without_trades_or_volatility_says_the_cost_is_unavailable_instead_of_failing():
    matrix, _ = tried()
    got = evidence("alpha", 1, matrix=matrix.iloc[:, :5], result=run_result(WINNING, closed=0), ledger=ledger_with("alpha", 6, 0), **FAST)
    assert got.parameters["cost_in_sharpe"] == {"cost_sharpe": None, "net_sharpe": None} and "cost_in_sharpe_unavailable:bad_spec" in got.warnings


def test_the_ambiguous_bars_and_the_warnings_of_the_run_reach_the_judge():
    matrix, _ = tried()
    result = run_result(WINNING, ambiguous=3)
    result = BacktestResult(*[getattr(result, f) for f in ("run_id", "status", "spec_hash", "data_hash", "engine_version", "seed", "trades", "equity",
                                                            "realized_pnl_usd", "mtm_pnl_usd", "costs_usd", "ambiguous_bars", "rejected", "stats")],
                            ("historical_margin_missing",), result.ambiguous_bar_dates)
    got = evidence("alpha", 2, matrix=matrix.iloc[:, :5], result=result, ledger=ledger_with("alpha", 6, 0), **FAST)
    assert got.parameters["ambiguous_bars"] == 3 and len(got.parameters["ambiguous_bar_dates"]) == 3 and "ambiguous_bars:3" in got.warnings
    assert "historical_margin_missing" in got.warnings and got.parameters["strategy_version"] == 2
    assert not any(w.startswith("ambiguous_bars") for w in evidence("alpha", 1, matrix=matrix.iloc[:, :5], result=run_result(WINNING), ledger=ledger_with("alpha", 6, 0), **FAST).warnings)


def test_the_walk_forward_and_the_monte_carlo_travel_with_the_evidence_and_the_payload_is_plain_json():
    matrix, swept = tried()
    wf = walkforward.run(GRID, swept.pnl, scheme="rolling", n_folds=4, purge=0, embargo=0)
    mc = placebo.MonteCarloResult(1, "placebo", 100, 3, {"p": 1}, (), 0.25)
    got = evidence("alpha", 1, matrix=matrix.iloc[:, :5], result=run_result(WINNING), ledger=ledger_with("alpha", 6, 0), walk_forward=wf, monte_carlo=mc, **FAST)
    assert got.parameters["walk_forward_run"] == wf.to_dict() and got.parameters["monte_carlo"] == mc.to_dict()
    payload = got.judge_payload()
    text = json.dumps(payload, allow_nan=False)
    assert json.loads(text)["parameters"]["walk_forward_run"]["scheme"] == "rolling" and payload["content_hash"] == got.judge_payload()["content_hash"]
    assert not any(key in payload for key in ("approved", "passed", "verdict", "decision"))               # the backtest delivers evidence and decides nothing


def test_too_little_data_still_returns_the_bundle_with_the_reason_and_the_extras():
    matrix, _ = tried()
    got = evidence("alpha", 1, matrix=matrix.iloc[:10, :5], result=run_result(WINNING), ledger=ledger_with("alpha", 6, 0), **FAST)
    assert got.dsr is None and got.pbo is None and any("need at least" in w for w in got.warnings) and got.parameters["ledger_trials"] == 6


def test_the_arguments_are_checked_and_the_ledger_is_required(monkeypatch):
    matrix, _ = tried()
    with pytest.raises(WalkForwardError):
        evidence("", 1, matrix=matrix, result=run_result(WINNING), ledger=FakeLedger())
    with pytest.raises(WalkForwardError):
        evidence("alpha", 0, matrix=matrix, result=run_result(WINNING), ledger=FakeLedger())
    ss.hide_trials(monkeypatch)
    with pytest.raises(optimize.LedgerMissing):
        evidence("alpha", 1, matrix=matrix, result=run_result(WINNING))
    with pytest.raises(WalkForwardError):
        returns_matrix(np.zeros((5, 2)), GRID[:3], capital_usd=1.0)
    with pytest.raises(WalkForwardError):
        returns_matrix(np.zeros((5, 3)), GRID[:3], capital_usd=0.0)


def test_the_returns_matrix_is_the_pnl_over_the_capital_with_one_named_column_per_combination():
    frame = returns_matrix(np.array([[10.0, -5.0], [20.0, 0.0]]), [{"fast": 5, "slow": 30}, {"fast": 10, "slow": 60}], capital_usd=1000.0)
    assert list(frame.columns) == ["fast=5|slow=30", "fast=10|slow=60"] and frame.to_numpy().tolist() == [[0.01, -0.005], [0.02, 0.0]]

"""The run of a backtest from a spec to a saved result (SPEC-020, section 4.2): the numbers are the hand computed ones of the NQ scenarios."""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest
from backtest import support
from backtest.runner_support import NQ, Fixed, Ticks, source_of, spec
from backtest.support import requires_engine
from dbsupport import db, env  # noqa: F401  (fixtures)
from exec_support import desks_db, make_desk  # noqa: F401  (fixtures and helper)

from hokma.backtest import results, runner, trials

pytestmark = requires_engine
UTC = timezone.utc


def go(bars, strategy, db, tmp_path, **over):
    kw = {k: over.pop(k) for k in ("source", "desk_limits", "hypothesis_id", "registry", "latency_ms") if k in over}
    kw.setdefault("source", source_of({NQ: bars}))
    return runner.run(spec(bars, **over), strategy=strategy, db=db, curve_dir=tmp_path, **kw)


def rows(db, sql, **params):
    with db.read() as u:
        return u.session.query(sql, params)


def test_a_strategy_of_the_factory_runs_the_scenario_t1_and_saves_it(db, tmp_path):
    bars = support.bars_of("T1")
    result = go(bars, Fixed(), db, tmp_path)
    assert result.status == "done" and result.realized_pnl_usd == D("100.50") and result.mtm_pnl_usd == D("100.50")
    (trade,) = result.trades
    # entry 25637.25 + 1 tick, exit 25643.00 - 1 tick: 5.25 * 20 = 105.00 less 2 * 2.25 of commission
    assert (trade.entry_price, trade.exit_price, trade.pnl_usd, trade.costs_usd) == (D("25637.50"), D("25642.75"), D("100.50"), D("4.50"))
    assert trade.entry_tag == "fixed" and trade.exit_tag == "exit" and result.ambiguous_bars == 0
    assert result.spec_hash == spec(bars).fingerprint() and len(result.data_hash) == 64 and result.rejected == {"reject_prob": 0}
    assert result.costs_usd["commission"] == D("4.50") and result.costs_usd["slippage"] == D("10.00")
    (saved,) = rows(db, "SELECT * FROM backtest_runs")
    assert saved["status"] == "done" and saved["kind"] == "run" and saved["spec_hash"] == result.spec_hash and saved["data_hash"] == result.data_hash
    assert saved["result_path"] == f"curve-{result.run_id}.json" and (tmp_path / saved["result_path"]).is_file()
    (stored,) = rows(db, "SELECT * FROM backtest_trades")
    assert (stored["run_id"], stored["pnl_usd"], stored["entry_tag"]) == (result.run_id, "100.50", "fixed")
    assert trials.count("t") == 1                                           # the run is a trial of its strategy
    assert results.load_trades(db, result.run_id) == result.trades


def test_the_statistics_come_from_quant_and_the_curve_is_marked_at_every_bar(db, tmp_path):
    from backtest.session_support import session_bars
    from hokma.quant import metrics

    bars = session_bars("daily")[:40]
    result = go(bars, Fixed(enter_at=10, exit_at=30, tag="swing"), db, tmp_path, line="SWC", bar="1d")
    assert len(result.equity.timestamps) == 40
    # decided at the close of bar 9 and filled at the open of bar 10 plus a tick; closed at the open of bar 30 less a tick
    entry, exit_ = bars[10].open + D("0.25"), bars[30].open - D("0.25")
    (trade,) = result.trades
    assert (trade.entry_price, trade.exit_price) == (entry, exit_) and trade.pnl_usd == (exit_ - entry) * 20 - D("4.50")
    assert result.equity.equity_usd[-1] == D(1_000_000) + trade.pnl_usd and result.realized_pnl_usd == trade.pnl_usd
    # the statistics are the ones of hokma.quant on the daily returns of the curve, never the ones of the engine
    expected = metrics.compute(results.equity_returns(result.equity), ["sharpe"])["sharpe"]
    assert result.stats["sharpe"].value == expected.value and result.stats["sharpe"].value is not None
    assert {"sharpe", "drawdown", "trade_stats", "psr"} <= set(result.stats)


def test_a_protective_stop_runs_in_the_engine_and_the_trade_has_its_mae_and_mfe(db, tmp_path):
    bars = support.bars_of("T2")
    result = go(bars, Fixed(enter_at=2, exit_at=None, sl=Ticks(20), tp=Ticks(20), protective=True), db, tmp_path)
    (trade,) = result.trades
    # entry 25471.75 + 1 tick = 25472.00; stop 5 points under the open, 25466.75, filled one tick lower: 25466.50; (25466.50 - 25472.00) * 20 = -110 - 4.50
    assert (trade.entry_price, trade.exit_price, trade.pnl_usd) == (D("25472.00"), D("25466.50"), D("-114.50"))
    assert result.ambiguous_bars == 1 and trade.exit_tag == "stop"
    # the bars of the trade are 05:05 and 05:06: the lowest low 25465.25 and the highest high 25478.50 against the entry 25472.00
    assert trade.mae_usd == (D("25465.25") - D("25472.00")) * 20 == D("-135.00") and trade.mfe_usd == (D("25478.50") - D("25472.00")) * 20 == D("130.00")


def test_the_indicator_values_at_the_entry_are_kept_with_the_trade(db, tmp_path):
    class WithIndicator(Fixed):
        def init(self, ctx):
            ctx.indicator("last_close", lambda values: values, warmup=1)

    bars = support.bars_of("T1")
    (trade,) = go(bars, WithIndicator(), db, tmp_path).trades
    assert trade.context == {"last_close": 25638.25}                        # the close of the bar 14:59 after which the order was decided


def test_the_same_spec_and_data_and_seed_give_the_same_result_byte_for_byte(db, tmp_path):
    bars = support.bars_of("T1")
    first = go(bars, Fixed(), db, tmp_path / "a", seed=5)
    second = go(bars, Fixed(), db, tmp_path / "b", seed=5)
    assert first.run_id != second.run_id
    assert results.to_json(replace(first, run_id=0)) == results.to_json(replace(second, run_id=0))
    assert first.spec_hash == second.spec_hash and first.data_hash == second.data_hash
    other = go(bars, Fixed(), db, tmp_path / "c", seed=6)                  # a seed changes only what is random: nothing is, with the one tick model
    assert results.to_json(replace(other, run_id=0, seed=5, spec_hash=first.spec_hash)) == results.to_json(replace(first, run_id=0))


def test_a_run_of_several_instruments_is_refused_and_the_engine_absent_raises_before_any_row(db, tmp_path):
    bars = support.bars_of("T1")
    with pytest.raises(runner.SpecError):
        runner.run(spec(bars, instruments=("CONT:NQ", "CONT:ES")), strategy=Fixed(), db=db)
    assert rows(db, "SELECT * FROM backtest_runs") == []


# ---------------------------------------------------------------------------------------------------------------- B-12
def test_a_run_that_loses_all_the_capital_ends_account_blown_with_its_date_and_final_capital_B_12(db, tmp_path):
    # T3: a short of 1 NQ with a stop 10 points over the open of the Friday close; the Sunday gap fills the stop at 25321.00 for -2059.50.
    # With 2000 USD of capital the equity at the close of the Sunday bar of 23:00 is 2000 - 2059.50 = -59.50: the account is blown there.
    bars = support.bars_of("T3")
    plan = Fixed(enter_at=2, exit_at=None, side="sell", sl=Ticks(40), protective=True)
    result = go(bars, plan, db, tmp_path, capital_usd=D(2000))
    assert result.status == "account_blown", rows(db, "SELECT error FROM backtest_runs")
    assert "account_blown_at=2025-11-09T23:01:00+00:00" in result.warnings and "final_capital_usd=-59.50" in result.warnings
    assert result.equity.equity_usd[-1] == D("-59.50") and result.equity.timestamps[-1] == datetime(2025, 11, 9, 23, 1, tzinfo=UTC)
    assert result.realized_pnl_usd == D("-2059.50") and result.mtm_pnl_usd == D("-2059.50")
    (saved,) = rows(db, "SELECT status FROM backtest_runs")
    assert saved["status"] == "account_blown"
    assert all(t.exit_at is None or t.exit_at <= datetime(2025, 11, 9, 23, 1, tzinfo=UTC) for t in result.trades)


def test_the_capital_floor_of_the_desk_stops_the_run_and_closes_the_position_B_12(db, tmp_path):
    # T1 long: after the entry bar 15:00 the equity is 1,000,000 - 55.00 - 2.25 = 999,942.75, under a floor of 999,950: the run stops at that bar,
    # the position is closed at its close 25634.75 (tag account_blown, no commission) and the exit of 15:30 never happens.
    bars = support.bars_of("T1")
    result = go(bars, Fixed(), db, tmp_path, desk_limits={"capital_floor_usd": 999_950})
    assert result.status == "account_blown" and result.equity.equity_usd[-1] == D("999942.75") and len(result.equity.equity_usd) == 3
    (trade,) = result.trades
    assert trade.exit_tag == "account_blown" and trade.exit_price == D("25634.75") and trade.exit_at == datetime(2025, 11, 10, 15, 1, tzinfo=UTC)
    assert trade.pnl_usd == D("-57.25") and result.realized_pnl_usd == D("-57.25")           # (25634.75 - 25637.50) * 20 = -55.00, less the entry commission
    assert "final_capital_usd=999942.75" in result.warnings


def test_a_run_that_never_reaches_the_floor_is_not_stopped_B_12(db, tmp_path):
    bars = support.bars_of("T1")
    result = go(bars, Fixed(), db, tmp_path, desk_limits={"capital_floor_usd": 500_000})
    assert result.status == "done" and result.realized_pnl_usd == D("100.50")


# ---------------------------------------------------------------------------------------------------------------- the filter of a desk (L-48, N-20)
def test_a_run_with_a_desk_applies_its_limits_through_the_guards_of_h2(desks_db, tmp_path):
    desk = make_desk(desks_db, name="NQ desk", capital=1_000_000)
    desk_id = desk["id"] if isinstance(desk, dict) else desk.id
    bars = support.bars_of("T1")
    # one NQ at 25637.25 is 512,745 USD of notional: 51 percent of the capital, over a ceiling of 40 percent
    # the entry carries a stop, as a future must to enter (SPEC-026): 2000 ticks is far from every price of the scenario, so the trade does not change
    blocked = go(bars, Fixed(sl=Ticks(2000), protective=True), desks_db, tmp_path, desk_id=desk_id, desk_limits={"max_position_pct": 40})
    assert blocked.status == "done" and blocked.trades == () and blocked.rejected["max_position_pct"] == 1 and blocked.realized_pnl_usd == 0
    # a ceiling of 60 percent lets it through: the same trade as a run with no desk
    passed = go(bars, Fixed(sl=Ticks(2000), protective=True), desks_db, tmp_path / "p", desk_id=desk_id, desk_limits={"max_position_pct": 60})
    assert passed.realized_pnl_usd == D("100.50") and "max_position_pct" not in passed.rejected
    # without a desk the limits are not applied: the run is the plain run
    plain = go(bars, Fixed(), desks_db, tmp_path / "q", desk_limits={"max_position_pct": 40})
    assert plain.realized_pnl_usd == D("100.50")
    with desks_db.read() as u:
        assert u.session.scalar("SELECT desk_id FROM backtest_runs WHERE id = :i", {"i": passed.run_id}) == desk_id

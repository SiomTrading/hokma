"""Criterion 2 of SPEC-020: a backtest fills at the next open plus one tick and the stop goes first in the same bar, with the engine library untouched. The four
real NQ scenarios give exactly 100.50, -114.50 (long and short), -2059.50 and -49.50 USD through `hokma.backtest.run`, and every statistic comes from
`hokma.quant`, never from the engine."""
from decimal import Decimal as D

import pytest
from backtest import support
from backtest.runner_support import NQ, Fixed, Ticks, source_of, spec
from backtest.support import requires_engine
from dbsupport import db, env  # noqa: F401  (fixtures)

from hokma import backtest
from hokma.backtest import results
from hokma.backtest.runner import Services
from hokma.quant import metrics, validation
from hokma.quant.types import QuantResult

pytestmark = requires_engine


def closed_bars_after(name: str, key: str) -> int:
    """The number of closed bars when the order of the scenario is decided: its bar is the one that has just closed."""
    bars, wanted = support.bars_of(name), support.load_fixture()["scenarios"][name]["decisions"][key]
    return next(i for i, b in enumerate(bars) if b.ts.strftime("%Y-%m-%dT%H:%M:%SZ") == wanted) + 1


def run(name: str, strategy, db, tmp_path, **over):
    bars = support.bars_of(name)
    services = Services(db=db, source=source_of({NQ: bars}), curve_dir=tmp_path, strategy=lambda params: strategy)
    return backtest.run(spec(bars, **over), services=services)


def test_t1_buy_at_the_next_open_plus_a_tick_and_sell_at_the_next_open_minus_a_tick_is_100_50(db, tmp_path):
    plan = Fixed(enter_at=closed_bars_after("T1", "buy_after_close_of"), exit_at=closed_bars_after("T1", "sell_after_close_of"))
    result = run("T1", plan, db, tmp_path)
    (trade,) = result.trades
    assert (trade.entry_price, trade.exit_price, trade.pnl_usd, trade.costs_usd) == (D("25637.50"), D("25642.75"), D("100.50"), D("4.50"))
    assert result.realized_pnl_usd == result.mtm_pnl_usd == D("100.50")


def test_t2_the_stop_fills_first_when_stop_and_target_share_a_bar_for_a_long_and_for_a_short(db, tmp_path):
    enter = closed_bars_after("T2", "enter_after_close_of")
    for side, entry, exit_ in (("buy", D("25472.00"), D("25466.50")), ("sell", D("25471.50"), D("25477.00"))):
        result = run("T2", Fixed(enter_at=enter, exit_at=None, side=side, sl=Ticks(20), tp=Ticks(20), protective=True), db, tmp_path / side)
        (trade,) = result.trades
        assert (trade.entry_price, trade.exit_price, trade.pnl_usd) == (entry, exit_, D("-114.50")), side
        assert result.ambiguous_bars == 1 and trade.exit_tag == "stop"                       # the target was in the same bar and did not win


def test_t3_a_gap_over_the_stop_fills_at_the_open_of_the_gap_plus_a_tick_not_at_the_stop(db, tmp_path):
    enter = closed_bars_after("T3", "enter_after_close_of")
    result = run("T3", Fixed(enter_at=enter, exit_at=None, side="sell", sl=Ticks(40), protective=True), db, tmp_path)
    (trade,) = result.trades
    assert (trade.entry_price, trade.exit_price, trade.pnl_usd) == (D("25218.25"), D("25321.00"), D("-2059.50"))
    assert D("25321.00") > D("25228.50")                                                      # the stop was at 25228.50: the fill is far beyond it


def test_t4_a_decision_at_the_close_fills_at_the_next_open_with_the_tick_against(db, tmp_path):
    plan = Fixed(enter_at=closed_bars_after("T4", "buy_after_close_of"), exit_at=closed_bars_after("T4", "sell_after_close_of"))
    (trade,) = run("T4", plan, db, tmp_path).trades
    assert (trade.entry_price, trade.exit_price, trade.pnl_usd, trade.costs_usd) == (D("25552.00"), D("25549.75"), D("-49.50"), D("4.50"))


@pytest.mark.parametrize("name", ["T1", "T4"])
def test_the_statistics_are_quant_results_computed_by_hokma_quant_and_not_by_the_engine(db, tmp_path, name):
    plan = Fixed(enter_at=closed_bars_after(name, "buy_after_close_of"), exit_at=closed_bars_after(name, "sell_after_close_of"))
    result = run(name, plan, db, tmp_path)
    returns = results.equity_returns(result.equity)
    assert {"sharpe", "drawdown", "trade_stats", "psr"} <= set(result.stats) and all(isinstance(v, QuantResult) for v in result.stats.values())
    assert result.stats["sharpe"].value == metrics.compute(returns, ["sharpe"])["sharpe"].value
    assert result.stats["psr"].value == validation.psr(returns).value
    assert all(v.method and v.piece_ids for v in result.stats.values())                       # each carries its method and the pieces that justify it

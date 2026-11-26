"""The edges of the statistics of a run (SPEC-020: B-43): no trades, one trade and a position still open at the end of the data. None of them raises."""
from decimal import Decimal as D

from backtest.runner_support import Fixed, source_of, spec
from backtest.session_support import session_bars
from backtest.support import requires_engine
from dbsupport import db, env  # noqa: F401  (fixtures)

from hokma.backtest import runner

pytestmark = requires_engine
DAILY = session_bars("daily")[:40]


def go(strategy, db, tmp_path, bars=DAILY):
    return runner.run(spec(bars, line="SWC", bar="1d"), strategy=strategy, source=source_of({"CONT:NQ": bars}), db=db, curve_dir=tmp_path)


def test_a_run_with_no_trades_has_metrics_of_none_with_a_warning_and_never_raises_B_43(db, tmp_path):
    result = go(Fixed(enter_at=10_000), db, tmp_path)                    # a strategy that never orders
    assert result.status == "done" and result.trades == () and result.realized_pnl_usd == 0 and result.mtm_pnl_usd == 0
    assert "no_trades" in result.warnings and result.stats                  # the table exists: its values say there is nothing to measure
    assert result.stats["sharpe"].value is None and result.stats["sharpe"].warnings
    assert "trade_stats" not in result.stats                                # no trade level block is invented
    assert set(result.equity.equity_usd) == {D(1_000_000)}


def test_one_trade_has_no_sqn_B_43(db, tmp_path):
    result = go(Fixed(enter_at=10, exit_at=30), db, tmp_path)
    assert len(result.trades) == 1 and result.warnings.count("no_trades") == 0
    assert result.stats["sqn"].value is None and result.stats["sqn"].warnings        # the SQN needs at least two trades
    assert result.stats["trade_stats"] is not None and result.stats["sharpe"].value is not None


def test_a_position_open_at_the_end_separates_what_was_realized_from_what_is_at_market_B_43(db, tmp_path):
    result = go(Fixed(enter_at=10, exit_at=None), db, tmp_path)
    (trade,) = result.trades
    assert trade.exit_at is None and trade.exit_price is None and trade.pnl_usd is None            # the position is still open
    assert "position_open_at_end" in result.warnings
    # filled at the open of bar 10 plus a tick, marked at the last close of the 40 bars, less the entry commission
    entry = DAILY[10].open + D("0.25")
    marked = (DAILY[-1].close - entry) * 20 - D("2.25")
    assert result.realized_pnl_usd == 0 and result.mtm_pnl_usd == marked and result.equity.equity_usd[-1] == D(1_000_000) + marked
    assert marked != 0


def test_a_run_that_closes_at_the_end_has_it_realized_and_not_open_B_43(db, tmp_path):
    from dataclasses import replace

    bars = DAILY
    result = runner.run(replace(spec(bars, line="SWC", bar="1d"), finalize="close_at_end"), strategy=Fixed(enter_at=10, exit_at=None),
                        source=source_of({"CONT:NQ": bars}), db=db, curve_dir=tmp_path)
    (trade,) = result.trades
    # the last bar has no next open: the close is the last close one tick against a seller, with the commission of the entry and of the exit
    exit_price = bars[-1].close - D("0.25")
    assert trade.exit_tag == "end_of_data" and trade.exit_price == exit_price and "closed_by_end_of_data" in result.warnings
    assert result.realized_pnl_usd == result.mtm_pnl_usd and result.realized_pnl_usd == (exit_price - (bars[10].open + D("0.25"))) * 20 - D("4.50")
    assert trade.pnl_usd == result.realized_pnl_usd and trade.costs_usd == D("4.50")

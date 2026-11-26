"""One strategy on several instruments (SPEC-020: B-35): one result per instrument and one aggregate that adds the profit and loss and measures its
drawdown on the summed curve. The bars are real 1 minute bars of ES, MES and CL of 2025-11-10, 14:58 to 15:11 UTC."""
from dataclasses import replace
from decimal import Decimal as D

import pytest
from backtest.runner_support import Fixed, data_of, spec
from backtest.session_support import multi_bars
from backtest.support import requires_engine
from dbsupport import db, env  # noqa: F401  (fixtures)

from hokma.backtest import runner
from hokma.backtest.data_feed import DataFeedError

pytestmark = requires_engine
ROOTS = ("ES", "MES", "CL")
INSTRUMENTS = tuple(f"CONT:{r}" for r in ROOTS)
TICKS = {"ES": D("0.25"), "MES": D("0.25"), "CL": D("0.01")}
MULTIPLIERS = {"ES": D(50), "MES": D(5), "CL": D(1000)}
COMMISSION = D("4.50")                                              # 2.25 on each side of one contract


def source(instrument, _spec):
    return data_of(multi_bars(instrument[5:]), instrument)


def expected_pnl(root: str) -> D:
    """Decided at the close of the bar 1 and filled at the open of the bar 2 plus a tick; closed at the open of the bar 10 less a tick."""
    bars = multi_bars(root)
    entry, exit_ = bars[2].open + TICKS[root], bars[10].open - TICKS[root]
    return (exit_ - entry) * MULTIPLIERS[root] - COMMISSION


def run_many(db, tmp_path, **over):
    bars = multi_bars("ES")
    base = spec(bars, instruments=INSTRUMENTS, capital_usd=D(900_000))
    return runner.run_many(replace(base, **over), lambda instrument: Fixed(contract=instrument, enter_at=2, exit_at=10), source=source, db=db, curve_dir=tmp_path)


def test_three_symbols_give_three_results_and_an_aggregate_that_adds_the_profit_and_loss_B_35(db, tmp_path):
    many = run_many(db, tmp_path)
    assert list(many.per_instrument) == list(INSTRUMENTS)
    for root, instrument in zip(ROOTS, INSTRUMENTS):
        result = many.per_instrument[instrument]
        assert result.status == "done" and result.realized_pnl_usd == expected_pnl(root)
        assert len(result.trades) == 1 and result.trades[0].contract_id == instrument
        assert result.equity.equity_usd[0] == D(300_000)                                # a third of the capital each
    total = sum((expected_pnl(r) for r in ROOTS), D(0))
    aggregate = many.aggregate
    assert aggregate.status == "done" and aggregate.realized_pnl_usd == total and aggregate.mtm_pnl_usd == total
    assert aggregate.equity.equity_usd[-1] == D(900_000) + total and len(aggregate.trades) == 3
    assert aggregate.costs_usd["commission"] == COMMISSION * 3


def test_the_aggregate_drawdown_is_measured_on_the_summed_curve_not_as_a_sum_of_drawdowns_B_35(db, tmp_path):
    many = run_many(db, tmp_path)
    curves = [r.equity for r in many.per_instrument.values()]
    assert all(c.timestamps == curves[0].timestamps for c in curves)                # the same 14 minutes: the sum is point by point
    summed = [sum(c.equity_usd[k] for c in curves) for k in range(len(curves[0].timestamps))]
    assert list(many.aggregate.equity.equity_usd) == summed and list(many.aggregate.equity.timestamps) == list(curves[0].timestamps)

    def worst_drop(values):
        peak, worst = values[0], D(0)
        for v in values:
            peak = max(peak, v)
            worst = max(worst, peak - v)
        return worst

    on_the_sum = worst_drop(summed)
    sum_of_each = sum(worst_drop(list(c.equity_usd)) for c in curves)
    assert on_the_sum > 0 and on_the_sum < sum_of_each                              # the three do not fall at the same time and by the same amount
    # hand check of the first minutes of ES: the long fills at 6828.75 + 0.25 = 6829.00 and the bar closes at 6829.00: only the commission 2.25 is lost
    assert many.per_instrument["CONT:ES"].equity.equity_usd[2] == D(300_000) - D("2.25")


def test_every_run_is_saved_with_the_aggregate_as_its_parent_and_counts_as_a_trial_B_35(db, tmp_path):
    many = run_many(db, tmp_path)
    with db.read() as u:
        rows = u.session.query("SELECT id, parent_id, status FROM backtest_runs ORDER BY id")
    assert rows[0]["id"] == many.aggregate.run_id and rows[0]["parent_id"] is None and rows[0]["status"] == "done"
    assert [r["parent_id"] for r in rows[1:]] == [many.aggregate.run_id] * 3
    from hokma.backtest import trials

    assert trials.count("t") == 3


def test_an_instrument_that_fails_leaves_no_aggregate_and_the_message_names_it_B_35(db, tmp_path):
    def broken(instrument, _spec):
        if instrument == "CONT:CL":
            raise DataFeedError("no_data", "no bars for CL")
        return source(instrument, _spec)

    bars = multi_bars("ES")
    with pytest.raises(runner.StageError) as error:
        runner.run_many(spec(bars, instruments=INSTRUMENTS), lambda i: Fixed(contract=i, enter_at=2, exit_at=10), source=broken, db=db, curve_dir=tmp_path)
    assert error.value.code == "instrument_failed" and "CONT:CL" in str(error.value)
    with db.read() as u:
        rows = u.session.query("SELECT status, error FROM backtest_runs ORDER BY id")
    assert rows[0]["status"] == "failed" and "CONT:CL" in rows[0]["error"] and [r["status"] for r in rows[1:]] == ["done", "done", "failed"]


def test_the_instruments_are_checked_by_size_and_repetition_B_35(db, tmp_path):
    from hokma.quant.errors import BadInput

    bars = multi_bars("ES")
    with pytest.raises(runner.SpecError):
        runner.run_many(spec(bars, instruments=("CONT:ES", "CONT:ES")), lambda i: Fixed(contract=i), source=source, db=db, curve_dir=tmp_path)
    many = tuple(f"CONT:X{n}" for n in range(101))
    with pytest.raises(BadInput):                                                   # 100 is the cap of n_instruments
        runner.run_many(spec(bars, instruments=many), lambda i: Fixed(contract=i), source=source, db=db, curve_dir=tmp_path)
    with db.read() as u:
        assert u.session.query("SELECT id FROM backtest_runs") == []                  # nothing was opened for a refused request

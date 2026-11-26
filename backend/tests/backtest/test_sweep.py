"""Fast sweep (SPEC-020, 4.5): B-32 and V-01. The fill arithmetic is checked by hand on six bars; the agreement with the engine is checked on a
long series built from the real NQ bars of the fixture (see search_support.chained_bars: the fixture has no long real series, so the series
is made of real moves, chained and seeded)."""
import time
from decimal import Decimal as D

import numpy as np
import pytest

from backtest import search_support as ss
from backtest import support
from backtest.support import requires_engine
from hokma.backtest import sweep
from hokma.backtest.spec import SpecError
from hokma.backtest.sweep import Arrays, SweepCosts
from hokma.quant.errors import BadInput

# ticks of 0.25 and a multiplier of 20: one tick is 5 USD; the commission is 2.25 per side, so one side costs 5 + 2.25 = 7.25
COSTS = SweepCosts(D("0.25"), D(20), 1, D("2.25"))
OPEN = [100.0, 101.0, 103.0, 102.0, 105.0, 104.0]
CLOSE = [100.5, 102.0, 102.5, 104.0, 104.5, 104.0]


def px(opens, closes):
    return Arrays(np.array(opens), np.maximum(opens, closes) + 1.0, np.minimum(opens, closes) - 1.0, np.array(closes))


# ---------------------------------------------------------------------------------------------------------------- the arithmetic, by hand
def test_the_fills_of_a_signal_are_priced_at_the_next_open_plus_a_tick_with_both_commissions():
    signals = np.array([[0, 1, 1, 0, 1, 1]])
    out = sweep.sweep_signals(px(OPEN, CLOSE), signals, COSTS, per_bar=True)
    # signal after bar 1 (on): buy at the open of bar 2, 103.00 + 0.25 = 103.25. Signal after bar 3 (off): sell at the open of bar 4, 105.00 - 0.25 = 104.75.
    # Closed trade: (104.75 - 103.25) * 20 = 30.00, less 2 commissions of 2.25 = 25.50. Signal after bar 4 (on): buy at the open of bar 5, 104.00 + 0.25 = 104.25.
    # The signal after the last bar has no next bar and is ignored. That lot is still open at the close 104.00: (104.00 - 104.25) * 20 = -5.00.
    assert out.realized[0] == pytest.approx(25.5) and out.trades[0] == 1
    assert out.mtm[0] == pytest.approx(20.5)                       # realized 25.50 plus the open lot -5.00, as the engine marks it (no commission on an open lot)
    # per bar (marked to market, with the 7.25 of the open lot already paid): 0, 0, -17.25, +30.00, +12.75, -7.25; they add up to 18.25
    assert out.pnl[:, 0] == pytest.approx([0, 0, -17.25, 30.0, 12.75, -7.25]) and out.pnl[:, 0].sum() == pytest.approx(18.25)


def test_a_flip_from_long_to_short_is_two_fills_each_with_its_costs():
    open_, close = [100.0, 101.0, 102.0, 103.0], [100.0, 101.0, 102.0, 103.0]
    out = sweep.sweep_signals(px(open_, close), np.array([[1, -1, 0, 0]]), COSTS)
    # buy at 101.25; at the open of bar 2 sell 2: 102.00 - 0.25 = 101.75 closes the long (101.75 - 101.25) * 20 = 10.00 and opens a short at 101.75;
    # at the open of bar 3 buy at 103.25 closes the short (101.75 - 103.25) * 20 = -30.00. Four fills of 2.25: 4 * 2.25 = 9.00 in total
    assert out.realized[0] == pytest.approx(10.0 - 30.0 - 9.0) and out.mtm[0] == pytest.approx(-29.0) and out.trades[0] == 2


def test_a_combination_that_never_trades_is_zero_and_a_short_position_is_priced_on_the_other_side():
    out = sweep.sweep_signals(px(OPEN, CLOSE), np.array([[0, 0, 0, 0, 0, 0], [0, -1, -1, -1, 0, 0]]), COSTS)
    assert out.realized[0] == 0.0 and out.mtm[0] == 0.0 and out.trades[0] == 0
    # short from the open of bar 2: sell at 103.00 - 0.25 = 102.75; the signal turns off after bar 4: cover at the open of bar 5, 104.00 + 0.25 = 104.25
    # (102.75 - 104.25) * 20 = -30.00, less 4.50 of commission = -34.50
    assert out.realized[1] == pytest.approx(-34.5) and out.mtm[1] == pytest.approx(-34.5)


def test_the_order_is_best_first_and_a_tie_keeps_the_order_of_the_grid():
    result = sweep.SweepResult(np.array([5.0, 9.0, 9.0, 1.0]), np.array([1.0, 2.0, 3.0, 4.0]), np.zeros(4, dtype=int))
    assert result.order("net_pnl").tolist() == [1, 2, 0, 3] and result.top(2, "net_pnl").tolist() == [1, 2]
    assert result.order().tolist() == [3, 2, 1, 0]                            # the default metric is the mark to market
    with pytest.raises(SpecError):
        result.scores("sharpe")
    with pytest.raises(BadInput):
        result.top(1_001)


def test_bad_signals_are_refused_with_a_stable_code():
    with pytest.raises(SpecError, match="bad_signal"):
        sweep.sweep_signals(px(OPEN, CLOSE), np.array([[0, 2, 0, 0, 0, 0]]), COSTS)
    with pytest.raises(SpecError, match="matrix of combinations by bars"):
        sweep.sweep_signals(px(OPEN, CLOSE), np.zeros((1, 5)), COSTS)
    with pytest.raises(SpecError, match="no combination"):
        sweep.sweep_signals(px(OPEN, CLOSE), np.zeros((0, 6)), COSTS)
    with pytest.raises(BadInput):
        sweep.sweep_signals(px(OPEN, CLOSE), np.zeros((20_001, 6)), COSTS)       # above the cap of combinations
    with pytest.raises(SpecError):
        sweep.arrays([])


def test_the_costs_come_from_the_same_fee_table_and_ticks_as_the_engine_run():
    costs = ss.costs_of(support.bars_of("T1"))
    assert (costs.commission_per_side, costs.slippage_ticks, costs.slippage_usd) == (D("2.25"), 1, 5.0)      # the fixture: 2.25 per side, one tick of 5 USD


def test_a_large_sweep_in_chunks_equals_the_same_signals_in_one_pass():
    bars = ss.chained_bars(300)
    grid = [{"fast": f, "slow": s} for f in range(2, 14) for s in range(20, 20 + 5 * 30, 5)]         # 12 * 30 = 360 combinations: more than one chunk
    assert len(grid) > sweep.CHUNK
    chunked = sweep.signal_sweep(bars, ss.builder, grid, ss.costs_of(bars), per_bar=True)
    arrays = sweep.arrays(bars)
    whole = sweep.sweep_signals(arrays, np.vstack([ss.builder(arrays, g) for g in grid]), ss.costs_of(bars), per_bar=True)
    assert np.array_equal(chunked.realized, whole.realized) and np.array_equal(chunked.mtm, whole.mtm) and np.array_equal(chunked.pnl, whole.pnl)
    assert chunked.pnl.shape == (300, 360)
    with pytest.raises(SpecError):
        sweep.signal_sweep(bars, ss.builder, [], ss.costs_of(bars))


def test_the_sweep_is_deterministic():
    bars = ss.chained_bars(300)
    grid = [{"fast": f, "slow": 60} for f in (5, 10, 20)]
    a, b = sweep.signal_sweep(bars, ss.builder, grid, ss.costs_of(bars)), sweep.signal_sweep(bars, ss.builder, grid, ss.costs_of(bars))
    assert a.realized.tobytes() == b.realized.tobytes() and a.mtm.tobytes() == b.mtm.tobytes()
    assert [b_.ts for b_ in ss.chained_bars(50, 3)] == [b_.ts for b_ in ss.chained_bars(50, 3)] and ss.chained_bars(50, 3) == ss.chained_bars(50, 3)
    assert ss.chained_bars(50, 3) != ss.chained_bars(50, 4)


# ---------------------------------------------------------------------------------------------------------------- rank agreement
def test_rank_correlation_by_hand():
    # ranks 1 2 3 4 against 1 3 2 4: the differences are 0, 1, 1, 0, so 1 - 6 * 2 / (4 * 15) = 0.8
    assert sweep.rank_correlation([1, 2, 3, 4], [1, 3, 2, 4]) == pytest.approx(0.8)
    # ties take the average rank: [1.5, 1.5, 3] against [1, 2, 3]: covariance 1.5, variances 1.5 and 2: 1.5 / sqrt(3)
    assert sweep.rank_correlation([1, 1, 2], [1, 2, 3]) == pytest.approx(1.5 / 3 ** 0.5)
    assert sweep.rank_correlation([1, 2, 3], [30, 20, 10]) == pytest.approx(-1.0)
    with pytest.raises(SpecError):
        sweep.rank_correlation([1, 1, 1], [1, 2, 3])
    with pytest.raises(SpecError):
        sweep.rank_correlation([1], [1])
    assert sweep.top_overlap([5, 4, 3, 2, 1], [5, 3, 4, 1, 2], 3) == 3 and sweep.top_overlap([5, 4, 3, 2, 1], [1, 2, 3, 4, 5], 2) == 0


@requires_engine
def test_the_cross_20_and_100_gives_the_operations_of_the_engine_and_a_ranking_of_20_combinations_with_rank_correlation_of_at_least_099_B_32():
    bars = ss.chained_bars(1500)
    costs = ss.costs_of(bars)
    # the cross 20 and 100 itself: the trades differ by at most one (the engine closes a position that is still open when the contract expires)
    one = sweep.signal_sweep(bars, ss.builder, [{"fast": 20, "slow": 100}], costs)
    engine_one = ss.engine_run(bars, 20, 100)
    assert abs(int(one.trades[0]) - len(engine_one.trades)) <= 1 and one.trades[0] >= 1
    assert one.mtm[0] == pytest.approx(float(engine_one.mtm_pnl_usd), abs=30)       # equal but for the exit tick of the final close, a few USD
    grid = [{"fast": f, "slow": s} for f in (5, 10, 20, 30) for s in (40, 60, 80, 100, 120)]
    swept = sweep.signal_sweep(bars, ss.builder, grid, costs)
    official = [float(ss.engine_run(bars, g["fast"], g["slow"]).mtm_pnl_usd) for g in grid]
    assert len(grid) == 20 and sweep.rank_correlation(swept.mtm, official) >= 0.99


@requires_engine
def test_200_combinations_of_the_cross_run_in_under_a_fifth_of_the_engine_time_and_the_top_10_agree_in_at_least_9_V_01():
    bars = ss.chained_bars(1000, seed=2)
    costs = ss.costs_of(bars)
    grid = [{"fast": f, "slow": s} for f in range(5, 55, 5) for s in range(60, 260, 10)]
    assert len(grid) == 200
    started = time.perf_counter()
    swept = sweep.signal_sweep(bars, ss.builder, grid, costs)
    fast_time = time.perf_counter() - started
    started = time.perf_counter()
    official = [float(ss.engine_run(bars, g["fast"], g["slow"]).mtm_pnl_usd) for g in grid]
    engine_time = time.perf_counter() - started
    assert fast_time < engine_time / 5
    assert sweep.top_overlap(swept.mtm, official, 10) >= 9

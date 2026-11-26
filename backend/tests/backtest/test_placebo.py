"""Controls (SPEC-020, 5.4.4): B-31 random series, V-04 buy and hold and random signals, V-27 (the same implementation). The expected values of buy
and hold are worked out by hand from the real bars of the fixture."""
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import numpy as np
import pytest

from backtest import search_support as ss
from backtest import support
from backtest.support import requires_engine
from hokma.backtest import placebo, sweep
from hokma.backtest.placebo import PlaceboError
from hokma.backtest.results import Trade
from hokma.backtest.ticks import Bar

TICK = D("0.25")
UTC = timezone.utc


def fixture_bars():
    return [b for name in support.SCENARIOS for b in support.bars_of(name)]


def log_returns(bars):
    close = np.array([float(b.close) for b in bars])
    return np.diff(np.log(close))


# ---------------------------------------------------------------------------------------------------------------- B-31
@pytest.mark.parametrize("bars", [fixture_bars, lambda: ss.chained_bars(500)], ids=["real_fixture_bars", "chained_real_moves"])
def test_a_random_series_repeats_with_its_seed_keeps_the_returns_and_the_candle_geometry_B_31(bars):
    original = bars()
    first = placebo.random_ohlc(original, seed=11, tick_size=TICK)
    assert first == placebo.random_ohlc(original, seed=11, tick_size=TICK)                  # the same seed gives the same series
    assert first != placebo.random_ohlc(original, seed=12, tick_size=TICK)
    assert len(first) == len(original) and first[0] == original[0] and [b.ts for b in first] == [b.ts for b in original]
    for b in first:                                                                         # the geometry of a candle
        assert b.high >= max(b.open, b.close) and b.low <= min(b.open, b.close) and b.open % TICK == 0 and b.close % TICK == 0
    # the records are permuted, so the close to close returns are the same numbers in another order: the mean and the deviation are those of the
    # original up to the rounding of the prices to the tick (one tick is 1e-5 of the price; the tolerances below are 10 and 100 times that)
    a, b = log_returns(original), log_returns(first)
    assert abs(a.mean() - b.mean()) < 1e-5 and abs(a.std(ddof=1) / b.std(ddof=1) - 1) < 0.01
    assert np.allclose(np.sort(a), np.sort(b), atol=2e-5)
    assert not np.allclose(a, b)                                                           # but the order is not the original one


def test_a_random_series_of_another_length_is_drawn_with_replacement_and_keeps_its_shape_B_31():
    original = ss.chained_bars(200)
    longer = placebo.random_ohlc(original, seed=3, tick_size=TICK, n=500)
    assert len(longer) == 500 and longer[0] == original[0]
    assert [b.ts for b in longer[:3]] == [original[0].ts + timedelta(minutes=k) for k in range(3)]
    assert all(b.high >= max(b.open, b.close) and b.low <= min(b.open, b.close) for b in longer)
    assert longer == placebo.random_ohlc(original, seed=3, tick_size=TICK, n=500)
    # the returns that were drawn are returns of the original: every one is within a tick of an original close to close return
    pool = log_returns(original)
    assert all(np.min(np.abs(pool - r)) < 5e-5 for r in log_returns(longer))
    with pytest.raises(PlaceboError):
        placebo.random_ohlc(original[:2], seed=1, tick_size=TICK)
    with pytest.raises(PlaceboError):
        placebo.random_ohlc(original, seed=1, tick_size=D(0))
    with pytest.raises(PlaceboError):
        placebo.random_ohlc(original, seed=1, tick_size=TICK, n=1)


# ---------------------------------------------------------------------------------------------------------------- V-04: buy and hold
def test_buy_and_hold_is_the_price_return_less_the_costs_of_one_entry_and_one_exit_V_04():
    bars = support.bars_of("T1")                       # first open 25642.50, last close 25649.00 (real NQ bars)
    held = placebo.buy_and_hold(bars, tick_size=support.TICK, multiplier=support.MULT, model=support.spec().costs)
    # entry 25642.50 + 1 tick = 25642.75; exit 25649.00 - 1 tick = 25648.75; (25648.75 - 25642.75) * 20 = 120.00; commissions 2 * 2.25 = 4.50
    assert (held.round_trip.entry_price, held.round_trip.exit_price) == (D("25642.75"), D("25648.75"))
    assert (held.round_trip.gross_usd, held.round_trip.commission_usd, held.round_trip.net_usd) == (D(120), D("4.50"), D("115.50"))
    assert float(held.price_return) == pytest.approx(6.5 / 25642.5, abs=1e-12) and held.bars == 8      # 25649.00 / 25642.50 - 1
    down = placebo.buy_and_hold(support.bars_of("T4"), tick_size=support.TICK, multiplier=support.MULT, model=support.spec().costs)
    # T4: open 25546.50, close 25541.00: entry 25546.75, exit 25540.75: (25540.75 - 25546.75) * 20 = -120.00, less 4.50 = -124.50
    assert down.round_trip.net_usd == D("-124.50") and down.price_return < 0
    with pytest.raises(PlaceboError):
        placebo.buy_and_hold([], tick_size=support.TICK, multiplier=support.MULT, model=support.spec().costs)


def test_buy_and_hold_uses_the_one_round_trip_of_the_cost_module():
    from hokma.backtest import costs
    bars = support.bars_of("T3")
    held = placebo.buy_and_hold(bars, tick_size=support.TICK, multiplier=support.MULT, model=support.spec().costs, qty=D(2))
    assert held.round_trip == costs.round_trip("long", entry_ref=bars[0].open, exit_ref=bars[-1].close, qty=D(2), tick_size=support.TICK,
                                               multiplier=support.MULT, model=support.spec().costs)
    assert held.round_trip.commission_usd == D("9.00")             # 2 contracts: 2 * 2.25 on each of the two sides


# ---------------------------------------------------------------------------------------------------------------- V-04 and V-27: random signals
def runs_of(row):
    """(start, length) of each run of equal non zero values in a signal vector."""
    out, i = [], 0
    while i < len(row):
        if row[i] != 0:
            j = i
            while j < len(row) and row[j] == row[i]:
                j += 1
            out.append((i, j - i))
            i = j
        else:
            i += 1
    return out


def test_random_signals_repeat_with_the_seed_and_have_the_trades_and_hold_asked_V_04():
    one = placebo.random_signals(300, n_trades=7, mean_hold=12, seed=5, count=20)
    assert one.shape == (20, 300) and one.dtype == np.int8 and (one == placebo.random_signals(300, n_trades=7, mean_hold=12, seed=5, count=20)).all()
    assert not (one == placebo.random_signals(300, n_trades=7, mean_hold=12, seed=6, count=20)).all()
    for row in one:
        runs = runs_of(row)
        assert len(runs) == 7 and all(length == 12 for _, length in runs)                 # the number of trades and the holding time are exact
        assert all(b[0] - (a[0] + a[1]) >= 1 for a, b in zip(runs, runs[1:]))             # at least one flat bar between two trades
        assert row[-1] == 0 and row[-2] == 0 and set(np.unique(row)) <= {0, 1}
    assert len({tuple(runs_of(r)) for r in one}) > 1                                      # the timing is random: the rows differ
    short = placebo.random_signals(100, n_trades=3, mean_hold=5, seed=1, side="short")
    assert set(np.unique(short)) == {-1, 0}
    both = placebo.random_signals(400, n_trades=40, mean_hold=3, seed=1, side="both")
    assert set(np.unique(both)) == {-1, 0, 1}


def test_the_random_signals_trade_as_often_as_asked_through_the_sweep_V_04():
    px = sweep.arrays(ss.chained_bars(600))
    signals = placebo.random_signals(600, n_trades=9, mean_hold=20, seed=2, count=50)
    out = sweep.sweep_signals(px, signals, ss.costs_of(ss.chained_bars(600)))
    assert (out.trades == 9).all()                                                           # every random run closes exactly 9 trades


def test_too_many_trades_for_the_bars_and_bad_arguments_are_refused():
    with pytest.raises(PlaceboError, match="too_many_trades"):
        placebo.random_signals(100, n_trades=10, mean_hold=10, seed=1)                      # 10 * 10 + 9 = 109 bars needed
    placebo.random_signals(111, n_trades=10, mean_hold=10, seed=1)                          # 109 + the two last bars: it just fits
    with pytest.raises(PlaceboError):
        placebo.random_signals(100, n_trades=0, mean_hold=5, seed=1)
    with pytest.raises(PlaceboError):
        placebo.random_signals(100, n_trades=1, mean_hold=5, seed=1, side="flat")
    from hokma.quant.errors import BadInput
    with pytest.raises(BadInput):
        placebo.random_signals(100, n_trades=1, mean_hold=5, seed=1, count=20_001)


def test_the_profile_of_a_run_is_its_trades_hold_and_side_V_04():
    t0 = datetime(2026, 1, 5, 14, 0, tzinfo=UTC)
    bars = ss.chained_bars(100)
    at = lambda k: t0 + timedelta(minutes=k)                                              # noqa: E731
    mk = lambda side, a, b: Trade("NQ", side, D(1), at(a), D(1), at(b), D(1), D(0), D(0))     # noqa: E731
    # entries at the open of bars 5, 30, 60 and exits at the open of bars 15, 40, 75: holds 10, 10, 15 bars: the mean is 35 / 3 = 11.67, so 12
    trades = [mk("long", 5, 15), mk("long", 30, 40), mk("long", 60, 75), Trade("NQ", "long", D(1), at(80), D(1), None, None, None, D(0))]
    assert placebo.profile(trades, bars) == placebo.Profile(3, 12, "long")                # the open trade is not counted
    assert placebo.profile([mk("short", 5, 15), mk("long", 30, 40)], bars) == placebo.Profile(2, 10, "both")
    with pytest.raises(PlaceboError, match="no closed trade"):
        placebo.profile([], bars)
    with pytest.raises(PlaceboError, match="not the ones of the run"):
        placebo.profile([mk("long", 5, 150)], bars)


@requires_engine
def test_the_random_signals_copy_the_trades_of_the_strategy_under_test_V_04():
    bars = ss.chained_bars(1000)
    out = ss.engine_run(bars, 10, 60)
    copy = placebo.profile(out.trades, bars)
    assert copy.n_trades == len(out.trades) and copy.mean_hold >= 1
    signals = placebo.random_signals(len(bars), n_trades=copy.n_trades, mean_hold=copy.mean_hold, seed=9, count=10, side=copy.side)
    assert (sweep.sweep_signals(bars, signals, ss.costs_of(bars)).trades == copy.n_trades).all()      # the same frequency of operations


def test_there_is_one_implementation_of_the_random_signals_V_27():
    folder = Path(placebo.__file__).parent
    assert sum(p.read_text(encoding="utf-8").count("def random_signals") for p in folder.glob("*.py")) == 1

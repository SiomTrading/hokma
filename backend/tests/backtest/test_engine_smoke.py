"""Reference cases of the engine run (SPEC-020: N-29): a moving average cross on a whole real session of NQ and an options run. Each ends in a valid result
and gives the same bytes when it is run again.

The options case has no real series behind it: the repository holds no recording of option bars (the chain files of tests/options_fixtures are
made up by a script), so its six bars are written by hand, across the 3 USD break of the tick scheme, and every expected value is worked out in the
comments. Replace them with a recording when one exists."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import numpy as np
from backtest.runner_support import NQ, source_of, spec
from backtest.session_support import session_bars
from backtest.support import requires_engine
from dbsupport import db, env  # noqa: F401  (fixtures)

from hokma.backtest import results, runner
from hokma.backtest.runner import InstrumentData
from hokma.backtest.ticks import Bar
from hokma.contracts.identity import ContractSheet
from hokma.factory.orders import buy, reduce_intent
from hokma.factory.params import ParamSchema

pytestmark = requires_engine
UTC = timezone.utc


def sma(values, length):
    """A causal simple moving average: the value at a bar uses only that bar and the ones before it (NaN until there are `length` of them)."""
    out = np.full(len(values), np.nan)
    if len(values) >= length:
        total = np.cumsum(values, dtype=float)
        out[length - 1:] = (total[length - 1:] - np.concatenate(([0.0], total[:-length]))) / length
    return out


class Cross:
    """Long while the fast average is above the slow one: buys on the cross up, closes on the cross down. No protective orders."""

    params = ParamSchema(())
    protective_orders = False

    def __init__(self, contract, fast=5, slow=20, qty=1):
        self.contract, self.fast, self.slow, self.qty = contract, fast, slow, qty

    def init(self, ctx):
        ctx.indicator("fast", sma, warmup=self.fast, length=self.fast)
        ctx.indicator("slow", sma, warmup=self.slow, length=self.slow)

    def on_bar(self, ctx):
        fast, slow = ctx.series("fast").tail(2), ctx.series("slow").tail(2)
        if len(fast) < 2 or np.isnan(slow).any():
            return []
        held = ctx.position is not None and ctx.position.qty != 0
        if not held and fast[0] <= slow[0] and fast[1] > slow[1]:
            return [buy(self.contract, self.qty, tag="cross_up")]
        if held and fast[0] >= slow[0] and fast[1] < slow[1]:
            return [reduce_intent(self.contract, ctx.position.side, abs(ctx.position.qty), tag="cross_down")]
        return []


def same_bytes(first, second) -> bool:
    return results.to_json(replace(first, run_id=0)) == results.to_json(replace(second, run_id=0))


def test_a_moving_average_cross_over_a_real_session_ends_in_a_valid_result_that_repeats_N_29(db, tmp_path):
    bars = session_bars("day")
    assert len(bars) == 390
    outcomes = [runner.run(spec(bars, start=bars[0].ts, end=bars[-1].ts), strategy=Cross(NQ), source=source_of({NQ: bars}), db=db, curve_dir=tmp_path / str(k))
                for k in (1, 2)]
    first, second = outcomes
    assert first.status == "done" and len(first.equity.timestamps) == 390 and len(first.trades) >= 2
    closed = [t for t in first.trades if t.exit_at is not None]
    assert closed and all(t.entry_tag == "cross_up" for t in first.trades) and all(t.exit_tag == "exit" for t in closed)
    assert sum(t.pnl_usd for t in closed) == first.realized_pnl_usd                      # the trades add up to the realized profit
    assert first.equity.equity_usd[-1] == D(1_000_000) + first.mtm_pnl_usd
    assert all(t.mae_usd <= 0 <= t.mfe_usd for t in first.trades)
    assert same_bytes(first, second)                                                       # deterministic
    # the first signal by hand: the fast average (5 bars) crosses above the slow (20) at the first bar where it is above after being at or under
    close = np.array([float(b.close) for b in bars])
    fast, slow = sma(close, 5), sma(close, 20)
    cross = next(i for i in range(20, 390) if fast[i - 1] <= slow[i - 1] and fast[i] > slow[i])
    assert first.trades[0].entry_at == bars[cross + 1].ts                                   # decided at the close of bar `cross`, filled at the open of the next
    assert first.trades[0].entry_price == bars[cross + 1].open + D("0.25")


def option_bars() -> list[Bar]:
    start = datetime(2026, 11, 20, 15, 0, tzinfo=UTC)
    rows = [("2.90", "2.95", "2.88", "2.94"), ("3.00", "3.04", "2.99", "3.03"), ("3.03", "3.08", "3.02", "3.07"), ("3.07", "3.12", "3.06", "3.11"),
            ("3.12", "3.16", "3.10", "3.14"), ("3.14", "3.15", "3.09", "3.10")]
    return [Bar(start + timedelta(minutes=k), *(D(x) for x in row), D(100)) for k, row in enumerate(rows)]


OPTION = "OPT:AAPL:2026-12-18:C:200"


def option_sheet() -> ContractSheet:
    return ContractSheet(OPTION, "option", "AAPL", "AAPL", D(100), D("0.01"), D("1.00"), date(2026, 12, 18), D(200), "C", "USD", "test",
                         datetime(2026, 11, 20, tzinfo=UTC))


def option_source(instrument, _spec):
    return InstrumentData(instrument, tuple(option_bars()), "option-bars-by-hand", option_sheet())


def test_an_options_run_fills_on_the_grid_of_each_price_level_and_repeats_N_29(db, tmp_path):
    class Calls(Cross):
        def init(self, ctx):
            pass

        def on_bar(self, ctx):
            held = ctx.position is not None and ctx.position.qty != 0
            if ctx.index == 1 and not held:
                return [buy(self.contract, 2, tag="long_call")]
            if ctx.index == 4 and held:
                return [reduce_intent(self.contract, ctx.position.side, abs(ctx.position.qty), tag="sell_call")]
            return []

    bars = option_bars()
    outcomes = [runner.run(spec(bars, instruments=(OPTION,), capital_usd=D(100_000)), strategy=Calls(OPTION), source=option_source, db=db,
                           curve_dir=tmp_path / str(k)) for k in (1, 2)]
    first, second = outcomes
    assert first.status == "done", first.warnings
    # bought after the close of bar 0, filled at the open of bar 1, 3.00, plus a tick of 0.01 = 3.01: from 3 USD the grid is 0.05, a buy rounds up to 3.05.
    # sold after the close of bar 3, filled at the open of bar 4, 3.12, less a tick = 3.11: a sell rounds down to 3.10.
    (trade,) = first.trades
    assert (trade.entry_price, trade.exit_price, trade.qty) == (D("3.05"), D("3.10"), D(2))
    # (3.10 - 3.05) * 100 * 2 = 10.00; commission 0.65 per contract and side: 4 contract sides = 2.60
    assert trade.pnl_usd == D("7.40") and trade.costs_usd == D("2.60") and first.realized_pnl_usd == D("7.40")
    assert same_bytes(first, second) and first.equity.equity_usd[-1] == D(100_000) + D("7.40")

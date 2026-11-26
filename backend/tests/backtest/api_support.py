"""A parameterized strategy and a registry for the tests of the public entry points: a moving average cross written as a strategy of the factory
(it decides in `on_bar`, declares its indicators in `init`), registered like any strategy, and the services that point the entry points at it."""
from __future__ import annotations

import pandas as pd
from backtest import search_support as ss
from backtest.runner_support import NQ, source_of, spec

from hokma.backtest.runner import Services
from hokma.backtest.spec import BacktestSpec
from hokma.factory.orders import Ticks, buy, reduce_intent
from hokma.factory.params import Param, ParamSchema
from hokma.factory.registry import Entry, Registry
from hokma.factory.strategy import BadStrategy


def sma(values, window):
    return pd.Series(values).rolling(window).mean().to_numpy()


class Cross:
    """Long one contract while the fast average of the closes is above the slow one; flat otherwise. A `fast` of 7 raises, to have a point that fails."""

    SCHEMA = ParamSchema((Param("fast", "int", 5, 1, 500), Param("slow", "int", 20, 2, 1000)))
    protective_orders = False
    stop = None

    def __init__(self, fast: int = 5, slow: int = 20) -> None:
        self.fast, self.slow = fast, slow

    def init(self, ctx) -> None:
        ctx.indicator("fast_ma", sma, warmup=self.fast, window=self.fast)
        ctx.indicator("slow_ma", sma, warmup=self.slow, window=self.slow)

    def on_bar(self, ctx):
        if self.fast == 7:
            raise BadStrategy("fast 7 is not allowed in this test strategy")
        fast, slow = ctx.series("fast_ma")[-1], ctx.series("slow_ma")[-1]
        held = ctx.position is not None and ctx.position.qty != 0
        if fast > slow and not held:
            return [buy(NQ, 1, sl=self.stop, tag="up")]
        if fast <= slow and held:
            return [reduce_intent(NQ, ctx.position.side, abs(ctx.position.qty), tag="down")]
        return []


class CrossWithStop(Cross):
    """The same cross with a stop on every entry: it has protective orders, so it never goes through the fast sweep."""

    protective_orders = True
    stop = Ticks(400)


def registry() -> Registry:
    """A registry with the cross as a strategy under `cross` and `cross_stop`."""
    found = Registry()
    found.add(Entry("cross", "strategy", Cross, "DT", "", {"fast": 5, "slow": 20}, "test"))
    found.add(Entry("cross_stop", "strategy", CrossWithStop, "DT", "", {"fast": 5, "slow": 20}, "test"))
    return found


def services(bars, db, folder, **over) -> Services:
    return Services(db=db, registry=registry(), source=source_of({NQ: bars}), curve_dir=folder, **over)


def spec_of(bars, **over) -> BacktestSpec:
    base = dict(strategy_id="cross", params={"fast": 5, "slow": 20})
    base.update(over)
    return spec(bars, **base)


def series(n: int = 600, seed: int = 1):
    """`n` one minute bars built from the real moves of the NQ fixture (see `search_support.chained_bars`)."""
    return ss.chained_bars(n, seed)

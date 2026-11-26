"""Strategies and sources for the tests of the runner: the real NQ bars of the fixtures and strategies written for the test."""
from __future__ import annotations

from datetime import date
from decimal import Decimal as D

from backtest import support
from backtest.session_support import MULT, TICK, session_bars

from hokma.backtest import runner
from hokma.backtest.runner import InstrumentData
from hokma.backtest.spec import BacktestSpec
from hokma.contracts.identity import ContractSheet
from hokma.factory.orders import Pct, Price, Ticks, buy, reduce_intent, sell
from hokma.factory.params import ParamSchema

NQ = "CONT:NQ"
ON = date(2025, 11, 10)


def sheet(contract: str = NQ) -> ContractSheet:
    return runner.sheet_of(contract, ON) if contract.startswith("CONT:") else None


def data_of(bars, contract: str = NQ) -> InstrumentData:
    from hokma.backtest.data_feed import data_hash
    import pandas as pd

    frame = pd.DataFrame({"open": [float(b.open) for b in bars], "high": [float(b.high) for b in bars], "low": [float(b.low) for b in bars],
                          "close": [float(b.close) for b in bars], "volume": [float(b.volume) for b in bars]},
                         index=pd.DatetimeIndex([b.ts for b in bars], name="time"))
    return InstrumentData(contract, tuple(bars), data_hash(frame), sheet(contract))


def source_of(bars_by_contract: dict):
    """A source that serves real bars that the test already holds."""
    return lambda instrument, spec: data_of(bars_by_contract[instrument], instrument)


class Fixed:
    """Buys one contract when the number of closed bars is `enter_at` and closes when it is `exit_at`; nothing else. Protective orders by choice."""

    params = ParamSchema(())

    def __init__(self, contract=NQ, enter_at=2, exit_at=6, side="buy", qty=1, sl=None, tp=None, tag="fixed", protective=False):
        self.contract, self.enter_at, self.exit_at, self.side, self.qty, self.sl, self.tp, self.tag = contract, enter_at, exit_at, side, qty, sl, tp, tag
        self.protective_orders = protective

    def init(self, ctx):
        pass

    def on_bar(self, ctx):
        held = ctx.position is not None and ctx.position.qty != 0
        if ctx.index == self.enter_at and not held:
            make = buy if self.side == "buy" else sell
            return [make(self.contract, self.qty, sl=self.sl, tp=self.tp, tag=self.tag)]
        if self.exit_at is not None and ctx.index == self.exit_at and held:
            return [reduce_intent(self.contract, ctx.position.side, abs(ctx.position.qty), tag="leave")]
        return []


def spec(bars, **over) -> BacktestSpec:
    start, end = bars[0].ts, bars[-1].ts
    base = dict(start=start, end=end, as_of=end, instruments=(NQ,))
    base.update(over)
    return support.spec(**base)


__all__ = ["D", "Fixed", "MULT", "NQ", "Pct", "Price", "TICK", "Ticks", "data_of", "session_bars", "sheet", "source_of", "spec"]

"""The strategy contract (SPEC-020: B-01, B-02, B-17)."""
from dataclasses import fields
from decimal import Decimal

import numpy as np
import pytest

from hokma.factory.orders import OrderIntent, buy
from hokma.factory.params import Param, ParamError, ParamSchema
from hokma.factory.strategy import (BadStrategy, BarContext, InitContext, LookAhead, Series, Strategy, build_context, check_strategy,
                                    replay_intents, warmup)
from hokma.quant.errors import BadInput
from tests.factory.support import minutes

CID = "FUT:NQ:2025-03"


def sma(values, length):
    """A causal moving average written for the test: NaN until `length` bars exist."""
    out = np.full(len(values), np.nan)
    for i in range(length - 1, len(values)):
        out[i] = values[i - length + 1:i + 1].mean()
    return out


def centered(values):
    """Not causal: every value looks one bar ahead."""
    return np.append(values[1:], values[-1])


class Probe:
    """A strategy that records what it sees and buys on every bar."""

    params = ParamSchema((Param("length", "int", 100, 2, 500),))

    def __init__(self) -> None:
        self.seen: list[tuple[int, int, int]] = []

    def init(self, ctx: InitContext) -> None:
        ctx.indicator("sma", sma, warmup=ctx.params["length"], length=ctx.params["length"])

    def on_bar(self, ctx: BarContext):
        self.seen.append((ctx.index, len(ctx.data.close), len(ctx.series("sma"))))
        return [buy(ctx.contract_id, 1, tag="probe")]


def bars(n: int) -> dict[str, np.ndarray]:
    close = 100.0 + np.arange(n)
    return {"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": np.ones(n)}


def test_series_reveals_only_closed_bars():                                           # B-02
    series = Series(np.arange(100.0), 50)
    assert len(series) == 50 and series[49] == 49.0 and series[-1] == 49.0 and series.last == 49.0
    with pytest.raises(LookAhead) as caught:
        series[51]
    assert caught.value.code == "look_ahead"
    with pytest.raises(LookAhead):
        series[50]
    with pytest.raises(LookAhead):
        series[0:51]
    assert list(series[48:50]) == [48.0, 49.0] and list(series.tail(2)) == [48.0, 49.0]
    assert len(np.asarray(series)) == 50
    with pytest.raises(ValueError):
        series._view[0] = 1.0                                                           # the revealed values cannot be rewritten


def test_on_bar_sees_50_bars_of_data_and_of_a_precomputed_indicator_at_bar_50():
    probe = Probe()
    replay_intents(probe, bars(100), times=minutes(100), contract_id=CID, params={"length": 10})
    seen = {index: (n_data, n_ind) for index, n_data, n_ind in probe.seen}
    assert seen[50] == (50, 50)                                                         # the indicator has its first 50 values only
    assert all(index == n_data == n_ind for index, n_data, n_ind in probe.seen)         # never a value of a later bar


def test_no_order_before_the_warmup_and_the_result_declares_it():                       # B-17
    probe = Probe()
    result = replay_intents(probe, bars(150), times=minutes(150), contract_id=CID, params={"length": 100})
    assert result.warmup_bars == 100
    assert probe.seen[0][0] == 100                                                      # the first call has 100 closed bars
    assert min(index for index, _ in result.intents) == 100


def test_warmup_is_the_longest_indicator():
    assert warmup([20, 100, 50]) == 100 and warmup([]) == 0
    with pytest.raises(BadInput):
        warmup([-1])
    with pytest.raises(BadInput):
        warmup([2_000_000])
    ctx = InitContext({})
    ctx.indicator("fast", sma, warmup=10, length=10)
    ctx.indicator("slow", sma, warmup=30, length=30)
    ctx.set_warmup(50)
    assert ctx.warmup_bars == 50
    with pytest.raises(BadStrategy, match="declared twice"):
        ctx.indicator("fast", sma, warmup=10, length=10)
    with pytest.raises(BadStrategy):
        ctx.indicator("close", sma, warmup=10)                                           # a bar field name
    with pytest.raises(BadStrategy, match="source"):
        ctx.indicator("x", sma, source="nothing", warmup=1)


def test_a_strategy_without_on_bar_fails_to_register():                                  # B-01
    class NoBar:
        params = ParamSchema(())

        def init(self, ctx):
            pass

    with pytest.raises(BadStrategy, match="NoBar does not implement on_bar"):
        check_strategy(NoBar())
    with pytest.raises(BadStrategy, match="declares no ParamSchema"):
        class NoParams:
            def init(self, ctx): ...
            def on_bar(self, ctx): ...
        check_strategy(NoParams())
    assert isinstance(Probe(), Strategy) and check_strategy(Probe) is Probe.params


def test_a_strategy_emits_only_order_intents():
    class Rogue(Probe):
        def on_bar(self, ctx):
            return [{"side": "buy"}]

    with pytest.raises(BadStrategy, match="emits only OrderIntent"):
        replay_intents(Rogue(), bars(120), times=minutes(120), contract_id=CID)

    class Single(Probe):
        def on_bar(self, ctx):
            return buy(ctx.contract_id, 1)

    with pytest.raises(BadStrategy, match="list of OrderIntent"):
        replay_intents(Single(), bars(120), times=minutes(120), contract_id=CID)
    result = replay_intents(Probe(), bars(120), times=minutes(120), contract_id=CID)
    assert all(isinstance(i, OrderIntent) for _, batch in result.intents for i in batch)


def test_the_context_holds_no_broker_or_engine():
    names = {f.name for f in fields(BarContext)}
    assert names == {"index", "as_of", "contract_id", "data", "indicators", "params", "position", "equity"}
    ctx = build_context(cursor=3, times=minutes(5), contract_id=CID, bars=bars(5), indicators={}, params={}, position=None, equity=Decimal(1))
    assert ctx.as_of == minutes(5)[2] and len(ctx.data.close) == 3
    with pytest.raises(AttributeError, match="no field"):
        ctx.data.broker
    with pytest.raises(ValueError):
        build_context(cursor=6, times=minutes(5), contract_id=CID, bars=bars(5), indicators={}, params={}, position=None, equity=Decimal(1))


def test_a_non_causal_indicator_is_caught():
    class Peeker(Probe):
        def init(self, ctx):
            ctx.indicator("peek", centered, warmup=2)

        def on_bar(self, ctx):
            return []

    with pytest.raises(LookAhead, match="uses future data"):
        replay_intents(Peeker(), bars(60), times=minutes(60), contract_id=CID, params={"length": 5})


def test_parameters_are_validated_before_init():
    with pytest.raises(ParamError, match="between 2 and 500"):
        replay_intents(Probe(), bars(60), times=minutes(60), contract_id=CID, params={"length": 1})

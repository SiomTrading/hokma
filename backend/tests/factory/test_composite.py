"""Composite strategy (SPEC-020: L-51): alpha, sizer, exit rule and execution are separate parts."""
from decimal import Decimal as D

import numpy as np
import pytest

from hokma.desks.core.signal import Signal
from hokma.factory.alphas import EmaCross
from hokma.factory.orders import ATR, Size, SizeType, intent_quantity
from hokma.factory.strategy import BadStrategy, BarContext, Composite, ExecutionStyle, Position, build_context, replay_intents
from tests.factory.support import minutes, nq

CID = "FUT:NQ:2025-03"


def ema(values, length):
    """An exponential average written for the test (alpha = 2 / (length + 1), seeded with the first value)."""
    out = np.empty(len(values))
    k = 2.0 / (length + 1)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = k * values[i] + (1 - k) * out[i - 1]
    return out


LIBRARY = {"ema": ema}


class FixedRisk:
    """Sizer fake: a fixed amount of risk in USD per trade."""

    def __init__(self, usd: int) -> None:
        self.usd = usd

    def size(self, *, signal: Signal, ctx: BarContext) -> Size:
        return Size(SizeType.RISK_USD, self.usd)


class FixedContracts:
    def __init__(self, n: int) -> None:
        self.n = n

    def size(self, *, signal: Signal, ctx: BarContext) -> Size:
        return Size(SizeType.CONTRACTS, self.n)


class AtrExit:
    """Exit rule fake: stop at 2 ATR, target at 3 ATR."""

    def levels(self, *, side, ctx):
        return ATR(2), ATR(3)


class NoExit:
    def levels(self, *, side, ctx):
        return None, None


def closes() -> np.ndarray:
    down = 120.0 - np.arange(30)                 # a falling market, then a rising one: the fast average crosses above the slow one
    up = down[-1] + 2.0 * np.arange(1, 31)
    return np.concatenate([down, up])


def composite(sizer, exit_rule=None, **kw) -> Composite:
    return Composite(EmaCross(fast=3, slow=8), sizer, exit_rule or AtrExit(), contract_id=CID, desk_id=1, line="DT", horizon="intraday",
                     library=LIBRARY, tag="cross", **kw)


def run(strategy: Composite):
    data = closes()
    bars = {"open": data, "high": data + 1, "low": data - 1, "close": data, "volume": np.ones(len(data))}
    return replay_intents(strategy, bars, times=minutes(len(data)), contract_id=CID)


def test_changing_only_the_size_changes_quantities_and_not_the_signals():
    small, large = run(composite(FixedRisk(500))), run(composite(FixedRisk(1000)))
    assert small.warmup_bars == 8 and small.intents                                     # the slow average sets the warm-up
    key = lambda result: [(bar, i.side, i.sl, i.tp, i.tag) for bar, batch in result.intents for i in batch]
    assert key(small) == key(large)                                                       # same bars, same sides, same exits
    sheet = nq()
    qty = lambda result: [intent_quantity(i, sheet=sheet, price=D("20000"), equity=D("1000000"), atr=D("5"))[0]
                          for _, batch in result.intents for i in batch]
    assert set(qty(small)) == {D(2)} and set(qty(large)) == {D(5)}                         # 500 / (2 * 5 * 20) = 2.5 -> 2 and 1000 / 200 = 5
    assert qty(small) != qty(large)


def test_the_alpha_decides_the_direction_and_the_exit_rule_the_protection():
    result = run(composite(FixedContracts(1)))
    first_bar, batch = result.intents[0]
    assert batch[0].side == "sell" and first_bar == 8                                    # the falling market: a short view
    assert batch[0].sl == ATR(2) and batch[0].tp == ATR(3) and batch[0].tag == "cross"
    sides = {i.side for _, b in result.intents for i in b}
    assert sides == {"buy", "sell"}                                                       # the market turned up later
    flat = run(composite(FixedContracts(1), NoExit()))
    assert all(i.sl is None and i.tp is None for _, b in flat.intents for i in b)


def make_ctx(position, fast, slow):
    n = 10
    arrays = {"close": np.full(n, 100.0), "open": np.full(n, 100.0), "high": np.full(n, 100.0), "low": np.full(n, 100.0), "volume": np.ones(n)}
    return build_context(cursor=n, times=minutes(n), contract_id=CID, bars=arrays,
                         indicators={"ema_fast": np.full(n, fast), "ema_slow": np.full(n, slow)}, params={}, position=position, equity=D(100000))


def test_hold_reverse_and_flatten_by_the_position_held():
    strategy = composite(FixedContracts(2))
    long = Position(CID, D(3), D(100))
    assert strategy.on_bar(make_ctx(long, 101.0, 100.0)) == ()                            # long view while long: nothing to do
    out = strategy.on_bar(make_ctx(long, 99.0, 100.0))                                     # the view turned: leave, then enter the other way
    assert [(i.side, i.reduce_only, i.size.value) for i in out] == [("sell", True, D(3)), ("sell", False, D(2))]
    short = Position(CID, D(-2), D(100))
    out = strategy.on_bar(make_ctx(short, 101.0, 100.0))
    assert [(i.side, i.reduce_only) for i in out] == [("buy", True), ("buy", False)]
    quiet = Composite(EmaCross(fast=3, slow=8), FixedContracts(1), NoExit(), contract_id=CID, desk_id=1, line="DT", min_conviction=0.6,
                      library=LIBRARY)
    weak = quiet.on_bar(make_ctx(long, 100.4, 100.0))                                      # 0.004 / 0.01 = 0.4 is below the conviction floor
    assert [(i.side, i.reduce_only) for i in weak] == [("sell", True)]                    # a flat view closes what is held and opens nothing


def test_the_sizer_and_the_execution_style_are_replaceable():
    class Skip:
        def size(self, *, signal, ctx):
            return None

    class Limit:
        def style(self, *, side, ctx):
            return ExecutionStyle("limit", limit=D("99.5"))

    assert composite(Skip()).on_bar(make_ctx(None, 101.0, 100.0)) == ()
    out = Composite(EmaCross(fast=3, slow=8), FixedContracts(1), NoExit(), Limit(), contract_id=CID, desk_id=1, line="DT",
                    library=LIBRARY).on_bar(make_ctx(None, 101.0, 100.0))
    assert out[0].order_type == "limit" and out[0].limit == D("99.5")


def test_an_abstaining_alpha_gives_no_order_a_missing_indicator_fails_and_bad_parts_fail():
    assert composite(FixedContracts(1)).on_bar(make_ctx(None, float("nan"), 100.0)) == ()   # the averages have no value yet: the alpha abstains
    ctx = make_ctx(None, 101.0, 100.0)
    broken = BarContext(**{**ctx.__dict__, "indicators": {}})
    with pytest.raises(KeyError):
        composite(FixedContracts(1)).on_bar(broken)
    with pytest.raises(BadStrategy):
        Composite(object(), FixedContracts(1), NoExit(), contract_id=CID, desk_id=1, line="DT")      # type: ignore[arg-type]
    with pytest.raises(BadStrategy, match="no 'ema'"):
        strategy = Composite(EmaCross(fast=3, slow=8), FixedContracts(1), NoExit(), contract_id=CID, desk_id=1, line="DT", library={})
        replay_intents(strategy, {"close": closes()}, times=minutes(60), contract_id=CID)
    with pytest.raises(BadStrategy):
        Composite(EmaCross(fast=3, slow=8), FixedContracts(1), NoExit(), contract_id=CID, desk_id=1, line="DT", min_conviction=1.0)

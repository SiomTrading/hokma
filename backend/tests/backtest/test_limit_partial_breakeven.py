"""Limit entry, partial exit and breakeven in the backtest of the engine (1.2.4, task 3422): one test per piece, with bars worked out by hand."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from backtest import support
from backtest.runner_support import NQ, Price, source_of, spec
from backtest.support import requires_engine
from dbsupport import db, env  # noqa: F401  (fixtures)

from hokma.backtest import runner
from hokma.backtest.ticks import Bar
from hokma.factory.orders import OrderIntent, Size, SizeType, buy, reduce_intent
from hokma.factory.params import ParamSchema

pytestmark = requires_engine
START = datetime(2025, 11, 10, 15, 0, tzinfo=timezone.utc)


def series(rows):
    """Bars of one minute from (open, high, low, close) tuples."""
    return [Bar(START + timedelta(minutes=i), D(str(o)), D(str(h)), D(str(l)), D(str(c)), D(100)) for i, (o, h, l, c) in enumerate(rows)]


class Script:
    """Emits what `plan` says at each bar index; a plan value is a function of the context or a list of intents."""

    params = ParamSchema(())
    protective_orders = True

    def __init__(self, plan):
        self.plan = plan

    def init(self, ctx):
        pass

    def on_bar(self, ctx):
        made = self.plan.get(ctx.index, [])
        return made(ctx) if callable(made) else made


def go(rows, plan, db, tmp_path):
    bars = series(rows)
    return runner.run(spec(bars), strategy=Script(plan), db=db, curve_dir=tmp_path, source=source_of({NQ: bars}))


def flat(n, price=20000):
    return [(price, price + 1, price - 1, price)] * n


def long_limit(limit, qty=1, sl=19980, tp=20020, **kw):
    return buy(NQ, qty, limit=limit, sl=Price(sl), tp=Price(tp), tag="lim", **kw)


# ---------------------------------------------------------------------------------------------------------------- 1. the limit entry
def test_a_long_limit_fills_at_its_price_when_the_low_reaches_it_and_the_target_closes_it(db, tmp_path):
    rows = flat(3) + [(20000, 20003, 19990, 20001), (20001, 20021, 20000, 20020)] + flat(3)       # bar 3: low 19990 touches the limit; bar 4: high 20021 touches the target
    result = go(rows, {3: [long_limit(19990)]}, db, tmp_path)
    assert result.status == "done", result.warnings
    (trade,) = result.trades
    assert trade.entry_price == D("19990") and trade.exit_price >= D("20020") and trade.qty == 1 and trade.exit_tag == "target"


def test_a_limit_that_the_bar_does_not_reach_is_cancelled_after_its_life_of_one_bar(db, tmp_path):
    rows = flat(3) + [(20000, 20003, 19995, 20001), (20001, 20003, 19980, 20000)] + flat(3)        # bar 3 stays above 19990; bar 4 would touch it but the order is gone
    result = go(rows, {3: [long_limit(19990)]}, db, tmp_path)
    assert result.status == "done" and result.trades == ()


def test_a_short_limit_fills_when_the_high_reaches_it_and_a_gap_in_favour_fills_at_the_open(db, tmp_path):
    from hokma.factory.orders import sell

    rows = flat(3) + [(20000, 20011, 19999, 20005)] + flat(4)
    short = sell(NQ, 1, limit=20010, sl=Price(20030), tp=Price(19990), tag="short")
    result = go(rows, {3: [short]}, db, tmp_path)
    (trade,) = result.trades
    assert result.status == "done" and trade.side in ("short", "sell") and trade.entry_price == D("20010")
    gap = flat(3) + [(20020, 20022, 20015, 20016)] + flat(4, 20016)                                # opens above the short limit: fills at the open, plus the tick against
    result = go(gap, {3: [short]}, db, tmp_path)
    (trade,) = result.trades
    assert D("20019") <= trade.entry_price <= D("20020")


# ---------------------------------------------------------------------------------------------------------------- 2. the partial exit
def test_a_partial_exit_closes_that_quantity_with_its_own_trade_and_the_rest_keeps_the_protection(db, tmp_path):
    def plan(ctx):
        if ctx.index == 3:
            return [buy(NQ, 2, sl=Price(19980), tp=Price(20030), tag="two")]
        if ctx.index == 5 and ctx.position is not None and abs(ctx.position.qty) == 2:
            return [reduce_intent(NQ, ctx.position.side, 1, tag="half")]
        return []

    rows = flat(6) + [(20000, 20001, 19979, 19985)] + flat(2)                                     # bar 6 touches the stop of the remaining contract
    result = go(rows, {3: plan, 5: plan}, db, tmp_path)
    assert result.status == "done", result.warnings
    (position,) = result.trades                                                                    # one life of the position, with both exits inside it
    assert position.qty == 2 and position.exit_tag == "stop"
    first_leg, second_leg = (D("19999.75") - position.entry_price) * 20, (D("19979.75") - position.entry_price) * 20   # half out at the open of bar 5, the rest at the stop of bar 6, one tick against each
    assert position.pnl_usd == first_leg + second_leg - position.costs_usd and position.costs_usd > 0
    assert D("19989") < position.exit_price < D("19990")                                           # the average of 19999.75 and 19979.75: the stop of the rest stayed alive


# ---------------------------------------------------------------------------------------------------------------- 3. the stop to breakeven
def test_the_stop_moved_to_breakeven_is_the_one_that_fills_from_the_next_bar(db, tmp_path):
    def plan(ctx):
        if ctx.index == 3:
            return [buy(NQ, 1, sl=Price(19980), tp=Price(20040), tag="be")]
        if ctx.index == 5 and ctx.position is not None and ctx.position.qty != 0:
            side = "sell" if ctx.position.qty > 0 else "buy"
            return [OrderIntent(NQ, side, Size(SizeType.CONTRACTS, 1), "stop", None, D("20000"), reduce_only=True, tag="breakeven")]
        return []

    rows = flat(3) + [(20000, 20010, 19999, 20008), (20008, 20012, 20000, 20010), (20010, 20011, 19999, 20000)] + flat(2)    # bar 5 (next bar after the move) falls through 20000
    result = go(rows, {3: plan, 5: plan}, db, tmp_path)
    (trade,) = result.trades
    assert result.status == "done" and trade.exit_tag == "stop"
    assert D("19998") <= trade.exit_price <= D("20000")                                            # the original stop was 19980: only the move to 20000 explains this exit


def test_a_stop_that_moves_against_the_position_is_refused(db, tmp_path):
    def plan(ctx):
        if ctx.index == 3:
            return [buy(NQ, 1, sl=Price(19990), tp=Price(20040), tag="loose")]
        if ctx.index == 5 and ctx.position is not None and ctx.position.qty != 0:
            return [OrderIntent(NQ, "sell", Size(SizeType.CONTRACTS, 1), "stop", None, D("19970"), reduce_only=True)]
        return []

    result = go(flat(8), {3: plan, 5: plan}, db, tmp_path)
    assert result.status == "failed" and "failed_code:loosen_stop" in result.warnings


def test_stop_limit_orders_are_still_not_supported(db, tmp_path):
    result = go(flat(6), {3: [buy(NQ, 1, limit=19990, stop=19995)]}, db, tmp_path)
    assert result.status == "failed" and "failed_code:order_type_unsupported" in result.warnings

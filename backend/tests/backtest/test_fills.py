"""Fill rules (SPEC-020 section 4.3): B-05, B-06, B-44, V-06, V-11, V-12, L-12, L-13, N-09, N-12.

Every expected value is the hand computed one of the fixture (real NQ bars, tick 0.25, multiplier 20, 2.25 USD per side per contract):
  T1  buy 25637.50 (open 25637.25 + 1 tick), sell 25642.75 (open 25643.00 - 1 tick): (25642.75 - 25637.50) * 20 - 4.50 = 100.50
  T2  entry open 25471.75, stop and target 5 points away, the bar of 05:06 (high 25478.50, low 25465.25) touches both:
      long stop 25466.75 fills at 25466.50: (25466.50 - 25472.00) * 20 - 4.50 = -114.50
      short stop 25476.75 fills at 25477.00: (25471.50 - 25477.00) * 20 - 4.50 = -114.50
  T3  short at 25218.25, stop 25228.50, Sunday open 25320.75 fills the stop at 25321.00: (25218.25 - 25321.00) * 20 - 4.50 = -2059.50
  T4  buy 25552.00 (open 25551.75 + 1 tick), sell 25549.75: (25549.75 - 25552.00) * 20 - 4.50 = -49.50
"""
from decimal import Decimal as D

import pytest

from backtest.support import MULT, TICK, bars_of, expected, requires_engine, scenario_run, spec
from hokma.backtest import fills
from hokma.backtest.fills import ExitChoice, Order, Resolution, resolve, stop_first, stop_price
from hokma.backtest.spec import FillRules, ForbiddenFill, SpecError
from hokma.backtest.ticks import Bar

RULES = FillRules()


def bar_at(name: str, hhmm: str) -> Bar:
    return next(b for b in bars_of(name) if b.ts.strftime("%H:%M") == hhmm)


def pnl(side: str, entry: D, exit_: D) -> D:
    return (exit_ - entry if side == "long" else entry - exit_) * MULT - 2 * D("2.25")


# --- pure rules: resolve, stop_first, stop_price, by_order_type ------------------------------------------------------------------------

def test_t1_buys_at_the_next_open_plus_one_tick_and_sells_one_tick_under_the_open():
    buy = resolve(Order("market", "buy"), bar_at("T1", "15:00"), RULES, TICK)
    sell = resolve(Order("market", "sell"), bar_at("T1", "15:30"), RULES, TICK)
    assert (buy.status, buy.price) == ("filled", D("25637.50")) and sell.price == D("25642.75")
    assert pnl("long", buy.price, sell.price) == D(expected("T1")["pnl_usd"]) == D("100.50")


def test_t4_enters_at_open_plus_one_tick_and_exits_one_tick_under_the_open():
    buy = resolve(Order("market", "buy"), bar_at("T4", "13:33"), RULES, TICK)
    sell = resolve(Order("market", "sell"), bar_at("T4", "14:03"), RULES, TICK)
    assert buy.price == D("25552.00") and sell.price == D("25549.75")
    assert pnl("long", buy.price, sell.price) == D("-49.50")


def test_the_market_fill_never_uses_the_close_of_the_signal_bar():
    signal, nxt = bar_at("T4", "13:32"), bar_at("T4", "13:33")
    assert signal.close == D("25552.50") and nxt.open == D("25551.75")
    assert resolve(Order("market", "buy"), nxt, RULES, TICK).price == nxt.open + TICK != signal.close + TICK


def test_the_stop_is_measured_from_the_entry_fill_reference_and_never_from_the_close_of_the_entry_bar():
    entry_bar = bar_at("T2", "05:05")
    assert entry_bar.open == D("25471.75") and entry_bar.close == D("25473.25")
    assert stop_price("long", entry_bar.open, D(5)) == D("25466.75") == D(expected("T2")["stop_level"])
    assert stop_price("short", entry_bar.open, D(5)) == D("25476.75")
    assert stop_price("long", entry_bar.close, D(5)) == D("25468.25") != D("25466.75")      # the trap: a stop 1.50 points too tight
    with pytest.raises(SpecError):
        stop_price("long", entry_bar.open, D(0))


def test_t2_both_levels_inside_the_bar_the_stop_fills_for_the_long_and_for_the_short():
    both = bar_at("T2", "05:06")
    assert both.high >= D("25476.75") and both.low <= D("25466.75")
    long_exit = stop_first("long", D("25466.75"), D("25476.75"), both)
    short_exit = stop_first("short", D("25476.75"), D("25466.75"), both)
    assert long_exit == short_exit == ExitChoice("stop", ambiguous=True, at_open=False)
    long_fill = fills.by_order_type(Order("stop", "sell", trigger=D("25466.75")), both, RULES, TICK)
    short_fill = fills.by_order_type(Order("stop", "buy", trigger=D("25476.75")), both, RULES, TICK)
    assert long_fill.price == D("25466.50") and short_fill.price == D("25477.00")
    assert pnl("long", D("25472.00"), long_fill.price) == pnl("short", D("25471.50"), short_fill.price) == D("-114.50")


def test_stop_first_gap_and_single_touch_cases():
    bar = Bar(bar_at("T2", "05:06").ts, D("100"), D("102"), D("99"), D("101"))
    assert stop_first("long", D("98"), D("103"), bar) == ExitChoice(None)
    assert stop_first("long", D("99"), D("103"), bar) == ExitChoice("stop")
    assert stop_first("long", D("98"), D("102"), bar) == ExitChoice("target")
    assert stop_first("short", D("103"), D("98"), bar) == ExitChoice(None)
    gap_down = Bar(bar.ts, D("95"), D("96"), D("94"), D("95"))
    assert stop_first("long", D("98"), D("103"), gap_down) == ExitChoice("stop", at_open=True)
    gap_up = Bar(bar.ts, D("104"), D("105"), D("103"), D("104"))
    assert stop_first("long", D("98"), D("103"), gap_up) == ExitChoice("target", at_open=True)


def test_t3_a_stop_crossed_by_the_sunday_gap_fills_at_the_open_plus_one_tick():
    sunday = bar_at("T3", "23:00")
    stop = D(expected("T3")["stop_level"])
    assert stop == D("25228.50") and sunday.open == D("25320.75") and sunday.low > stop
    out = fills.by_order_type(Order("stop", "buy", trigger=stop), sunday, RULES, TICK)
    assert out == Resolution("filled", D("25321.00"), "gap_at_open")
    assert out.price != stop + TICK                                               # not the level that was waiting
    assert pnl("short", D("25218.25"), out.price) == D("-2059.50")


def test_a_limit_that_is_not_crossed_does_not_fill_and_a_limit_through_the_bar_fills_at_its_price():
    bar = Bar(bar_at("T2", "05:06").ts, D("100"), D("101"), D("98"), D("100.5"))
    assert fills.by_order_type(Order("limit", "buy", limit=D("97")), bar, RULES, TICK) == Resolution("open")
    assert fills.by_order_type(Order("limit", "buy", limit=D("98")), bar, RULES, TICK) == Resolution("open")        # touching is not trading through
    assert fills.by_order_type(Order("limit", "buy", limit=D("99")), bar, RULES, TICK) == Resolution("filled", D("99"), "limit")
    assert fills.by_order_type(Order("limit", "sell", limit=D("101.5")), bar, RULES, TICK) == Resolution("open")
    assert fills.by_order_type(Order("limit", "sell", limit=D("100.5")), bar, RULES, TICK).price == D("100.5")
    assert fills.by_order_type(Order("limit", "buy", limit=D("101")), bar, RULES, TICK) == Resolution("filled", D("100"), "open_through_limit")


def test_a_stop_limit_crossed_by_a_gap_beyond_its_limit_is_left_unfilled():
    sunday = bar_at("T3", "23:00")
    beyond = fills.by_order_type(Order("stop_limit", "buy", trigger=D("25228.50"), limit=D("25240.00")), sunday, RULES, TICK)
    assert beyond == Resolution("open", None, "beyond_limit")
    inside = fills.by_order_type(Order("stop_limit", "buy", trigger=D("25228.50"), limit=D("25400.00")), sunday, RULES, TICK)
    assert inside == Resolution("filled", D("25320.75"), "gap_at_open")                       # a limit never pays the tick against
    quiet = Bar(sunday.ts, D("100"), D("101"), D("99"), D("100"))
    assert fills.by_order_type(Order("stop_limit", "buy", trigger=D("100.5"), limit=D("101")), quiet, RULES, TICK) == Resolution("filled", D("100.5"), "trigger")


def test_trailing_stop_and_limit_if_touched():
    bar = Bar(bar_at("T2", "05:06").ts, D("100"), D("102"), D("97"), D("101"))
    sell_trail = Order("trailing_stop", "sell", trail=D(2), extreme=D("101"))           # level 99, the low 97 reaches it first
    assert fills.by_order_type(sell_trail, bar, RULES, TICK) == Resolution("filled", D("98.75"), "trigger")
    far = Order("trailing_stop", "sell", trail=D(10), extreme=D("101"))                 # level 91, not reached
    assert fills.by_order_type(far, bar, RULES, TICK) == Resolution("open")
    gapped = Order("trailing_stop", "buy", trail=D(1), extreme=D("96"))                 # level 97, the open 100 is already above it: the open fills
    assert fills.by_order_type(gapped, bar, RULES, TICK) == Resolution("filled", D("100.25"), "gap_at_open")
    buy_trail = Order("trailing_stop", "buy", trail=D(5), extreme=D("96"))              # level 101, the high 102 crosses it
    assert fills.by_order_type(buy_trail, bar, RULES, TICK) == Resolution("filled", D("101.25"), "trigger")
    touched = fills.by_order_type(Order("limit_if_touched", "buy", trigger=D("98")), bar, RULES, TICK)
    assert touched == Resolution("filled", D("98.25"), "trigger")                       # market after the touch: one tick against
    not_touched = fills.by_order_type(Order("limit_if_touched", "buy", trigger=D("96")), bar, RULES, TICK)
    assert not_touched == Resolution("open")
    assert fills.by_order_type(Order("limit_if_touched", "buy", trigger=D("100")), bar, RULES, TICK).reason == "gap_at_open"


def test_an_order_needs_its_fields():
    for kwargs in ({"type": "stop"}, {"type": "limit"}, {"type": "stop_limit", "trigger": D(1)}, {"type": "trailing_stop", "trail": D(1)}):
        with pytest.raises(SpecError):
            Order(side="buy", **kwargs)
    with pytest.raises(SpecError):
        Order("bracket", "buy")


# --- N-12: the synthetic ticks of a bar -------------------------------------------------------------------------------------------------

def kinds_prices(points):
    return [(p.kind, str(p.price)) for p in points]


def test_the_ticks_of_the_t2_bar_put_the_stop_before_the_target_for_the_long_and_for_the_short():
    from hokma.backtest.ticks import bar_to_ticks
    both = bar_at("T2", "05:06")                  # open 25472.25, high 25478.50, low 25465.25, close 25477.50
    long = bar_to_ticks(both, side="long", stop=D("25466.75"), target=D("25476.75"))
    assert kinds_prices(long) == [("open", "25472.25"), ("stop", "25466.75"), ("low", "25465.25"), ("target", "25476.75"),
                                  ("high", "25478.50"), ("close", "25477.50")]
    short = bar_to_ticks(both, side="short", stop=D("25476.75"), target=D("25466.75"))
    assert kinds_prices(short) == [("open", "25472.25"), ("stop", "25476.75"), ("high", "25478.50"), ("target", "25466.75"),
                                   ("low", "25465.25"), ("close", "25477.50")]
    for points in (long, short):
        assert [p.offset_ns for p in points] == sorted(p.offset_ns for p in points) and len({p.offset_ns for p in points}) == len(points)
        assert points[-1].offset_ns < 60 * 10**9


def test_the_ticks_of_a_bar_skip_levels_outside_the_range_and_the_extremes_follow_the_position():
    from hokma.backtest.ticks import bar_to_ticks
    bar = bar_at("T2", "05:05")                   # open 25471.75, high 25475.75, low 25471.00, close 25473.25
    flat = bar_to_ticks(bar, side=None)
    assert kinds_prices(flat) == [("open", "25471.75"), ("low", "25471.00"), ("high", "25475.75"), ("close", "25473.25")]
    out_of_range = bar_to_ticks(bar, side="long", stop=D("25466.75"), target=D("25476.75"))
    assert [p.kind for p in out_of_range] == ["open", "low", "high", "close"]
    on_open = bar_to_ticks(bar, side="long", stop=bar.open)                              # a level equal to the open gets no tick of its own
    assert [p.kind for p in on_open] == ["open", "low", "high", "close"]
    assert [p.kind for p in bar_to_ticks(bar, side="short")][:3] == ["open", "high", "low"]
    assert kinds_prices(bar_to_ticks(bar, side="long", fast=True)) == [("open", "25471.75")]
    with pytest.raises(ValueError):
        bar_to_ticks(bar, side=None, span_ns=10)
    with pytest.raises(ValueError):
        Bar(bar.ts, D("100"), D("99"), D("98"), D("100"))


def test_one_implementation_each_for_the_stop_rule_and_the_market_fill():
    """V-12 is the stop_first of B-06 and L-12 is the resolve of B-05: the engine wrapper imports the very same functions."""
    from hokma.backtest import _lib
    assert _lib.stop_first is fills.stop_first and _lib.stop_price is fills.stop_price and _lib.reject_prob is fills.reject_prob
    assert fills.resolve is resolve and fills.by_order_type.__module__ == fills.stop_first.__module__ == "hokma.backtest.fills"


# --- N-09: the model catalog and V-06: reject_prob ---------------------------------------------------------------------------------------

def test_the_best_price_model_is_forbidden_and_the_catalog_matches_the_spec():
    with pytest.raises(ForbiddenFill) as err:
        FillRules(model="best_price")
    assert err.value.code == "forbidden_fill"
    assert set(fills.MODELS) == {"one_tick", "probabilistic"} and set(fills.FORBIDDEN_MODELS).isdisjoint(fills.MODELS)
    with pytest.raises(ForbiddenFill):
        FillRules(stop_first=False)
    with pytest.raises(ForbiddenFill):
        FillRules(next_open_ticks=0)


def test_the_probabilistic_model_repeats_with_a_fixed_seed_and_never_pays_more_than_one_tick():
    prob = FillRules(model="probabilistic")
    bar = bar_at("T4", "13:33")
    first = [resolve(Order("market", "buy"), bar, prob, TICK, seed=11, index=i).price for i in range(200)]
    again = [resolve(Order("market", "buy"), bar, prob, TICK, seed=11, index=i).price for i in range(200)]
    other = [resolve(Order("market", "buy"), bar, prob, TICK, seed=12, index=i).price for i in range(200)]
    assert first == again and first != other
    assert set(first) == {bar.open, bar.open + TICK}                                   # half of the fills pay the tick, none pay more
    assert 60 <= first.count(bar.open + TICK) <= 140


def test_reject_prob_is_seeded_counted_and_zero_rejects_nothing():
    one = [fills.reject_prob(7, i, 0.1) for i in range(1000)]
    two = [fills.reject_prob(7, i, 0.1) for i in range(1000)]
    assert one == two and one != [fills.reject_prob(8, i, 0.1) for i in range(1000)]
    assert 60 <= sum(one) <= 140                                                         # 1000 draws at 10 percent: 100 plus or minus 3 sigma
    assert not any(fills.reject_prob(7, i, 0.0) for i in range(1000))
    rules = FillRules(reject_prob=0.5)
    resolved = [resolve(Order("market", "buy"), bar_at("T4", "13:33"), rules, TICK, seed=3, index=i) for i in range(100)]
    rejected = [r for r in resolved if r.status == "rejected"]
    assert rejected and all(r.reason == "reject_prob" and r.price is None for r in rejected)
    assert len(rejected) == sum(fills.reject_prob(3, i, 0.5) for i in range(100))
    with pytest.raises(SpecError):
        fills.reject_prob(1, 1, 1.0)


def test_the_mode_is_fast_only_without_protective_orders():
    assert fills.choose_mode(False) == "fast" and fills.choose_mode(True) == "ticks"


# --- the engine: the same numbers through the service (N-27, N-12) ----------------------------------------------------------------------

@requires_engine
def test_t1_through_the_engine_is_100_50_and_says_it_ran_fast():
    out, _ = scenario_run("T1")
    assert out.mode == "fast" and out.realized_pnl_usd == D("100.50") == D(expected("T1")["pnl_usd"])
    assert [(f.side, f.price) for f in out.fills] == [("buy", D("25637.50")), ("sell", D("25642.75"))]
    assert out.costs_usd["commission"] == D("4.50") and out.costs_usd["slippage"] == D("10.00")


@requires_engine
def test_t4_through_the_engine_enters_25552_and_exits_25549_75():
    out, _ = scenario_run("T4")
    assert [f.price for f in out.fills] == [D("25552.00"), D("25549.75")] and out.realized_pnl_usd == D("-49.50")
    assert out.trades[0].entry_price == D("25552.00") and out.trades[0].exit_price == D("25549.75")


@requires_engine
@pytest.mark.parametrize("side,entry,exit_", [("long", "25472.00", "25466.50"), ("short", "25471.50", "25477.00")])
def test_t2_through_the_engine_fills_the_stop_and_never_the_target(side, entry, exit_):
    out, _ = scenario_run("T2", side)
    assert out.mode == "ticks" and out.ambiguous_bars == 1
    assert [f.kind for f in out.fills] == ["entry", "stop"]
    assert [f.price for f in out.fills] == [D(entry), D(exit_)] and out.realized_pnl_usd == D("-114.50")
    assert out.fills[1].reference == (D("25466.75") if side == "long" else D("25476.75"))     # measured from the entry open, not the close


@requires_engine
def test_t3_through_the_engine_fills_the_gap_at_the_sunday_open_plus_one_tick():
    out, _ = scenario_run("T3", "short")
    assert [f.price for f in out.fills] == [D("25218.25"), D("25321.00")] and out.realized_pnl_usd == D("-2059.50")
    assert out.fills[1].kind == "stop" and out.costs_usd["slippage"] == D("10.00")          # the gap is a loss, the slippage is only the tick


@requires_engine
@pytest.mark.parametrize("name,side", [("T1", "long"), ("T4", "long"), ("T2", "long"), ("T2", "short"), ("T3", "short")])
def test_the_engine_fills_are_the_prices_of_the_pure_rules(name, side):
    """Identity of the two paths: what the engine filled is what resolve and by_order_type say for the same bars."""
    out, bars = scenario_run(name, side)
    by_ts = {int(b.ts.timestamp()) * 10**9: b for b in bars}
    for f in out.fills:
        bar = by_ts[max(t for t in by_ts if t <= f.ts_ns)]
        if f.kind in ("entry", "exit"):
            rule = resolve(Order("market", f.side), bar, RULES, TICK)
        else:
            rule = fills.by_order_type(Order("stop", f.side, trigger=f.reference), bar, RULES, TICK)
        assert rule.price == f.price, (name, f)


@requires_engine
def test_control_the_engine_default_order_of_the_bar_fills_the_target_first():
    """With the bars alone, in the order the engine uses by default, T2 fills the target before the stop: the optimistic error that
    the synthetic ticks of ticks.py correct. If a new engine version changes this, the test says so."""
    from backtest.support import bars_of as get
    from hokma.backtest import _lib
    from hokma.backtest.fills import Decision

    bars = get("T2")
    decide = lambda i, bar, side: [Decision("enter", "long", stop_points=D(5), target_points=D(5))] if i == 1 else []
    inst = _lib.InstrumentSpec("NQZ5", TICK, MULT)
    kw = dict(bars=bars, instrument=inst, decide=decide, rules=RULES, starting_balance=D(1_000_000), per_contract_fee=D("2.25"))
    default = _lib.run_bars(feed="engine_default", **kw)
    exits = [f for f in default.fills if f.kind in ("stop", "target")]
    assert exits[0].kind == "target"
    corrected = _lib.run_bars(feed="ticks", **kw)
    assert [f.kind for f in corrected.fills] == ["entry", "stop"]


@requires_engine
def test_reject_prob_through_the_engine_counts_the_rejections_and_repeats():
    from hokma.backtest import engine
    from hokma.backtest.fills import Decision

    bars = bars_of("T1")
    always = lambda i, bar, side: [Decision("enter", "long")] if side is None else [Decision("exit")]
    outs = [engine.run(spec(fill=FillRules(reject_prob=0.5), seed=5), bars, always, tick_size=TICK, multiplier=MULT, symbol="NQZ5",
                       protective_orders=False) for _ in range(2)]
    assert outs[0].fills == outs[1].fills and outs[0].rejected == outs[1].rejected
    none = engine.run(spec(), bars, always, tick_size=TICK, multiplier=MULT, symbol="NQZ5", protective_orders=False)
    assert none.rejected == {"reject_prob": 0} and len(none.fills) >= 2
    rej = outs[0].rejected["reject_prob"]
    attempts = rej + len(outs[0].fills)                                                     # every attempt is either rejected or filled
    assert rej == sum(fills.reject_prob(5, i, 0.5) for i in range(attempts))


@requires_engine
def test_the_probabilistic_model_through_the_engine_repeats_with_the_same_seed():
    from hokma.backtest import engine
    from hokma.backtest.fills import Decision

    bars = bars_of("T1")
    always = lambda i, bar, side: [Decision("enter", "long")] if side is None else [Decision("exit")]
    runs = [engine.run(spec(fill=FillRules(model="probabilistic"), seed=9), bars, always, tick_size=TICK, multiplier=MULT, symbol="NQZ5",
                       protective_orders=False) for _ in range(2)]
    assert runs[0].fills == runs[1].fills and runs[0].fills

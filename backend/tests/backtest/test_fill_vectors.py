"""Fill vectors with hand computed results (SPEC-020 V-39): one tick against on a buy and on a sell, the smallest and the largest order,
the stop of a buy and of a sell, and a limit that is not crossed. Tick 0.25, multiplier 20, 2.25 USD per side per contract."""
from decimal import Decimal as D

import pytest

from backtest.support import MULT, TICK, bars_of, requires_engine, spec
from hokma.backtest import costs, engine, fills
from hokma.backtest.fills import Decision, Order, Resolution, resolve
from hokma.backtest.spec import CostModel, FillRules, SpecError
from hokma.backtest.ticks import Bar
from hokma.quant import limits
from hokma.quant.errors import BadInput

RULES = FillRules()
BAR = Bar(bars_of("T1")[0].ts, D("100.00"), D("101.00"), D("98.50"), D("100.50"), D(1000))


def test_a_buy_pays_one_tick_over_the_open_and_a_sell_gets_one_tick_under_it():
    assert resolve(Order("market", "buy"), BAR, RULES, TICK).price == D("100.25")
    assert resolve(Order("market", "sell"), BAR, RULES, TICK).price == D("99.75")
    assert resolve(Order("market", "buy"), BAR, FillRules(next_open_ticks=2), TICK).price == D("100.50")      # 2 ticks: 100 + 0.50
    assert resolve(Order("market", "buy"), BAR, RULES, D("0.01")).price == D("100.01")                       # the tick of a stock


def test_the_smallest_order_is_one_unit_and_zero_or_negative_is_refused():
    assert Decision("enter", "long", qty=D(1)).qty == D(1)
    assert costs.commission_usd(costs.fee_model(CostModel("schwab")), qty=D(1), price=D(100), multiplier=MULT) == D("2.25")
    for qty in (D(0), D(-1)):
        with pytest.raises(SpecError):
            Decision("enter", "long", qty=qty)
        with pytest.raises(SpecError):
            Order("market", "buy", qty=qty)


def test_the_largest_order_is_the_whole_volume_of_the_bar_and_not_one_unit_more():
    """Share 1.0: 1000 bp is 10 percent, 25000 * 1.10 = 27500.00 against the trader. One unit more than the bar traded cannot fill."""
    assert costs.volume_share("buy", D("25000"), D(1000), D(1000)) == D("27500.0")
    with pytest.raises(SpecError):
        costs.volume_share("buy", D("25000"), D(1001), D(1000))


@requires_engine
def test_two_contracts_double_the_result_and_the_commission_of_one_per_contract():
    """T1 bars with 2 contracts: price result 2 * 5.25 * 20 = 210.00, commission 2 sides * 2 contracts * 2.25 = 9.00, net 201.00."""
    bars = bars_of("T1")
    decide = lambda i, bar, side: ([Decision("enter", "long", qty=D(2))] if i == 1 else [Decision("exit")] if i == 5 else [])
    out = engine.run(spec(), bars, decide, tick_size=TICK, multiplier=MULT, symbol="NQZ5", protective_orders=False)
    assert [f.qty for f in out.fills] == [D(2), D(2)] and out.costs_usd["commission"] == D("9.00") and out.realized_pnl_usd == D("201.00")


def test_a_buy_stop_fills_above_its_trigger_and_a_sell_stop_below_it():
    bar = Bar(BAR.ts, D("100.00"), D("101.00"), D("98.50"), D("100.50"))
    assert fills.by_order_type(Order("stop", "buy", trigger=D("100.75")), bar, RULES, TICK) == Resolution("filled", D("101.00"), "trigger")
    assert fills.by_order_type(Order("stop", "sell", trigger=D("99.00")), bar, RULES, TICK) == Resolution("filled", D("98.75"), "trigger")
    assert fills.by_order_type(Order("stop", "buy", trigger=D("101.50")), bar, RULES, TICK) == Resolution("open")       # never reached
    assert fills.by_order_type(Order("stop", "sell", trigger=D("98.00")), bar, RULES, TICK) == Resolution("open")
    assert fills.by_order_type(Order("stop", "buy", trigger=D("99.50")), bar, RULES, TICK) == Resolution("filled", D("100.25"), "gap_at_open")


def test_a_limit_that_is_not_crossed_stays_open_for_a_buy_and_for_a_sell():
    assert fills.by_order_type(Order("limit", "buy", limit=D("98.00")), BAR, RULES, TICK) == Resolution("open")
    assert fills.by_order_type(Order("limit", "sell", limit=D("101.25")), BAR, RULES, TICK) == Resolution("open")
    assert fills.by_order_type(Order("limit", "sell", limit=D("100.75")), BAR, RULES, TICK) == Resolution("filled", D("100.75"), "limit")


def test_the_target_of_a_long_is_a_sell_limit_that_the_bar_must_trade_through():
    assert fills.target_price("long", D("25471.75"), D(5)) == D("25476.75") and fills.target_price("short", D("25471.75"), D(5)) == D("25466.75")
    assert fills.by_order_type(Order("limit", "sell", limit=D("25476.75")), bars_of("T2")[3], RULES, TICK).price == D("25476.75")


def test_the_size_limits_of_the_quant_service_apply_to_a_run(monkeypatch):
    monkeypatch.setitem(limits.PARAM_CAPS, "n_observations", 3)
    with pytest.raises(BadInput):
        engine.run(spec(), bars_of("T1"), lambda i, b, s: [], tick_size=TICK, multiplier=MULT, symbol="NQZ5", protective_orders=False)


@requires_engine
def test_a_protective_order_in_a_run_declared_fast_is_refused():
    bars = bars_of("T2")
    decide = lambda i, bar, side: [Decision("enter", "long", stop_points=D(5))] if i == 1 else []
    with pytest.raises(SpecError) as err:
        engine.run(spec(), bars, decide, tick_size=TICK, multiplier=MULT, symbol="NQZ5", protective_orders=False)
    assert "protective_in_fast_mode" in str(err.value)
    with pytest.raises(SpecError):
        Decision("enter", "long", target_points=D(5))                                           # a target without a stop


@requires_engine
def test_the_ticks_against_of_the_cost_model_and_of_the_fill_rule_must_agree():
    with pytest.raises(SpecError):
        engine.run(spec(costs=CostModel("schwab", slippage_ticks=2)), bars_of("T1"), lambda i, b, s: [], tick_size=TICK, multiplier=MULT,
                   symbol="NQZ5", protective_orders=False)
    with pytest.raises(SpecError) as err:
        engine.run(spec(costs=CostModel("schwab", slippage_ticks=2), fill=FillRules(next_open_ticks=2, model="probabilistic")), bars_of("T1"),
                   lambda i, b, s: [], tick_size=TICK, multiplier=MULT, symbol="NQZ5", protective_orders=False)
    assert "unsupported_ticks" in str(err.value)


@requires_engine
def test_two_ticks_against_move_every_fill_by_half_a_point_more():
    """T1 with 2 ticks per side: buy 25637.25 + 0.50 = 25637.75, sell 25643.00 - 0.50 = 25642.50: 4.75 * 20 - 4.50 = 90.50."""
    bars = bars_of("T1")
    decide = lambda i, bar, side: [Decision("enter", "long")] if i == 1 else [Decision("exit")] if i == 5 else []
    out = engine.run(spec(costs=CostModel("schwab", slippage_ticks=2), fill=FillRules(next_open_ticks=2)), bars, decide, tick_size=TICK,
                     multiplier=MULT, symbol="NQZ5", protective_orders=False)
    assert [f.price for f in out.fills] == [D("25637.75"), D("25642.50")] and out.realized_pnl_usd == D("90.50")

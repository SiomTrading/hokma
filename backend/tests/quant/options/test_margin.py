"""L-21: margin of a group of option legs. Hand computed requirements, covered calls, spreads, condors and uncovered shorts."""
from __future__ import annotations

import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.options import margin

from .conftest import assert_empty, call, piece_ids

MULT = 100


def leg(position, kind, strike, premium, quantity=1):
    return {"position": position, "kind": kind, "strike": strike, "premium": premium, "quantity": quantity}


def margin_of(legs, **kwargs):
    res = call("options.strategy_margin", legs=legs, multiplier=MULT, **kwargs)
    assert res.value is not None, res.warnings
    return res.value


def test_a_credit_spread_of_five_points_needs_500_minus_the_credit_L_21():
    # short 95 put at 2.00, long 90 put at 0.80: width 5 points times 100 = 500; credit (2.00 - 0.80) * 100 = 120
    v = margin_of([leg("short", "put", 95, 2.0), leg("long", "put", 90, 0.8)])
    assert v["requirement_usd"] == 500.0 and v["net_credit_usd"] == pytest.approx(120.0) and v["margin_usd"] == pytest.approx(380.0)
    assert v["structure"] == "credit_put_spread" and v["spread_requirement_usd"] == 500.0 and v["naked_requirement_usd"] == 0.0
    # the call side is the same arithmetic: short 105 call at 1.50, long 110 call at 0.50: 500 - 100
    assert margin_of([leg("short", "call", 105, 1.5), leg("long", "call", 110, 0.5)])["margin_usd"] == pytest.approx(400.0)
    assert piece_ids("options.strategy_margin") == ("L-21",)


def test_the_quantity_scales_the_spread():
    v = margin_of([leg("short", "put", 95, 2.0, 3), leg("long", "put", 90, 0.8, 3)])
    assert v["margin_usd"] == pytest.approx(3 * 380.0)


def test_an_uncovered_short_put_follows_the_written_formula():
    # spot 100, strike 95, premium 1.50: out of the money by 5. Per share: max(20 percent of 100 - 5, 10 percent of 95) = max(15, 9.5) = 15, plus the
    # premium 1.50: 16.50 per share, 1,650 per contract, less the credit of 150: 1,500
    v = margin_of([leg("short", "put", 95, 1.5)], spot=100.0)
    assert v["requirement_usd"] == pytest.approx(1650.0) and v["margin_usd"] == pytest.approx(1500.0) and v["structure"] == "short_put"
    # deep out of the money: strike 80, premium 0.20: 20 - 20 = 0, so the floor of 10 percent of the strike (8.00) applies: 800
    assert margin_of([leg("short", "put", 80, 0.2)], spot=100.0)["margin_usd"] == pytest.approx(800.0)
    # in the money: strike 105, spot 100: out of the money amount is 0: max(20, 10.5) = 20, so 2,000
    assert margin_of([leg("short", "put", 105, 6.0)], spot=100.0)["margin_usd"] == pytest.approx(2000.0)


def test_an_uncovered_short_call_follows_the_written_formula():
    # spot 100, strike 110, premium 1.00: out of the money by 10: max(20 - 10, 10 percent of the spot 100 = 10) = 10 per share: 1,000
    assert margin_of([leg("short", "call", 110, 1.0)], spot=100.0)["margin_usd"] == pytest.approx(1000.0)
    # strike 102: out of the money by 2: max(18, 10) = 18: 1,800
    assert margin_of([leg("short", "call", 102, 3.0)], spot=100.0)["margin_usd"] == pytest.approx(1800.0)


def test_a_covered_call_adds_nothing():
    stock = {"instrument": "stock", "position": "long", "quantity": 100, "premium": 100.0}
    v = margin_of([stock, leg("short", "call", 105, 1.2)])
    assert v["margin_usd"] == 0.0 and v["covered_contracts"] == 1.0 and v["structure"] == "covered_call"
    # two contracts and 100 shares: one is covered, the other is uncovered. Spot 100, strike 105: max(20 - 5, 10) = 15, plus 1.20 = 16.20 per share: 1,620,
    # less the credit of the uncovered contract only (120): 1,500. The covered contract's premium does not reduce the requirement.
    v = margin_of([stock, leg("short", "call", 105, 1.2, 2)], spot=100.0)
    assert v["requirement_usd"] == pytest.approx(1620.0) and v["margin_usd"] == pytest.approx(1500.0) and v["covered_contracts"] == 1.0
    # without a spot an uncovered leg cannot be margined
    assert_empty(call("options.strategy_margin", legs=[stock, leg("short", "call", 105, 1.2, 2)], multiplier=MULT), "spot is required")


def test_an_iron_condor_needs_its_wider_wing_less_the_whole_credit():
    wings = [leg("long", "put", 85, 0.4), leg("short", "put", 90, 1.0), leg("short", "call", 110, 1.1), leg("long", "call", 115, 0.5)]
    v = margin_of(wings)
    # both wings are 5 points: only one side can lose: 500. The credit is (1.0 - 0.4 + 1.1 - 0.5) * 100 = 120
    assert v["requirement_usd"] == 500.0 and v["margin_usd"] == pytest.approx(380.0) and v["structure"] == "iron_condor"
    wider = [leg("long", "put", 80, 0.4), leg("short", "put", 90, 1.0), leg("short", "call", 110, 1.1), leg("long", "call", 115, 0.5)]
    v = margin_of(wider)                                                     # put wing 10 points, call wing 5: the wider wing, 1,000 - 120
    assert v["requirement_usd"] == 1000.0 and v["margin_usd"] == pytest.approx(880.0)


def test_a_debit_spread_needs_no_requirement_and_its_margin_is_the_debit():
    v = margin_of([leg("long", "call", 100, 3.0), leg("short", "call", 105, 1.0)])
    assert v["requirement_usd"] == 0.0 and v["net_credit_usd"] == -200.0 and v["margin_usd"] == 200.0 and v["structure"] == "debit_call_spread"
    assert margin_of([leg("long", "put", 95, 2.5)])["margin_usd"] == 250.0         # a long option: the premium paid


def test_a_short_strangle_needs_the_larger_side_plus_the_premium_of_the_other():
    # spot 100. Put 90 at 1.00: max(20 - 10, 9) = 10, plus 1.00 = 11.00 per share (1,100). Call 110 at 1.50: max(20 - 10, 10) = 10, plus 1.50 = 11.50 (1,150).
    # The larger side (1,150) plus the premium of the other side (100) is 1,250; less the credit 250: 1,000
    v = margin_of([leg("short", "put", 90, 1.0), leg("short", "call", 110, 1.5)], spot=100.0)
    assert v["requirement_usd"] == pytest.approx(1250.0) and v["margin_usd"] == pytest.approx(1000.0) and v["structure"] == "short_strangle"


def test_a_cash_secured_put_needs_its_strike_in_cash():
    v = margin_of([leg("short", "put", 50, 1.0)], spot=60.0, cash_secured=True)
    assert v["requirement_usd"] == 5000.0 and v["margin_usd"] == pytest.approx(4900.0)


def test_a_butterfly_pairs_each_short_with_the_long_that_needs_the_least():
    fly = [leg("long", "call", 100, 6.0), leg("short", "call", 105, 3.0, 2), leg("long", "call", 110, 1.0)]
    v = margin_of(fly)
    # one short pairs with the long 100 (no requirement) and the other with the long 110 (5 points): 500; net debit (6 + 1 - 6) * 100 = 100
    assert v["requirement_usd"] == 500.0 and v["net_credit_usd"] == pytest.approx(-100.0) and v["margin_usd"] == pytest.approx(600.0)


def test_domain_problems_give_no_value_and_wrong_types_raise():
    assert_empty(call("options.strategy_margin", legs=[leg("short", "put", 95, 1.0)], multiplier=MULT), "spot is required")
    assert_empty(call("options.strategy_margin", legs=[leg("short", "put", 95, 1.0)], spot=100.0), "multiplier")           # no default multiplier
    assert_empty(call("options.strategy_margin", legs=[{"instrument": "stock", "position": "short", "quantity": 100, "premium": 100.0}], multiplier=MULT), "short stock")
    assert_empty(call("options.strategy_margin", legs=[leg("short", "put", 95, 1.0)], multiplier=MULT, spot=100.0, naked_pct=0.05, naked_floor_pct=0.1), "naked_pct")
    assert_empty(call("options.strategy_margin", legs=[leg("short", "put", 95, 1.0)], multiplier=MULT, spot=-1.0), "spot")
    with pytest.raises(BadInput):
        call("options.strategy_margin", legs="x", multiplier=MULT)
    with pytest.raises(BadInput):
        call("options.strategy_margin", legs=[leg("short", "put", 95, 1.0)], multiplier=MULT, spot=100.0, cash_secured="yes")


def test_the_function_is_registered_with_its_inputs_and_is_the_one_the_backtest_imports():
    spec = registry.describe("options.strategy_margin")
    assert spec.fn is margin.strategy_margin and spec.inputs["multiplier"]["default"] is None and spec.module == "options"
    parsed = margin.pay.parse_legs([leg("short", "put", 95, 2.0), leg("long", "put", 90, 0.8)], MULT)
    assert margin.group_margin(parsed, None)["margin_usd"] == pytest.approx(380.0)


def test_the_function_runs_in_a_worker_process_and_comes_back_whole(monkeypatch):
    monkeypatch.setattr(registry, "IN_PROCESS", False)
    res = registry.call("options.strategy_margin", legs=[leg("short", "put", 95, 2.0), leg("long", "put", 90, 0.8)], multiplier=MULT)
    assert res.value["margin_usd"] == pytest.approx(380.0) and res.piece_ids == ("L-21",)

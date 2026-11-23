"""The pieces of the trader in code (SPEC-019 A11): exact sizing (CTS-011), exits (DTS-04) and the budget (DTS-07a)."""
import inspect
from decimal import Decimal as D

import pytest

from hokma.desks.trading import budget, exits, sizing
from hokma.desks.trading.budget import BudgetInputs, pretrade_check, size_by_plan
from trader_support import book, inputs

# Contract sheets of the futures vectors: (multiplier, tick, value of one tick in USD), from the table verified against the exchange.
VECTORS = {"NQ": (20, "0.25", "5"), "ES": (50, "0.25", "12.5"), "CL": (1000, "0.01", "10"), "6E": (125000, "0.00005", "6.25"),
           "GC": (100, "0.10", "10")}


# ---- CTS-011 -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("root", list(VECTORS))
def test_CTS_011_one_tick_is_the_value_of_the_sheet_and_the_floor_is_exact(root):
    mult, tick, value = VECTORS[root]
    entry, stop = D("100000"), D("100000") - D(tick)
    seven = sizing.contracts(risk_budget_usd=D(value) * 7, entry=entry, stop=stop, multiplier=mult, tick_size=tick)
    assert (seven.status, seven.contracts, seven.risk_per_contract) == ("OK", 7, D(value))
    under = sizing.contracts(risk_budget_usd=D(value) * 7 - D("0.01"), entry=entry, stop=stop, multiplier=mult, tick_size=tick)
    assert under.contracts == 6


def test_CTS_011_the_floor_uses_rational_arithmetic_no_epsilon():
    # 0.3 over 0.1 is 2.9999999999999996 in binary floats: the exact floor is 3
    assert 0.3 / 0.1 < 3
    out = sizing.contracts(risk_budget_usd="0.3", entry="10", stop="9.9", multiplier=1)
    assert out.contracts == 3
    assert sizing.contracts(risk_budget_usd="0.29", entry="10", stop="9.9", multiplier=1).contracts == 2


def test_CTS_011_options_by_hand_with_the_multiplier_of_the_sheet():
    out = sizing.contracts(risk_budget_usd=500, entry="2.50", stop="1.25", multiplier=100, tick_size="0.05")
    assert (out.contracts, out.risk_per_contract, out.risk_usd) == (4, D(125), D(500))


def test_CTS_011_futures_by_hand():
    out = sizing.contracts(risk_budget_usd=1000, entry="21000", stop="20990", multiplier=20, tick_size="0.25")
    assert (out.contracts, out.risk_per_contract) == (5, D(200))


def test_CTS_011_no_trade_when_one_contract_does_not_fit_and_the_stop_never_moves():
    out = sizing.contracts(risk_budget_usd=99, entry="21000", stop="20995", multiplier=20, tick_size="0.25")
    assert out.status == "NO_TRADE" and out.contracts == 0 and out.code == "no_trade_budget"
    assert out.stop_distance == D(5) and out.risk_usd == 0
    assert not hasattr(out, "stop") and not hasattr(out, "new_stop")
    assert "stop" not in inspect.signature(size_by_plan).parameters


@pytest.mark.parametrize("kwargs, code", [
    (dict(entry="21000", stop="21000"), "stop_inside_tick"),
    (dict(entry="21000", stop="20999.9"), "off_grid"),
    (dict(entry="21000.1", stop="20990"), "off_grid"),
    (dict(entry="21000", stop="20990", multiplier=0), "bad_multiplier"),
    (dict(entry="21000", stop="0"), "bad_price"),
    (dict(entry="21000", stop="20990", risk_budget_usd=D("1e13")), "absurd_budget"),
    (dict(entry="21000", stop="20990", risk_budget_usd=-1), "absurd_budget"),
    (dict(entry="21000", stop="20990", risk_budget_usd=float("nan")), "bad_number"),
    (dict(entry="21000", stop="20990", currency="EUR"), "fx_missing"),
    (dict(entry="21000", stop="20990", currency="EUR", fx_rate=0), "bad_fx"),
    (dict(entry="21000", stop="20990", max_contracts=0), "bad_cap"),
    (dict(entry="21000", stop="20990", tick_size=0), "bad_tick"),
])
def test_CTS_011_unusable_inputs_are_refused_with_a_code(kwargs, code):
    args = dict(risk_budget_usd=1000, multiplier=20, tick_size="0.25")
    args.update(kwargs)
    with pytest.raises(sizing.SizingError) as exc:
        sizing.contracts(**args)
    assert exc.value.code == code


def test_CTS_011_a_bond_price_must_be_on_the_grid_of_thirty_seconds():
    tick = D(1) / 32
    assert sizing.parse_bond_price("110'16") == D("110.5")
    assert sizing.parse_bond_price("110'165") == D("110") + D("16.5") / 32
    assert sizing.parse_bond_price("110'16+") == sizing.parse_bond_price("110'165")
    with pytest.raises(sizing.SizingError):
        sizing.parse_bond_price("110'32")
    with pytest.raises(sizing.SizingError) as exc:                       # 110.16 is not 110'16
        sizing.contracts(risk_budget_usd=1000, entry="110.16", stop="110.0", multiplier=1000, tick_size=tick)
    assert exc.value.code == "off_grid"
    out = sizing.contracts(risk_budget_usd=1000, entry="110'16", stop="110'00", multiplier=1000, tick_size=tick)
    assert (out.contracts, out.risk_per_contract) == (2, D(500))


def test_CTS_011_a_foreign_currency_uses_the_rate_and_the_count_has_a_hard_cap():
    out = sizing.contracts(risk_budget_usd=1100, entry="1.1000", stop="1.0990", multiplier=125000, tick_size="0.00005", currency="EUR", fx_rate="1.1")
    assert out.risk_per_contract == D("0.0010") * 125000 * D("1.1") and out.contracts == 8
    big = sizing.contracts(risk_budget_usd=D("1e9"), entry="10", stop="9", multiplier=1)
    assert big.contracts == sizing.HARD_MAX_CONTRACTS and big.capped and big.code == "capped"
    assert sizing.contracts(risk_budget_usd=D("1e9"), entry="10", stop="9", multiplier=1, max_contracts=50).contracts == 50


def _leg(kind, side, strike):
    return sizing.OptionLegInfo(kind, side, D(strike))


def test_CTS_011_the_most_a_structure_can_lose_by_hand():
    f = sizing.max_loss_per_unit_usd
    assert f("debit_call_spread", [_leg("C", "buy", 200), _leg("C", "sell", 210)], D("3.40"), D(100)) == D(340)
    assert f("credit_put_spread", [_leg("P", "sell", 190), _leg("P", "buy", 185)], D("-1.50"), D(100)) == D(350)
    condor = [_leg("P", "buy", 170), _leg("P", "sell", 180), _leg("C", "sell", 220), _leg("C", "buy", 230)]
    assert f("iron_condor", condor, D("-2"), D(100)) == D(800)
    assert f("short_put", [_leg("P", "sell", 100)], D("-3"), D(100)) == D(9700)
    assert f("covered_call", [_leg("C", "sell", 210)], D("-2"), D(100), D(200)) == D(19800)
    assert f("short_strangle", [_leg("P", "sell", 180), _leg("C", "sell", 220)], D("-2"), D(100)) is None
    with pytest.raises(sizing.SizingError):
        f("debit_call_spread", [_leg("C", "buy", 200), _leg("C", "sell", 210)], D("-1"), D(100))
    with pytest.raises(sizing.SizingError):
        f("covered_call", [_leg("C", "sell", 210)], D("-2"), D(100))


# ---- DTS-04 -------------------------------------------------------------------------------------------------------

def test_DTS_04_day_trading_stop_in_ticks_and_target_in_r():
    long_ = exits.dt_exit("long", "21000", stop_ticks=40, tick="0.25")
    assert (long_.stop, long_.target, long_.risk_per_unit, long_.r_multiple, long_.basis) == (D("20990"), D("21020"), D(10), D(2), "ticks")
    short = exits.dt_exit("short", "21000", stop_ticks=40, tick="0.25", r_multiple=3)
    assert (short.stop, short.target) == (D("21010"), D("20970"))
    assert exits.stop_and_target("DT", side="long", entry="21000", stop_ticks=4, tick="0.25").stop == D("20999")


def test_DTS_04_swing_on_cfds_uses_the_atr_and_rounds_against_the_trader():
    plan = exits.swc_exit("long", "100.37", atr="1.23", atr_multiple=2, tick="0.05")
    assert plan.stop == D("97.90") and plan.risk_per_unit == D("2.47")               # 97.91 rounded away from the entry
    assert plan.target == D("105.25") and plan.reward_per_unit == D("4.88")           # 105.29 rounded toward the entry
    short = exits.swc_exit("short", "100.37", atr="1.23", atr_multiple=2, tick="0.05")
    assert short.stop == D("102.85") and short.target == D("95.45")
    assert plan.basis == "atr" and plan.variant == "SWC"


def test_DTS_04_options_stop_on_the_premium_for_a_debit_and_for_a_credit():
    debit = exits.swo_exit("debit", "2.35", tick="0.01")
    assert (debit.stop, debit.risk_per_unit, debit.target) == (D("1.17"), D("1.18"), D("4.70"))
    assert debit.uncalibrated == ("stop_fraction",)
    assert exits.swo_exit("debit", "2.35", tick="0.01", stop_fraction="0.4").uncalibrated == ()
    credit = exits.swo_exit("credit", "1.50", tick="0.01")
    assert (credit.stop, credit.target, credit.side) == (D("4.50"), D("0.75"), "short")
    assert set(credit.uncalibrated) == {"stop_multiple", "profit_fraction"}


def test_DTS_04_options_stop_on_the_underlying_uses_the_premium_the_options_module_priced():
    plan = exits.swo_exit("debit", "2.35", tick="0.01", basis="underlying", underlying_stop="188.5", premium_at_stop="1.40")
    assert (plan.stop, plan.underlying_stop, plan.basis) == (D("1.40"), D("188.5"), "underlying")
    for kw in (dict(underlying_stop="188.5"), dict(premium_at_stop="1.40"), dict(underlying_stop="188.5", premium_at_stop="2.50")):
        with pytest.raises(exits.ExitError) as exc:
            exits.swo_exit("debit", "2.35", tick="0.01", basis="underlying", **kw)
        assert exc.value.code == "bad_stop"


@pytest.mark.parametrize("call, code", [
    (lambda: exits.swo_exit("debit", "0.05", tick="0.05"), "stop_not_positive"),
    (lambda: exits.dt_exit("long", "21000", stop_ticks=0, tick="0.25"), "bad_stop"),
    (lambda: exits.dt_exit("long", "21000", stop_ticks=4, tick="0"), "bad_tick"),
    (lambda: exits.dt_exit("up", "21000", stop_ticks=4, tick="0.25"), "bad_side"),
    (lambda: exits.swc_exit("long", "100", atr="0", tick="0.01"), "bad_stop"),
    (lambda: exits.swo_exit("debit", "2.35", r_multiple=0), "bad_r"),
    (lambda: exits.swo_exit("debit", "2.35", stop_fraction="1"), "bad_stop"),
    (lambda: exits.stop_and_target("XX"), "bad_variant"),
    (lambda: exits.swo_exit("both", "2.35"), "bad_side"),
])
def test_DTS_04_a_bad_exit_is_refused_with_a_code(call, code):
    with pytest.raises(exits.ExitError) as exc:
        call()
    assert exc.value.code == code


# ---- DTS-07a ------------------------------------------------------------------------------------------------------

def _size(inp, rpc="250", premium="300", cash="400"):
    return size_by_plan(inp, risk_per_contract_usd=D(rpc), premium_per_contract_usd=D(premium), cash_per_contract_usd=D(cash))


def test_DTS_07a_the_size_is_the_minimum_of_the_four_limits():
    inp = inputs(max_premium_usd=D(2000), committee_max_contracts=3)
    out = _size(inp)
    assert out.limits == {"risk_per_trade": 4, "max_premium": 6, "committee": 3, "cash": 250}
    assert (out.status, out.contracts, out.binding) == ("OK", 3, "committee")
    assert _size(inputs()).contracts == 4 and _size(inputs()).binding == "risk_per_trade"
    assert _size(inputs(max_premium_usd=D(1000))).contracts == 3 and _size(inputs(max_premium_usd=D(1000))).binding == "max_premium"
    assert _size(inputs(cash=D(900))).contracts == 2 and _size(inputs(cash=D(900))).binding == "cash"
    assert _size(inputs(max_position_pct=D("1"))).limits["committee"] == 2                    # 1% of 100000 over 400
    assert _size(inputs(max_order_pct=D("0.5"))).contracts == 1


def test_DTS_07a_if_it_does_not_fit_it_is_refused_before_any_stop_is_narrowed():
    out = _size(inputs(), rpc="2000")
    assert (out.status, out.contracts, out.binding, out.code) == ("NO_TRADE", 0, "risk_per_trade", "no_room_risk_per_trade")
    assert "stop is not narrowed" in out.reason
    assert "stop" not in inspect.signature(size_by_plan).parameters and not hasattr(out, "stop")
    assert _size(inputs(cash=D(100))).code == "no_room_cash"


def test_DTS_07a_drawdown_from_the_peak_is_counted_and_a_reached_limit_stops_the_order():
    assert _size(inputs(equity=D(95_000), max_drawdown_pct=D(10)), rpc="250").limits["drawdown"] == 20             # 10000 - 5000 = 5000 room
    out = _size(inputs(equity=D(90_000), max_drawdown_pct=D(10)))
    assert out.status == "NO_TRADE" and out.binding == "drawdown"
    assert [b.code for b in pretrade_check(inputs(equity=D(90_000), max_drawdown_pct=D(10)))] == ["max_drawdown_pct"]


def test_DTS_07a_capital_floor_and_funded_account_rules():
    assert _size(inputs(equity=D(50_500), cash=D(50_500), capital_floor_usd=D(50_000))).limits["capital_floor"] == 2
    assert _size(inputs(capital_floor_usd=D(100_000))).binding == "capital_floor"
    funded = inputs(funding={"daily_loss_usd": 1000, "trailing_drawdown_usd": 2500, "max_contracts": 5}, day_pnl=D(-500), open_contracts=D(3))
    out = _size(funded)
    assert out.limits["funding_daily_loss"] == 2 and out.limits["funding_trailing"] == 10 and out.limits["funding_max_contracts"] == 2
    assert (out.contracts, out.binding) == (2, "funding_daily_loss")
    assert [b.code for b in pretrade_check(inputs(funding={"daily_loss_usd": 1000}, day_pnl=D(-1000)))] == ["daily_loss"]
    assert [b.code for b in pretrade_check(inputs(funding={"trailing_drawdown_usd": 1000}, equity=D(99_000)))] == ["trailing_drawdown"]


def test_DTS_07a_a_limit_that_is_set_and_whose_fact_is_unknown_fails_closed():
    unknown = inputs(max_drawdown_pct=D(10), peak_equity=None)
    assert _size(unknown).limits["drawdown"] == 0 and [b.code for b in pretrade_check(unknown)] == ["drawdown_unknown"]
    assert _size(inputs(cash=None)).limits["cash"] == 0
    assert _size(inputs(equity=None, capital=D(0))).limits["risk_per_trade"] == 0
    assert _size(inputs(funding={"daily_loss_usd": 100}, day_pnl=None)).limits["funding_daily_loss"] == 0
    assert _size(inputs(funding={"max_contracts": 4}, open_contracts=None)).limits["funding_max_contracts"] == 0


def test_DTS_07a_the_pretrade_check_stops_an_inactive_desk_switched_off_orders_and_no_account():
    codes = [b.code for b in pretrade_check(inputs(desk_status="paused", orders_enabled=False, account_ok=False))]
    assert codes == ["desk_inactive", "orders_off", "no_account"]
    assert pretrade_check(inputs()) == ()
    assert [b.code for b in pretrade_check(inputs(equity=D(40_000), capital_floor_usd=D(50_000)))] == ["capital_floor"]


def test_DTS_07a_the_inputs_come_from_the_book_and_the_limits_of_the_desk():
    inp = BudgetInputs.from_book(book(), {"max_premium_usd": 2500, "max_drawdown_pct": 10, "funding": {"max_contracts": 4}}, capital=100_000,
                                 risk_pct="0.5", committee_max_contracts=3)
    assert (inp.equity, inp.max_premium_usd, inp.max_drawdown_pct, inp.risk_pct, inp.committee_max_contracts) == (D(100_000), D(2500), D(10), D("0.5"), 3)
    out = _size(inp)
    assert out.risk_budget_usd == D(500) and out.limits["risk_per_trade"] == 2


def test_DTS_07a_bad_unit_amounts_are_refused():
    for kw in (dict(rpc="0"), dict(cash="-1")):
        with pytest.raises(ValueError):
            _size(inputs(), **kw)
    with pytest.raises(ValueError):
        size_by_plan(inputs(), risk_per_contract_usd=D(10), premium_per_contract_usd=D(-1), cash_per_contract_usd=D(10))

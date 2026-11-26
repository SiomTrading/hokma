"""Costs (SPEC-020 B-13, B-42, V-08, L-14, L-15, N-10, N-28, P-07). Every expected value is computed by hand in its comment.

Commission rates: 2.25 USD per side per contract for a future (the table of schwab), 0.65 per contract for an option."""
import ast
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from backtest.support import MULT, TICK, expected, requires_engine, scenario_run
from hokma.backtest import costs
from hokma.backtest.fills import against
from hokma.backtest.spec import CostModel, SpecError
from hokma.broker import costs as broker_costs
from hokma.broker import fees
from hokma.db.errors import ValidationError
from hokma.exec import tca

COSTS_SOURCE = Path(costs.__file__)
FUTURE = costs.fee_model(CostModel("schwab"), "future")


def per_contract(rate: str, qty: str) -> D:
    """What a per contract rule must charge, by hand: the rate times the contracts. The trap is charging the rate once per order."""
    return D(rate) * D(qty)


def assert_charges_per_contract(spec_: "costs.FeeSpec", qty: str, rate: str) -> None:
    got = costs.commission_usd(spec_, qty=D(qty), price=D("25000"), multiplier=MULT)
    assert got == per_contract(rate, qty), f"{qty} contracts at {rate} per contract must pay {per_contract(rate, qty)}, not {got}"


# --- V-08: the trap of the commission per order ----------------------------------------------------------------------------------

def test_two_contracts_pay_4_50_at_2_25_per_contract_and_not_2_25():
    assert_charges_per_contract(FUTURE, "2", "2.25")
    assert costs.commission_usd(FUTURE, qty=D(2), price=D("25000"), multiplier=MULT) == D("4.50") != D("2.25")
    assert costs.commission_usd(FUTURE, qty=D(1), price=D("25000"), multiplier=MULT) == D("2.25")
    assert costs.commission_usd(FUTURE, qty=D(0), price=D("25000"), multiplier=MULT) == D(0)


def test_the_per_contract_check_fails_for_a_commission_per_order():
    per_order = costs.FeeSpec(fees.FeeTable("trap", "USD", "a rate charged once per order", {"future": fees.FeeRule("per_order", D("2.25"))}), "future")
    with pytest.raises(AssertionError):
        assert_charges_per_contract(per_order, "2", "2.25")


# --- B-42: commission by contract, order, bps and minimum; the cost on both legs ---------------------------------------------------------

def table(kind: str, value: str, minimum: str = "0", cap_bps: str | None = None, asset: str = "future") -> "costs.FeeSpec":
    rule = fees.FeeRule(kind, D(value), D(minimum), None if cap_bps is None else D(cap_bps))
    return costs.FeeSpec(fees.FeeTable("t", "USD", "test table", {asset: rule}), asset)


def test_commission_by_order_is_charged_once_whatever_the_quantity():
    assert costs.commission_usd(table("per_order", "5"), qty=D(10), price=D(100), multiplier=D(1)) == D(5)       # 10 contracts, one order: 5
    assert costs.commission_usd(table("per_order", "5"), qty=D(0), price=D(100), multiplier=D(1)) == D(0)


def test_commission_in_bps_equals_the_function_of_the_execution_service_and_is_by_hand_1_dollar():
    spec_ = table("bps", "1")
    got = costs.commission_usd(spec_, qty=D(100), price=D(100), multiplier=D(1))
    assert got == D("1.00")                                                    # 100 * 100 = 10000 of notional, 1 bp of it is 1.00
    assert got == D(str(broker_costs.commission(10_000.0, 100.0, {"type": "bps", "value": 1.0, "min": 0.0})))
    assert costs.commission_usd(spec_, qty=D(10), price=D("123.456"), multiplier=D(1)) == D(str(broker_costs.commission(1234.56, 10.0, {"type": "bps", "value": 1.0, "min": 0.0})))


def test_commission_minimum_applies_per_order_and_not_when_nothing_trades():
    spec_ = table("bps", "1", minimum="1")
    assert costs.commission_usd(spec_, qty=D(1), price=D(100), multiplier=D(1)) == D("1.00")                    # 0.01 by bps, raised to the minimum
    assert costs.commission_usd(spec_, qty=D(0), price=D(100), multiplier=D(1)) == D(0)
    assert costs.commission_usd(table("per_contract", "0.65", minimum="1", asset="option"), qty=D(1), price=D("2"), multiplier=D(100)) == D("1.00")


def test_the_ticks_against_are_paid_on_both_legs_and_never_only_on_the_entry():
    """T1 with no commission: the open to open move is 5.75 points = 115.00 USD; one tick on each leg is 0.50 points = 10.00 USD,
    so 105.00. With the tick on the entry leg only it would be 110.00."""
    zero = CostModel("zero")
    trip = costs.round_trip("long", entry_ref=D("25637.25"), exit_ref=D("25643.00"), qty=D(1), tick_size=TICK, multiplier=MULT, model=zero)
    assert (trip.entry_price, trip.exit_price) == (D("25637.50"), D("25642.75"))
    assert trip.gross_usd == D("105.00") != D("110.00") and trip.commission_usd == D(0)
    flat = costs.round_trip("long", entry_ref=D("25637.25"), exit_ref=D("25643.00"), qty=D(1), tick_size=TICK, multiplier=MULT,
                            model=CostModel("zero", slippage_ticks=0))
    assert flat.gross_usd == D("115.00")


# --- B-13: T1 with the whole model, and no second formula in the file -----------------------------------------------------------------

def test_t1_with_2_25_per_side_per_contract_and_one_tick_per_side_is_100_50():
    trip = costs.round_trip("long", entry_ref=D("25637.25"), exit_ref=D("25643.00"), qty=D(1), tick_size=TICK, multiplier=MULT, model=CostModel("schwab"))
    assert (trip.gross_usd, trip.commission_usd, trip.net_usd) == (D("105.00"), D("4.50"), D("100.50")) and trip.net_usd == D(expected("T1")["pnl_usd"])
    short = costs.round_trip("short", entry_ref=D("100"), exit_ref=D("99"), qty=D(2), tick_size=D("0.25"), multiplier=D(1), model=CostModel("zero"))
    assert (short.entry_price, short.exit_price, short.gross_usd) == (D("99.75"), D("99.25"), D("1.00"))     # (99.75 - 99.25) * 2


@requires_engine
def test_the_engine_charges_the_same_commission_as_the_table():
    out, _ = scenario_run("T1")
    assert [f.commission for f in out.fills] == [costs.commission_usd(FUTURE, qty=f.qty, price=f.price, multiplier=MULT) for f in out.fills]
    bps_table = fees.FeeTable("t", "USD", "bps table", {"future": fees.FeeRule("bps", D("1"))})
    assert costs.FeeSpec(bps_table, "future").engine_rate() is None and FUTURE.engine_rate() == D("2.25")


def test_the_cost_file_has_no_formula_of_basis_points_of_its_own():
    tree = ast.parse(COSTS_SOURCE.read_text(encoding="utf-8"))
    constants = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool)}
    assert not constants & {10_000, 10_000.0, 100, 100.0}, "a conversion of basis points to a fraction belongs to hokma.broker.costs"
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
            assert not any("bps" in name for name in names), f"line {node.lineno}: a division of a basis point amount"
    assert "def commission(" not in COSTS_SOURCE.read_text(encoding="utf-8") and "def fill_price(" not in COSTS_SOURCE.read_text(encoding="utf-8")
    assert costs.CostModel is CostModel


# --- L-14: the ticks mode of the one slippage function ------------------------------------------------------------------------------

def test_fill_price_in_ticks_for_futures_and_the_bps_mode_is_unchanged():
    assert broker_costs.fill_price("buy", 25551.75, ticks=1, tick=0.25) == 25552.00
    assert broker_costs.fill_price("sell", 25643.00, ticks=1, tick=0.25) == 25642.75
    assert broker_costs.fill_price("buy", 100.0, ticks=0, tick=0.25) == 100.0
    assert broker_costs.fill_price("buy", 100.0, 2.0) == 100.02 and broker_costs.fill_price("sell", 100.0, 2.0) == 99.98
    assert against("buy", D("25551.75"), 1, TICK) == D("25552.00")
    for bad in (dict(slippage_bps=1.0, ticks=1, tick=0.25), dict(), dict(ticks=1), dict(ticks=1, tick=0), dict(ticks=-1, tick=0.25), dict(ticks=True, tick=0.25)):
        with pytest.raises(ValidationError):
            broker_costs.fill_price("buy", 100.0, **bad)


# --- L-15: volume share and impact ---------------------------------------------------------------------------------------------------

def test_slippage_by_volume_share_at_10_percent_is_10_basis_points():
    """100 contracts in a bar of 1000: share 0.1, coefficient 1000 bp * 0.1 squared = 10 bp of 25000 = 25.00 against the trader."""
    assert costs.volume_share("buy", D("25000"), D(100), D(1000)) == D("25025.0")
    assert costs.volume_share("sell", D("25000"), D(100), D(1000)) == D("24975.0")
    assert costs.volume_share("buy", D("25000"), D(50), D(1000)) == D("25006.25")           # share 0.05: 1000 * 0.0025 = 2.5 bp
    assert costs.volume_share("buy", D("25000"), D(100), D(1000)) > costs.volume_share("buy", D("25000"), D(10), D(1000))
    with pytest.raises(SpecError):
        costs.volume_share("buy", D("25000"), D(1001), D(1000))
    with pytest.raises(SpecError):
        costs.volume_share("buy", D("25000"), D(1), D(0))


def test_market_impact_is_the_model_of_the_cost_analysis_of_the_execution_service():
    """Volatility 100 bp a day, 1 contract of a daily volume of 100: 100 * sqrt(1 / 100) = 10 bp of 25000 = 25.00."""
    bps = tca.sqrt_impact_bps(daily_volatility_bps=D(100), qty=D(1), daily_volume=D(100))
    assert bps == D(10)
    assert costs.market_impact("buy", D("25000"), D(1), D(100), D(100)) == D("25025.0")
    assert costs.market_impact("sell", D("25000"), D(1), D(100), D(100)) == D(repr(broker_costs.fill_price("sell", 25000.0, float(bps))))
    assert costs.market_impact("buy", D("25000"), D(4), D(100), D(100), D(2)) == D(repr(broker_costs.fill_price(
        "buy", 25000.0, float(tca.sqrt_impact_bps(daily_volatility_bps=D(100), qty=D(4), daily_volume=D(100), coefficient=D(2))))))
    assert "sqrt(" not in COSTS_SOURCE.read_text(encoding="utf-8").replace("sqrt_impact_bps", "")


def test_the_impact_of_the_cost_model_is_chosen_by_its_name_and_never_defaulted():
    price = D("25000")
    assert costs.apply_impact(CostModel("zero"), "buy", price, D(1)) == price
    assert costs.apply_impact(CostModel("zero", impact="volume_share"), "buy", price, D(100), bar_volume=D(1000)) == D("25025.0")
    assert costs.apply_impact(CostModel("zero", impact="sqrt"), "buy", price, D(1), daily_volume=D(100), daily_volatility_bps=D(100)) == D("25025.0")
    with pytest.raises(SpecError):
        costs.apply_impact(CostModel("zero", impact="volume_share"), "buy", price, D(1))
    with pytest.raises(SpecError):
        costs.apply_impact(CostModel("zero", impact="sqrt"), "buy", price, D(1), daily_volume=D(100))


@requires_engine
def test_the_impact_is_charged_on_the_fills_of_a_run():
    plain, _ = scenario_run("T1")
    with_impact, _ = scenario_run("T1", costs=CostModel("schwab", impact="volume_share"))
    assert plain.costs_usd["impact"] == D(0) and with_impact.costs_usd["impact"] > D(0)
    assert with_impact.realized_pnl_usd == plain.realized_pnl_usd - with_impact.costs_usd["impact"]


# --- N-10: the fee tables ------------------------------------------------------------------------------------------------------------

def test_t1_pays_4_50_per_operation_and_an_spy_option_pays_the_table_of_schwab():
    assert costs.commission_usd(FUTURE, qty=D(1), price=D("25637.50"), multiplier=MULT) * 2 == D("4.50")
    option = costs.fee_model(CostModel("schwab"), "option")
    assert costs.commission_usd(option, qty=D(10), price=D("1.00"), multiplier=D(100)) == D("6.50")        # 10 contracts * 0.65
    assert costs.commission_usd(costs.fee_model(CostModel("schwab"), "equity"), qty=D(100), price=D(500), multiplier=D(1)) == D(0)


def test_the_cap_of_an_option_table_limits_the_commission_to_a_share_of_the_traded_value():
    """1 contract at 0.20 of premium, multiplier 100: 20.00 traded. 0.65 raised to the minimum 1.00, capped at 100 bp of 20.00 = 0.20."""
    capped = table("per_contract", "0.65", minimum="1", cap_bps="100", asset="option")
    assert costs.commission_usd(capped, qty=D(1), price=D("0.20"), multiplier=D(100)) == D("0.20")
    assert costs.commission_usd(capped, qty=D(10), price=D("5.00"), multiplier=D(100)) == D("6.50")         # 6.50 against a cap of 50.00


def test_the_fee_tables_load_by_name_only():
    assert {"schwab", "zero"} <= set(fees.tables())
    assert fees.load("schwab") is fees.load("schwab.json") and fees.load("schwab").rules["future"].value == D("2.25")
    for bad in ("../schwab", "schwab/../zero", "/etc/passwd", "Schwab", "", "nope", "a" * 60):
        with pytest.raises(ValidationError):
            fees.load(bad)
    with pytest.raises(SpecError):
        costs.fee_model(CostModel("nope"))
    with pytest.raises(SpecError):
        costs.fee_model(CostModel("schwab"), "crypto")


# --- N-28: the swap of a CFD ---------------------------------------------------------------------------------------------------------

def test_a_long_cfd_held_three_nights_pays_three_times_the_daily_rate_on_the_notional():
    rates = fees.SwapRates(long_daily=D("0.0001"), short_daily=D("-0.00005"))
    nights = [date(2025, 11, 3), date(2025, 11, 4), date(2025, 11, 6)]                               # Monday, Tuesday, Thursday
    assert costs.cfd_swap("long", D("100000"), nights, rates) == D("30.0000")                       # 3 * 0.0001 * 100000
    assert costs.cfd_swap("short", D("100000"), nights, rates) == D("-15.00000")                    # a credit: 3 * -0.00005 * 100000


def test_the_triple_swap_day_pays_three_nights_when_the_table_says_so():
    nights = [date(2025, 11, 4), date(2025, 11, 5), date(2025, 11, 6)]                              # Tuesday, Wednesday, Thursday
    triple = fees.SwapRates(D("0.0001"), D("0.0001"), triple_weekday=2)
    assert costs.cfd_swap("long", D("100000"), nights, triple) == D("50.0000")                      # (1 + 3 + 1) * 0.0001 * 100000
    assert costs.cfd_swap("long", D("100000"), nights, fees.SwapRates(D("0.0001"), D("0.0001"))) == D("30.0000")
    assert costs.cfd_swap("long", D("100000"), [], triple) == D(0)
    with pytest.raises(SpecError):
        costs.cfd_swap("long", D("100000"), nights, None)


# --- P-07: the cost in units of Sharpe -----------------------------------------------------------------------------------------------

def test_the_yearly_cost_in_sharpe_is_turnover_times_cost_over_volatility():
    """100 round trips a year at 0.0002 of capital each = 0.02 a year; over a volatility of 0.10 that is 0.20 of Sharpe."""
    dead = costs.cost_in_sharpe(round_trips_per_year=100, cost_per_round_trip=0.0002, annual_volatility=0.10, gross_sharpe=0.15)
    assert dead.cost_sharpe == pytest.approx(0.20) and dead.net_sharpe == pytest.approx(-0.05) and dead.warnings == ("costs_kill_edge",)
    alive = costs.cost_in_sharpe(round_trips_per_year=100, cost_per_round_trip=0.0002, annual_volatility=0.10, gross_sharpe=0.50)
    assert alive.net_sharpe == pytest.approx(0.30) and alive.warnings == ()
    assert costs.cost_in_sharpe(round_trips_per_year=50, cost_per_round_trip=0.0002, annual_volatility=0.10).net_sharpe is None
    for bad in (dict(annual_volatility=0), dict(round_trips_per_year=-1), dict(cost_per_round_trip=-0.1)):
        with pytest.raises(SpecError):
            costs.cost_in_sharpe(**{"round_trips_per_year": 1, "cost_per_round_trip": 0.1, "annual_volatility": 0.1, **bad})

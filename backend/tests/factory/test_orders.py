"""Orders of the factory (SPEC-020: B-04, B-08, B-09, V-09, V-13, V-14), expected values computed by hand."""
from dataclasses import FrozenInstanceError
from decimal import Decimal as D

import pytest

from hokma.factory.orders import (ATR, BadOrder, BadSize, BadUnit, BracketError, LoosenStop, Lot, OrderIntent, Pct, Price, Protection, Size,
                                  SizeType, Ticks, adjust_protection, bracket, buy, intent_quantity, protective_price, reduce, reduce_intent,
                                  sell, stop_units, to_contracts)
from tests.factory.support import nq, stock


def test_buy_with_bracket_and_tag():                                            # B-04
    intent = buy("FUT:NQ:2025-03", 2, sl=Ticks(20), tp=Ticks(60), tag="orb")
    assert (intent.side, intent.size, intent.order_type, intent.tag) == ("buy", Size(SizeType.CONTRACTS, D(2)), "market", "orb")
    assert intent.sl == Ticks(20) and intent.tp == Ticks(60) and intent.has_bracket and not intent.reduce_only
    with pytest.raises(FrozenInstanceError):
        intent.tag = "x"  # type: ignore[misc]
    assert sell("FUT:NQ:2025-03", 1).side == "sell"


def test_order_type_follows_limit_and_stop():
    assert buy("A", 1, limit=100).order_type == "limit"
    assert buy("A", 1, stop=100).order_type == "stop"
    both = buy("A", 1, limit=100, stop=101)
    assert both.order_type == "stop_limit" and both.limit == D("100") and both.stop == D("101")
    with pytest.raises(BadOrder):
        OrderIntent("A", "buy", Size(SizeType.CONTRACTS, 1), "limit")


def test_dt_fraction_fails_and_lp_size_is_a_weight():
    with pytest.raises(BadSize, match="whole contracts"):
        buy("FUT:NQ:2025-03", 1.5)
    with pytest.raises(BadSize):
        sell("CFD:EURUSD", 0.5, line="SWC")
    assert buy("EQ:SPY", 0.25, line="LP").size == Size(SizeType.TARGET_WEIGHT, D("0.25"))
    with pytest.raises(BadSize):
        buy("EQ:SPY", 1.5, line="LP")                                            # a weight above the whole capital


def test_a_stop_without_unit_and_bad_tags_fail():
    with pytest.raises(BadUnit, match="explicit unit"):
        buy("A", 1, sl=5)
    with pytest.raises(BadOrder):
        buy("A", 1, tag="has space")
    with pytest.raises(BadOrder):
        OrderIntent("A", "sell", Size(SizeType.CONTRACTS, 1), sl=Ticks(4), reduce_only=True)
    with pytest.raises(BadSize):
        buy("A", True)


def test_reduce_intent_closes_the_other_way():
    intent = reduce_intent("A", "buy", 1, tag="exit")
    assert intent.side == "sell" and intent.reduce_only and intent.size.value == D(1)


# ---- V-13 ----

def test_stop_units_in_points():
    sheet = nq()
    assert stop_units(Ticks(20), entry=D("20000"), sheet=sheet) == D("5.00")           # 20 ticks of 0.25
    assert stop_units(Pct(1), entry=D("20000"), sheet=sheet) == D("200")               # 1 percent of the entry
    assert stop_units(ATR(2), entry=D("20000"), sheet=sheet, atr=D("12.5")) == D("25.0")
    assert stop_units(Price(19900), entry=D("20000"), sheet=sheet) == D("100")
    with pytest.raises(BadUnit):
        stop_units(20, entry=D("20000"), sheet=sheet)                                   # a number has no unit
    with pytest.raises(BadUnit):
        stop_units(ATR(2), entry=D("20000"), sheet=sheet)                               # no ATR value supplied
    with pytest.raises(BadUnit):
        stop_units(Ticks(4), entry=D("100"), sheet=stock())                             # a stock has no tick size
    with pytest.raises(BadUnit):
        Pct(100)
    with pytest.raises(BadUnit):
        Ticks(0)


def test_protective_prices_by_side_and_tick_grid():
    sheet = nq()
    assert protective_price(Pct(1), side="buy", kind="sl", entry=D("20000"), sheet=sheet) == D("19800")
    assert protective_price(Pct(1), side="buy", kind="tp", entry=D("20000"), sheet=sheet) == D("20200")
    assert protective_price(Pct(1), side="sell", kind="sl", entry=D("20000"), sheet=sheet) == D("20200")
    assert protective_price(Ticks(20), side="sell", kind="tp", entry=D("20000"), sheet=sheet) == D("19995")
    # 100.10 minus 0.30 is 99.80: a long stop rounds away from the entry (99.75), a long target toward it (100.25 from 100.40)
    assert protective_price(ATR(1), side="buy", kind="sl", entry=D("100.10"), sheet=sheet, atr=D("0.30")) == D("99.75")
    assert protective_price(ATR(1), side="buy", kind="tp", entry=D("100.10"), sheet=sheet, atr=D("0.30")) == D("100.25")
    assert protective_price(ATR(1), side="sell", kind="sl", entry=D("100.10"), sheet=sheet, atr=D("0.30")) == D("100.50")
    assert protective_price(ATR(1), side="sell", kind="tp", entry=D("100.10"), sheet=sheet, atr=D("0.30")) == D("100.00")
    with pytest.raises(BadUnit, match="wrong side"):
        protective_price(Price(20100), side="buy", kind="sl", entry=D("20000"), sheet=sheet)


# ---- V-09 ----

def test_every_size_type_converts_with_the_multiplier_and_rounds_down():
    sheet = nq()
    common = dict(sheet=sheet, price=D("20000"), equity=D("1000000"))                   # one contract is 20 * 20000 = 400000
    assert to_contracts(Size(SizeType.CONTRACTS, 3), **common) == (D(3), ())
    assert to_contracts(Size(SizeType.NOTIONAL, 1_000_000), **common) == (D(2), ("size_rounded_down:2.5->2",))
    assert to_contracts(Size(SizeType.PCT_EQUITY, 80), **common) == (D(2), ())          # 800000 / 400000
    assert to_contracts(Size(SizeType.TARGET_WEIGHT, "0.4"), **common) == (D(1), ())
    assert to_contracts(Size(SizeType.RISK_USD, 1000), **common, stop_distance=D("5")) == (D(10), ())   # 1000 / (5 * 20)
    qty, warnings = to_contracts(Size(SizeType.PCT_EQUITY, 20), **common)               # 200000 / 400000 = 0.5
    assert qty == D(0) and warnings == ("size_rounded_down:0.5->0",)


def test_size_rules_and_unknown_type():
    with pytest.raises(BadSize, match="unknown size type"):
        SizeType.parse("lots")
    with pytest.raises(BadSize):
        Size("lots", 1)                                                                  # type: ignore[arg-type]
    with pytest.raises(BadSize, match="stop distance"):
        to_contracts(Size(SizeType.RISK_USD, 100), sheet=nq(), price=D("20000"), equity=D("1000000"))
    with pytest.raises(BadSize, match="whole contracts"):
        to_contracts(Size(SizeType.CONTRACTS, "1.5"), sheet=nq(), price=D("20000"), equity=D("1000000"))
    with pytest.raises(BadSize):
        Size(SizeType.PCT_EQUITY, 101)
    qty, _ = to_contracts(Size(SizeType.TARGET_WEIGHT, "0.25"), sheet=stock(), price=D("400"), equity=D("100000"), mode="fractional")
    assert qty == D("62.5")                                                              # 25000 / 400, the fraction is kept in LP


def test_quantity_of_an_intent_uses_its_stop_distance():
    intent = buy("FUT:NQ:2025-03", Size(SizeType.RISK_USD, 500), sl=ATR(2))
    assert intent_quantity(intent, sheet=nq(), price=D("20000"), equity=D("1000000"), atr=D("5")) == (D(2), ("size_rounded_down:2.5->2",))


# ---- B-09 ----

def test_reduce_closes_first_in_first_out():
    lots = [Lot(D(2), D("100")), Lot(D(1), D("102"))]
    result = reduce(lots, 1, exit_price=D("105"), side_held="buy", multiplier=D("20"))
    assert result.closed == (Lot(D(1), D("100")),)
    assert result.remaining == (Lot(D(1), D("100")), Lot(D(1), D("102")))
    assert result.realized_pnl == D("100")                                               # (105 - 100) * 1 * 20
    both = reduce(lots, 2, exit_price=D("105"), side_held="buy", multiplier=D("20"))
    assert both.remaining == (Lot(D(1), D("102")),) and both.realized_pnl == D("200")
    across = reduce(lots, 3, exit_price=D("105"), side_held="buy", multiplier=D("20"))
    assert across.remaining == () and across.realized_pnl == D("260")                    # (5 * 2 + 3 * 1) * 20


def test_reduce_short_and_too_much():
    short = reduce([Lot(D(3), D("100"))], 1, exit_price=D("98"), side_held="sell", multiplier=D("20"))
    assert short.realized_pnl == D("40") and short.remaining == (Lot(D(2), D("100")),)
    with pytest.raises(BadSize, match="holds 3"):
        reduce([Lot(D(3), D("100"))], 4, exit_price=D("98"), side_held="sell", multiplier=D("20"))
    with pytest.raises(BadSize):
        reduce([Lot(D(3), D("100"))], 0, exit_price=D("98"), side_held="sell", multiplier=D("20"))


# ---- V-14 ----

def test_adjust_protection_tightens_and_never_loosens():
    start = Protection(D("95"), D("110"))
    assert adjust_protection("buy", start, new_sl=100) == Protection(D("100"), D("110"))      # break even
    assert adjust_protection("buy", start, new_tp=120).tp == D("120")
    with pytest.raises(LoosenStop) as caught:
        adjust_protection("buy", start, new_sl=94)
    assert caught.value.code == "loosen_stop"
    short = Protection(D("105"), D("90"))
    assert adjust_protection("sell", short, new_sl=100).sl == D("100")
    with pytest.raises(LoosenStop):
        adjust_protection("sell", short, new_sl=106)
    with pytest.raises(BadOrder):
        adjust_protection("buy", start)
    with pytest.raises(BadOrder, match="profit side"):
        adjust_protection("buy", start, new_tp=96, new_sl=97)


# ---- B-08 ----

def entry_with_bracket():
    return buy("FUT:NQ:2025-03", 1, sl=Ticks(20), tp=Ticks(60), tag="orb")


def test_children_exist_only_after_the_entry_fills():
    parent = bracket(entry_with_bracket(), sheet=nq())
    assert parent.state == "pending" and parent.children == ()
    parent.cancel_entry()
    assert parent.state == "cancelled" and parent.children == ()                          # no entry fill: no children
    with pytest.raises(BracketError):
        parent.fill_entry(D("20000"), D(1))


def test_children_are_built_from_the_fill_price_and_one_cancels_the_other():
    parent = bracket(entry_with_bracket(), sheet=nq())
    stop, target = parent.fill_entry(D("20000.25"), D(1))
    assert (stop.kind, stop.side, stop.price, stop.qty) == ("stop", "sell", D("19995.25"), D(1))
    assert (target.kind, target.price, target.group) == ("target", D("20015.25"), stop.group)
    done = parent.fill_child("stop")
    assert done.filled == stop and done.cancelled == target and parent.state == "closed"
    with pytest.raises(BracketError):
        parent.fill_child("target")                                                       # the target is gone

    other = bracket(entry_with_bracket(), sheet=nq())
    other.fill_entry(D("20000"), D(1))
    done = other.fill_child("target")
    assert done.filled.kind == "target" and done.cancelled.kind == "stop"


def test_modify_moves_the_child_and_refuses_to_loosen():
    parent = bracket(entry_with_bracket(), sheet=nq())
    parent.fill_entry(D("20000"), D(1))                                                   # stop 19995, target 20015
    changed = parent.modify(new_sl=D("20000"))                                            # break even
    assert [c.price for c in changed] == [D("20000")] and parent.protection() == Protection(D("20000"), D("20015"))
    with pytest.raises(LoosenStop):
        parent.modify(new_sl=D("19990"))
    assert parent.modify(new_sl=D("20000")) == ()                                         # no change, no child touched


def test_a_bracket_needs_protection_and_a_stop_alone_is_valid():
    with pytest.raises(BadOrder):
        bracket(buy("A", 1), sheet=nq())
    only = bracket(buy("FUT:NQ:2025-03", 1, sl=Pct(1)), sheet=nq())
    assert len(only.fill_entry(D("20000"), D(1))) == 1
    assert only.fill_child("stop").cancelled is None

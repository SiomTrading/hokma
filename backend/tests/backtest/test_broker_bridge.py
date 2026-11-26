"""The backtest takes the proposals of live execution (SPEC-020: L-59, N-14, N-16): what a strategy emits becomes the proposal H2 executes,
with every control field derived from real state, and the engine fills that same proposal with the fill rules and the cost tables."""
import inspect
from decimal import Decimal as D

import pytest
from backtest.support import MULT, TICK, bars_of, expected, plan, requires_engine, spec

from hokma.backtest import broker_bridge, costs, engine, fills
from hokma.backtest.broker_bridge import BridgeError, order_types, supported_types, to_decision, to_engine_order, to_proposal
from hokma.backtest.fills import Decision
from hokma.contracts import identity
from hokma.factory.orders import OrderIntent, Pct, Price, Size, SizeType, Ticks, buy, sell

NQ = "FUT:NQ:2026-12"
SHEET = identity.resolve(NQ)                       # tick 0.25, multiplier 20
ENTRY = D("25471.75")                              # the open of the entry bar of T2


def test_a_bracket_intent_becomes_a_proposal_with_prices_and_risk_worked_out_from_the_real_entry():
    # 20 ticks of 0.25 are 5 points: stop 25466.75 and target 25476.75 (the levels of T2), risk 5 x 20 x 1 = 100
    p = to_proposal(buy(NQ, 1, sl=Ticks(20), tp=Ticks(20), tag="t2"), desk_id=7, line="DT", sheet=SHEET, price=ENTRY)
    assert (p.stop_loss, p.take_profit, p.risk_usd) == (D("25466.75"), D("25476.75"), D(100))
    assert (p.desk_id, p.line, p.origin, p.order_type, p.thesis) == (7, "DT", "desk", "market", "t2")
    assert p.legs[0].qty == 1 and p.legs[0].effect == "open" and p.legs[0].side == "buy"
    short = to_proposal(sell(NQ, 2, sl=Pct(1), tp=Price(D("25000"))), desk_id=7, line="DT", sheet=SHEET, price=ENTRY)
    assert short.stop_loss == D("25726.50") and short.take_profit == D("25000") and short.risk_usd == D("254.7175") * 2 * 20   # 1 percent of 25471.75, on the tick grid


def test_the_control_fields_cannot_be_declared_by_the_strategy():
    names = set(inspect.signature(to_proposal).parameters)
    assert not names & {"origin", "effect", "risk_usd", "stop_loss", "take_profit", "limit_price", "fingerprint"}
    p = to_proposal(buy(NQ, 1), desk_id=1, line="SWC")
    assert (p.line, p.origin) == ("SWC", "desk")


def test_the_effect_is_read_from_what_is_held_and_never_from_the_strategy():
    sold = sell(NQ, 1)
    assert to_proposal(sold, desk_id=1, line="DT").legs[0].effect == "open"                          # nothing known: a plain sell opens
    assert to_proposal(sold, desk_id=1, line="DT", held_qty=D(1)).legs[0].effect == "close"          # it reduces a long
    assert to_proposal(sold, desk_id=1, line="DT", held_qty=D(-1)).legs[0].effect == "open"          # it adds to a short
    assert to_proposal(sell(NQ, 2), desk_id=1, line="DT", held_qty=D(1)).legs[0].effect == "open"    # it is bigger than the long
    assert to_proposal(buy(NQ, 1), desk_id=1, line="DT", held_qty=D(-3)).legs[0].effect == "close"
    reduce = OrderIntent(NQ, "sell", Size(SizeType.CONTRACTS, D(1)), reduce_only=True)
    assert to_proposal(reduce, desk_id=1, line="DT").legs[0].effect == "close" and to_proposal(reduce, desk_id=1, line="DT").reduce_only


def test_the_size_by_money_uses_the_real_price_and_equity():
    risk = buy(NQ, Size(SizeType.RISK_USD, D(1000)), sl=Ticks(20))                 # 1000 / (5 points x 20) is 10 contracts
    assert to_proposal(risk, desk_id=1, line="DT", sheet=SHEET, price=ENTRY, equity=D(1_000_000)).legs[0].qty == 10
    pct = buy(NQ, Size(SizeType.PCT_EQUITY, D(50)))                                # 500000 / (25000 x 20) is 1 contract
    assert to_proposal(pct, desk_id=1, line="DT", sheet=SHEET, price=D(25000), equity=D(1_000_000)).legs[0].qty == 1
    with pytest.raises(BridgeError) as lacking:
        to_proposal(pct, desk_id=1, line="DT")
    assert lacking.value.code == "bad_proposal"
    with pytest.raises(BridgeError):
        to_proposal(buy(NQ, 1, sl=Ticks(20)), desk_id=1, line="DT")               # a stop with no entry price to measure from


# --- N-14 and N-16: only the types the broker of the desk takes ------------------------------------------------------------------------

def test_the_order_types_of_the_engine_and_how_a_proposal_carries_them():
    table = order_types()
    assert set(table) == set(fills.ORDER_TYPES) | {"bracket", "oco"}
    assert {k: table[k] for k in ("market", "limit", "stop", "stop_limit")} == {"market": "market", "limit": "limit", "stop": "stop", "stop_limit": "stop_limit"}
    assert table["trailing_stop"] is None and table["limit_if_touched"] is None
    assert table["bracket"] == table["oco"] == "stop_loss+take_profit"


def test_a_trailing_order_on_a_desk_whose_broker_does_not_take_it_is_refused_with_the_code_of_live():
    trailing = OrderIntent(NQ, "sell", Size(SizeType.CONTRACTS, D(1)), "trailing", stop=D("25000"))
    for kind in ("sim", "schwab", "alpaca", "ibkr", "crypto"):
        with pytest.raises(BridgeError) as refused:
            to_proposal(trailing, desk_id=1, line="DT", broker_kind=kind)
        assert refused.value.code == "bad_proposal"


def test_the_list_of_a_broker_and_the_brackets_it_refuses():
    assert supported_types("schwab") == ("market", "limit", "stop", "stop_limit")
    assert supported_types("sim") == supported_types("schwab")
    with pytest.raises(BridgeError):
        supported_types("nowhere")
    stop_limit = buy(NQ, 1, limit=D(25000), stop=D(25010))
    assert to_proposal(stop_limit, desk_id=1, line="DT", broker_kind="schwab").order_type == "stop_limit"
    for kind in ("ibkr", "crypto"):
        with pytest.raises(BridgeError):
            to_proposal(buy(NQ, 1, sl=Ticks(20)), desk_id=1, line="DT", broker_kind=kind, sheet=SHEET, price=ENTRY)
    assert to_proposal(buy(NQ, 1, sl=Ticks(20)), desk_id=1, line="DT", broker_kind="schwab", sheet=SHEET, price=ENTRY).stop_loss == D("25466.75")


# --- L-59: the proposal goes to the engine and comes back as a fill -------------------------------------------------------------------

def test_a_proposal_is_translated_to_the_order_and_the_decision_of_the_engine():
    p = to_proposal(buy(NQ, 1, limit=D("25400")), desk_id=1, line="DT")
    order = to_engine_order(p)
    assert (order.type, order.side, order.qty, order.limit, order.trigger) == ("limit", "buy", 1, D("25400"), None)
    stop = to_engine_order(to_proposal(sell(NQ, 3, stop=D("25100")), desk_id=1, line="DT"))
    assert (stop.type, stop.side, stop.qty, stop.trigger) == ("stop", "sell", 3, D("25100"))
    bracket = to_proposal(buy(NQ, 2, sl=Ticks(20), tp=Ticks(20)), desk_id=1, line="DT", sheet=SHEET, price=ENTRY)
    assert to_decision(bracket, ref_price=ENTRY) == Decision("enter", "long", D(2), stop_points=D(5), target_points=D(5))
    closing = to_proposal(sell(NQ, 2), desk_id=1, line="DT", held_qty=D(2))
    assert to_decision(closing, ref_price=ENTRY, held_qty=D(2)) == Decision("exit")
    with pytest.raises(BridgeError):
        to_decision(closing, ref_price=ENTRY, held_qty=D(3))                       # a partial close is not something the engine runs
    with pytest.raises(BridgeError):
        to_decision(p, ref_price=ENTRY)                                             # a limit order is not an entry of the engine


@requires_engine
def test_a_fixture_proposal_comes_back_from_the_engine_as_a_fill_with_the_cost_tables_of_the_backtest():
    bars = bars_of("T1")
    d = {"buy": "2025-11-10T14:59:00Z", "sell": "2025-11-10T15:29:00Z"}
    entry = to_proposal(buy(NQ, 1), desk_id=1, line="DT", sheet=SHEET)
    exit_ = to_proposal(sell(NQ, 1), desk_id=1, line="DT", held_qty=D(1))
    decisions = {d["buy"]: [to_decision(entry, ref_price=bars[0].open)], d["sell"]: [to_decision(exit_, ref_price=bars[0].open, held_qty=D(1))]}
    out = engine.run(spec(), bars, plan(bars, decisions), tick_size=TICK, multiplier=MULT, symbol="NQZ5", protective_orders=False)
    assert [f.price for f in out.fills] == [D(expected("T1")["entry_price"]), D(expected("T1")["exit_price"])]
    assert out.realized_pnl_usd == D(expected("T1")["pnl_usd"]) == D("100.50")
    fee = costs.fee_model(spec().costs)
    assert out.costs_usd["commission"] == 2 * costs.commission_usd(fee, qty=D(1), price=out.fills[0].price, multiplier=MULT) == D(expected("T1")["commission_usd"])

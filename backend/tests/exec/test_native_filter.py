"""The native orders of SPEC-015 go through the same guards (no route reaches Schwab around the filter) and the scrub keeps masking credentials."""
import json
import time
from decimal import Decimal

import pytest
from broker_orders_support import ACCOUNT, account_body, pos, calls, connections, created, desks_db, env, db, fake, keys, limit, ready, rows, sent, server  # noqa: F401

from hokma.broker import service
from hokma.broker.service import BrokerError, Who, owner_who
from hokma.desks.core import safety
from hokma.exec import levels, prefilter

AGENT = Who(agent="desk-chief")


def test_a_native_order_over_a_new_limit_is_refused_and_nothing_goes_to_schwab(ready):
    prefilter.set_mark_source(lambda ids: {i: Decimal(10) for i in ids})
    service.set_limits(ready.desk_id, {"max_position_pct": 1}, db=ready.db)                     # 1 percent of the 10,000 of the desk
    with pytest.raises(BrokerError) as e:
        service.place(ready.desk_id, limit("BUY", 100, "10.00"), "attack", AGENT, db=ready.db)
    assert e.value.code in ("max_position_pct", "no_base") and sent(ready.fake) == []


def test_a_native_order_with_a_symbol_that_cannot_be_checked_is_refused(ready):
    with pytest.raises(BrokerError) as e:
        service.place(ready.desk_id, limit("BUY", 1, "10.00", symbol="$$$$"), "attack", AGENT, db=ready.db)
    assert sent(ready.fake) == []


def test_the_account_guard_and_the_emergency_monitor_also_stop_native_orders(ready):
    service.set_limits(ready.desk_id, {"funding": {"trailing_drawdown_usd": 5000}}, db=ready.db)
    with pytest.raises(BrokerError) as e:
        service.place(ready.desk_id, limit("BUY", 1, "10.00"), "attack", AGENT, db=ready.db)
    assert e.value.code in ("monitor_never_measured", "book_unavailable") and sent(ready.fake) == []


def test_credentials_in_urls_are_masked_whatever_precedes_the_scheme():
    for text in ("https://user:pw@host/x", "-https://user:pw@host/x", "1https://user:pw@host/x", "a-b+c.https://user:pw@host/x"):
        assert "pw" not in safety.scrub_text(text), text
    started = time.time()
    safety.scrub_text("a" * 200_000)
    safety.scrub_text("-" * 100_000 + "https://u:p@h")
    assert time.time() - started < 3


def _oco(*symbols):
    return {"orderStrategyType": "OCO", "childOrderStrategies": [
        {"orderStrategyType": "SINGLE", "session": "NORMAL", "duration": "DAY", "orderType": "LIMIT", "price": "10.00",
         "orderLegCollection": [{"instruction": "BUY", "quantity": 1, "instrument": {"symbol": s, "assetType": "EQUITY"}}]} for s in symbols]}


def test_an_oco_with_a_second_child_outside_the_list_is_refused_and_so_is_a_symbol_outside_the_list_by_the_native_road(ready):
    from hokma.desks import service as desks

    desks.set_assets(ready.desk_id, ["MSFT"], db=ready.db)
    with pytest.raises(BrokerError) as e:
        service.place(ready.desk_id, _oco("MSFT", "AAPL"), "attack", AGENT, db=ready.db)
    assert e.value.code == "cannot_check" and sent(ready.fake) == []                   # an OCO is only read as exits of the same contracts
    with pytest.raises(BrokerError) as e:
        service.place(ready.desk_id, limit(symbol="AAPL"), "attack", AGENT, db=ready.db)
    assert e.value.code == "symbol_not_allowed" and sent(ready.fake) == []
    with pytest.raises(BrokerError) as e:
        service.place(ready.desk_id, _oco("MSFT", "MSFT"), "entries", AGENT, db=ready.db)           # an OCO of entries is not one of the shapes that read
    assert e.value.code in ("cannot_check", "exit_exceeds") and sent(ready.fake) == []





def test_all_shares_legs_are_read_with_an_exact_list_and_never_skip_the_node():
    from hokma.exec import native

    node = {"orderStrategyType": "SINGLE", "orderType": "MARKET", "session": "NORMAL", "duration": "DAY", "orderLegCollection": [
        {"instruction": "SELL", "quantityType": "ALL_SHARES", "instrument": {"symbol": "MSFT", "assetType": "EQUITY"}},
        {"instruction": "BUY", "quantity": 50, "instrument": {"symbol": "AAPL", "assetType": "EQUITY"}}]}
    buy, close = native.to_proposals(1, node, created_by="x", line="LP")
    assert [g.contract_id for g in buy.legs] == ["EQ:AAPL"] and [g.contract_id for g in close.legs] == ["EQ:MSFT"]     # both go through the filter
    for bad in ("SELL_TO_OPEN", "SELLX", "SELL_SHORT", "BUY", "BUY_TO_OPEN"):
        leg = {"instruction": bad, "quantityType": "ALL_SHARES", "instrument": {"symbol": "MSFT", "assetType": "EQUITY"}}
        with pytest.raises(native.Unconvertible):
            native.to_proposals(1, {**node, "orderLegCollection": [leg]}, created_by="x", line="LP")
    option = {"instruction": "SELL_TO_CLOSE", "quantityType": "ALL_SHARES", "instrument": {"symbol": "SPY   261218C00600000", "assetType": "OPTION"}}
    with pytest.raises(native.Unconvertible):
        native.to_proposals(1, {**node, "orderLegCollection": [option]}, created_by="x", line="LP")
    with pytest.raises(native.Unconvertible):
        native.to_proposals(1, {**node, "orderLegCollection": [{"instruction": "SELLX", "quantity": 1, "instrument": {"symbol": "MSFT", "assetType": "EQUITY"}}]},
                            created_by="x", line="LP")


def test_an_all_shares_sell_of_a_symbol_outside_the_list_with_nothing_held_is_refused(ready):
    from hokma.desks import service as desks

    desks.set_assets(ready.desk_id, ["MSFT"], db=ready.db)
    order = {"orderStrategyType": "SINGLE", "orderType": "MARKET", "session": "NORMAL", "duration": "DAY", "orderLegCollection": [
        {"instruction": "SELL", "quantityType": "ALL_SHARES", "instrument": {"symbol": "AAPL", "assetType": "EQUITY"}}]}
    with pytest.raises(BrokerError) as e:
        service.place(ready.desk_id, order, "attack", AGENT, db=ready.db)
    assert e.value.code == "symbol_not_allowed" and sent(ready.fake) == []


def _leg(instruction, symbol, qty=None, all_shares=False, asset="EQUITY"):
    leg = {"instruction": instruction, "instrument": {"symbol": symbol, "assetType": asset}}
    if all_shares:
        leg["quantityType"] = "ALL_SHARES"
    else:
        leg["quantity"] = qty
    return leg


def _node(kind="SINGLE", legs=(), children=(), order_type="LIMIT"):
    node = {"orderStrategyType": kind, "session": "NORMAL", "duration": "DAY", "orderType": order_type, "price": "10.00"}
    if legs:
        node["orderLegCollection"] = list(legs)
    if children:
        node["childOrderStrategies"] = list(children)
    return node


ATTACKS = {
    "oco second child": _node("OCO", children=[_node(legs=[_leg("BUY", "MSFT", 1)]), _node(legs=[_leg("BUY", "AAPL", 1)])]),
    "trigger child is a new entry": _node("TRIGGER", [_leg("BUY", "MSFT", 5)], [_node(legs=[_leg("BUY", "AAPL", 5)])]),
    "grandchild": _node("TRIGGER", [_leg("BUY", "MSFT", 5)], [_node("TRIGGER", [_leg("SELL", "MSFT", 5)], [_node(legs=[_leg("BUY", "AAPL", 1)])])]),
    "all shares root with a child entry": _node("TRIGGER", [_leg("SELL", "MSFT", all_shares=True)], [_node(legs=[_leg("BUY", "AAPL", 1)])]),
    "all shares and a quantity leg in one node": _node(legs=[_leg("SELL", "MSFT", all_shares=True), _leg("BUY", "AAPL", 1)]),
    "all shares of the outside symbol": _node(legs=[_leg("SELL", "AAPL", all_shares=True)]),
    "exit child bigger than the parent": _node("TRIGGER", [_leg("BUY", "MSFT", 1)], [_node(legs=[_leg("SELL", "AAPL", 1)])]),
    "three levels deep": _node("TRIGGER", [_leg("BUY", "MSFT", 1)], [_node("OCO", children=[_node(legs=[_leg("SELL", "MSFT", 1)]), _node("TRIGGER", [_leg("SELL", "MSFT", 1)], [_node(legs=[_leg("BUY", "AAPL", 1)])])])]),
    "odd instruction": _node(legs=[_leg("SELLX", "MSFT", 1)]),
    "odd instruction in a child": _node("TRIGGER", [_leg("BUY", "MSFT", 1)], [_node(legs=[_leg("BUY_TO_OPENX", "MSFT", 1)])]),
    "unreadable symbol in a child": _node("OCO", children=[_node(legs=[_leg("BUY", "MSFT", 1)]), _node(legs=[_leg("BUY", "$$$", 1)])]),
    "same contract twice in a node": _node(legs=[_leg("BUY", "MSFT", 1), _leg("BUY", "MSFT", 2)]),
    "all shares on a buy": _node(legs=[_leg("BUY", "MSFT", all_shares=True)]),
}


@pytest.mark.parametrize("name", sorted(ATTACKS))
def test_every_nested_structure_ends_in_a_refusal_or_in_the_filter_and_nothing_reaches_schwab(ready, name):
    from hokma.desks import service as desks

    desks.set_assets(ready.desk_id, ["MSFT"], db=ready.db)                     # anything with AAPL in it must not go
    with pytest.raises(BrokerError) as e:
        service.place(ready.desk_id, ATTACKS[name], "attack", AGENT, db=ready.db)
    assert e.value.code in ("symbol_not_allowed", "cannot_check", "invalid", "rejected") or e.value.status in (400, 422), (name, e.value.code)
    assert sent(ready.fake) == [], name


def test_no_child_is_skipped_and_the_exits_of_a_trigger_add_up_against_what_there_is_to_exit(ready):
    from hokma.exec import native

    bracket = _node("TRIGGER", [_leg("BUY", "MSFT", 5)], [_node(legs=[_leg("SELL", "MSFT", 5)])])
    assert len(native.to_proposals(1, bracket, created_by="x", line="LP")) == 2             # the exit goes through the filter too
    two_exits = _node("TRIGGER", [_leg("BUY", "MSFT", 5)], [_node(legs=[_leg("SELL", "MSFT", 3)]), _node(legs=[_leg("SELL", "MSFT", 3)])])
    assert native.check(ready.desk_id, bracket, preview=None, who=AGENT, db=ready.db) is None
    refused = native.check(ready.desk_id, two_exits, preview=None, who=AGENT, db=ready.db)
    assert refused and refused["code"] == "exit_exceeds"
    with pytest.raises(BrokerError) as e:
        service.place(ready.desk_id, two_exits, "attack", AGENT, db=ready.db)
    assert e.value.code == "exit_exceeds" and sent(ready.fake) == []


def test_an_oco_nested_two_levels_shares_the_budget_between_alternatives_and_carries_the_least_left(ready):
    from hokma.exec import native

    alternatives = _node("TRIGGER", [_leg("BUY", "MSFT", 5)], [_node("OCO", children=[_node(legs=[_leg("SELL", "MSFT", 5)]), _node(legs=[_leg("SELL", "MSFT", 5)])])])
    assert native.check(ready.desk_id, alternatives, preview=None, who=AGENT, db=ready.db) is None      # one or the other fills: 5 and 5 are fine
    nested = _node("TRIGGER", [_leg("BUY", "MSFT", 5)], [_node("OCO", children=[
        _node(legs=[_leg("SELL", "MSFT", 5)]), _node("OCO", children=[_node(legs=[_leg("SELL", "MSFT", 4)]), _node(legs=[_leg("SELL", "MSFT", 6)])])])])
    assert native.check(ready.desk_id, nested, preview=None, who=AGENT, db=ready.db)["code"] == "cannot_check"      # an OCO inside an OCO is not a shape that reads
    sixes = _node("TRIGGER", [_leg("BUY", "MSFT", 5)], [_node("OCO", children=[_node(legs=[_leg("SELL", "MSFT", 5)]), _node(legs=[_leg("SELL", "MSFT", 6)])])])
    assert native.check(ready.desk_id, sixes, preview=None, who=AGENT, db=ready.db)["code"] == "exit_exceeds"      # the 6 is more than the 5 there is
    then_more = _node("TRIGGER", [_leg("BUY", "MSFT", 5)], [_node("OCO", children=[_node(legs=[_leg("SELL", "MSFT", 5)]), _node(legs=[_leg("SELL", "MSFT", 5)])]),
                                                              _node(legs=[_leg("SELL", "MSFT", 1)])])
    assert native.check(ready.desk_id, then_more, preview=None, who=AGENT, db=ready.db)["code"] == "exit_exceeds"   # after the OCO there is nothing left to exit


def test_an_order_without_legs_is_refused_as_not_checkable(ready):
    from hokma.exec import native

    assert native.check(ready.desk_id, {"orderStrategyType": "SINGLE"}, preview=None, who=AGENT, db=ready.db)["code"] == "cannot_check"
    assert _codes(ready, _node("TRIGGER", [_leg("BUY", "MSFT", 1)], [{"orderStrategyType": "SINGLE"}])) == "cannot_check"
    with pytest.raises(BrokerError):
        service.place(ready.desk_id, {"orderStrategyType": "SINGLE"}, "attack", AGENT, db=ready.db)
    assert sent(ready.fake) == []


def _desk_msft(ready):
    from hokma.desks import service as desks

    desks.set_assets(ready.desk_id, ["MSFT"], db=ready.db)


def _codes(ready, order):
    from hokma.exec import native

    r = native.check(ready.desk_id, order, preview=None, who=AGENT, db=ready.db)
    return r and r["code"]


def _live(ready, qty):
    ready.fake.on("GET", ACCOUNT, account_body([pos("MSFT", long=qty, value=qty * 10)] if qty else []))


def test_siblings_of_a_trigger_never_credit_each_other_in_any_order(ready):
    """Vera's attack (bug 1236): a root BUY 10 with the children BUY 100 and SELL 105 went to Schwab because one sibling funded the other."""
    import itertools

    _desk_msft(ready)
    for kids in itertools.permutations([_node(legs=[_leg("BUY", "MSFT", 100)]), _node(legs=[_leg("SELL", "MSFT", 105)])]):
        order = _node("TRIGGER", [_leg("BUY", "MSFT", 10)], list(kids))
        assert _codes(ready, order) == "cannot_check"                     # a child that opens is not an exit of the root: the shape does not read
        with pytest.raises(BrokerError) as e:
            service.place(ready.desk_id, order, "attack", AGENT, db=ready.db)
        assert e.value.code == "cannot_check" and sent(ready.fake) == []
    assert _codes(ready, _node("TRIGGER", [_leg("BUY", "MSFT", 10)], [_node(legs=[_leg("SELL", "MSFT", 105)])])) == "exit_exceeds"


def test_a_nested_oco_with_a_branch_that_opens_does_not_read_and_one_of_exits_does(ready):
    _desk_msft(ready)
    for kids in ([_leg("BUY", "MSFT", 100), _leg("SELL", "MSFT", 105)], [_leg("SELL", "MSFT", 105), _leg("BUY", "MSFT", 100)]):
        order = _node("TRIGGER", [_leg("BUY", "MSFT", 10)], [_node("OCO", children=[_node(legs=[k]) for k in kids])])
        assert _codes(ready, order) == "cannot_check"
    inside = _node("TRIGGER", [_leg("BUY", "MSFT", 10)], [_node("OCO", children=[_node(legs=[_leg("SELL", "MSFT", 10)]), _node(legs=[_leg("SELL", "MSFT", 10)])])])
    assert _codes(ready, inside) is None


def test_the_verdict_does_not_depend_on_the_order_of_the_children(ready):
    import itertools

    _desk_msft(ready)
    cases = {
        "too much": ([_node(legs=[_leg("SELL", "MSFT", 3)]), _node(legs=[_leg("SELL", "MSFT", 2)]), _node(legs=[_leg("SELL", "MSFT", 1)])], "exit_exceeds"),
        "exact": ([_node(legs=[_leg("SELL", "MSFT", 2)]), _node(legs=[_leg("SELL", "MSFT", 2)]), _node(legs=[_leg("SELL", "MSFT", 1)])], None),
        "an entry between exits": ([_node(legs=[_leg("BUY", "MSFT", 100)]), _node(legs=[_leg("SELL", "MSFT", 4)]), _node(legs=[_leg("SELL", "MSFT", 2)])], "cannot_check"),
        "an oco among exits": ([_node("OCO", children=[_node(legs=[_leg("SELL", "MSFT", 4)]), _node(legs=[_leg("SELL", "MSFT", 3)])]),
                                _node(legs=[_leg("SELL", "MSFT", 2)])], "exit_exceeds"),
    }
    for name, (kids, expected) in cases.items():
        verdicts = {_codes(ready, _node("TRIGGER", [_leg("BUY", "MSFT", 5)], list(p))) for p in itertools.permutations(kids)}
        assert verdicts == {expected}, (name, verdicts)


def test_siblings_share_the_budget_with_the_live_position(ready):
    _desk_msft(ready)
    _live(ready, 3)                                                        # 3 held and the root opens 5: 8 to exit, counted once for the whole tree
    over = _node("TRIGGER", [_leg("BUY", "MSFT", 5)], [_node(legs=[_leg("SELL", "MSFT", 5)]), _node(legs=[_leg("SELL", "MSFT", 4)])])
    exact = _node("TRIGGER", [_leg("BUY", "MSFT", 5)], [_node(legs=[_leg("SELL", "MSFT", 5)]), _node(legs=[_leg("SELL", "MSFT", 3)])])
    assert _codes(ready, over) == "exit_exceeds"
    assert _codes(ready, exact) is None
    with pytest.raises(BrokerError) as e:
        service.place(ready.desk_id, over, "attack", AGENT, db=ready.db)
    assert e.value.code == "exit_exceeds" and sent(ready.fake) == []
    _live(ready, 10)                                                       # an OCO of exits counts its biggest branch against what is held
    assert _codes(ready, _node("OCO", children=[_node(legs=[_leg("SELL", "MSFT", 6)]), _node(legs=[_leg("SELL", "MSFT", 10)])])) is None
    assert _codes(ready, _node("OCO", children=[_node(legs=[_leg("SELL", "MSFT", 6)]), _node(legs=[_leg("SELL", "MSFT", 11)])])) == "exit_exceeds"
    _live(ready, 0)                                                        # nothing held: there is nothing to exit
    assert _codes(ready, _node("OCO", children=[_node(legs=[_leg("SELL", "MSFT", 1)]), _node(legs=[_leg("SELL", "MSFT", 2)])])) == "exit_exceeds"


def test_an_all_shares_exit_takes_everything_and_nothing_else_of_its_contract_may_exit(ready):
    _desk_msft(ready)
    _live(ready, 10)
    everything = _leg("SELL", "MSFT", all_shares=True)
    for order in (_node("OCO", children=[_node(legs=[everything]), _node(legs=[_leg("SELL", "MSFT", 5)])]),
                  _node("TRIGGER", [_leg("BUY", "MSFT", 5)], [_node(legs=[everything]), _node(legs=[_leg("SELL", "MSFT", 1)])]),
                  _node("TRIGGER", [_leg("BUY", "MSFT", 5)], [_node("OCO", children=[_node(legs=[everything]), _node(legs=[_leg("SELL", "MSFT", 1)])])])):
        assert _codes(ready, order) == "cannot_check"
        with pytest.raises(BrokerError) as e:
            service.place(ready.desk_id, order, "attack", AGENT, db=ready.db)
        assert e.value.code == "cannot_check" and sent(ready.fake) == []
    assert _codes(ready, _node("TRIGGER", [_leg("BUY", "MSFT", 5)], [_node(legs=[everything])])) is None

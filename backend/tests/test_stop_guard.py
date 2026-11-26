"""The stop of an entry before it goes out (SPEC-026 v2, section 4.4 a, piece 3390e3; tests A16, A17, A18). Hokma reserves nothing and values nothing: it refuses an entry
with no stop where one is mandatory, or with a stop that cannot work. The STOP exit of a native order that does not cover its entry is tested in test_native_filter."""
from __future__ import annotations

from decimal import Decimal

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from desks_support import desks_db  # noqa: F401  (fixtures)
from exec_support import NQ, SPY, proposal, ready  # noqa: F401  (fixtures)

from hokma import exec as hexec
from hokma.contracts import discipline
from hokma.exec.guards import registry
from hokma.exec.guards.stop import StopRefused, check_stop
from hokma.exec.proposal import Leg, OrderProposal
from hokma.i18n import catalog

D = Decimal
MNQ = "FUT:MNQ:2026-12"
CALL = "OPT:SPY:2026-12-18:C:600"
CALL_HI = "OPT:SPY:2026-12-18:C:610"


def sheets(*ids):
    return {k: v for k, v in discipline.load_sheets(list(ids)).items() if v is not None}


def prop(*legs, **kw):
    kw.setdefault("order_type", "market")
    return OrderProposal(1, "DT", "chat", tuple(legs), created_by="owner", **kw)


def leg(cid, side="buy", effect="open", qty="1"):
    return Leg(cid, side, effect, D(qty))


def refused(p, marks):
    with pytest.raises(StopRefused) as e:
        check_stop(p, sheets(*(g.contract_id for g in p.legs)), marks)
    return e.value


# ---- the rule of each instrument -------------------------------------------------------------------------------------

def test_a_future_or_a_micro_needs_its_stop_long_or_short_and_the_target_is_optional():
    for cid in (NQ, MNQ):
        check_stop(prop(leg(cid), stop_loss=D("20990")), sheets(cid), {cid: D(21000)})
        check_stop(prop(leg(cid, "sell"), stop_loss=D("21010")), sheets(cid), {cid: D(21000)})
        check_stop(prop(leg(cid, "sell"), stop_loss=D("21010"), take_profit=D("20900")), sheets(cid), {cid: D(21000)})
        assert refused(prop(leg(cid)), {cid: D(21000)}).code == "stop_required"
        assert refused(prop(leg(cid, "sell")), {cid: D(21000)}).code == "stop_required"


def test_a_stop_on_the_wrong_side_and_an_entry_with_no_price_are_refused():
    assert refused(prop(leg(NQ), stop_loss=D("21010")), {NQ: D(21000)}).code == "stop_invalid"             # a buy with its stop above
    assert refused(prop(leg(NQ, "sell"), stop_loss=D("20990")), {NQ: D(21000)}).code == "stop_invalid"      # a sale with its stop below
    assert refused(prop(leg(NQ), stop_loss=D("21000")), {NQ: D(21000)}).code == "stop_invalid"              # a stop AT the entry is on neither side
    assert refused(prop(leg(NQ), stop_loss=D("20990")), {}).code == "stop_unpriced"                         # no limit and no quote: the side cannot be read
    check_stop(prop(leg(NQ), stop_loss=D("20990"), order_type="limit", limit_price=D("21000")), sheets(NQ), {})        # the limit of the order is the price


def test_shares_bought_need_no_stop_and_a_stop_they_carry_has_to_be_below_the_entry():
    check_stop(prop(leg(SPY, qty="100")), sheets(SPY), {SPY: D(600)})
    check_stop(prop(leg(SPY, qty="100"), stop_loss=D(590)), sheets(SPY), {SPY: D(600)})
    assert refused(prop(leg(SPY, qty="10"), stop_loss=D(610)), {SPY: D(600)}).code == "stop_invalid"
    assert refused(prop(leg(SPY, qty="10"), stop_loss=D(590)), {}).code == "stop_unpriced"                  # a stop with no price to read its side against is not zero


def test_a_short_of_shares_and_a_naked_option_need_their_stop_above_the_entry():
    assert refused(prop(leg(SPY, "sell", qty="10")), {SPY: D(600)}).code == "stop_required"
    check_stop(prop(leg(SPY, "sell", qty="10"), stop_loss=D(610)), sheets(SPY), {SPY: D(600)})
    assert refused(prop(leg(SPY, "sell", qty="10"), stop_loss=D(590)), {SPY: D(600)}).code == "stop_invalid"
    assert refused(prop(leg(CALL, "sell")), {CALL: D(5)}).code == "stop_required"
    check_stop(prop(leg(CALL, "sell"), stop_loss=D(8)), sheets(CALL), {CALL: D(5)})
    for stop in ("0.01", "0", "4"):                                                                          # a stop of one cent, of zero, or under the entry never passes
        assert refused(prop(leg(CALL, "sell"), stop_loss=D(stop)), {CALL: D(5)}).code == "stop_invalid"


def test_a_bought_option_and_a_vertical_spread_need_no_stop():
    check_stop(prop(leg(CALL, qty="2"), order_type="limit", limit_price=D("5.10")), sheets(CALL), {})
    spread = prop(leg(CALL, "sell"), leg(CALL_HI), order_type="limit", limit_price=D("2.5"))
    check_stop(spread, sheets(CALL, CALL_HI), {})
    check_stop(prop(leg(CALL), leg(CALL_HI, "sell"), order_type="limit", limit_price=D("2.5")), sheets(CALL, CALL_HI), {})


def test_a_combination_that_cannot_carry_one_stop_is_refused_with_its_own_words():
    both = refused(prop(leg(CALL, "sell"), leg(CALL_HI, "sell")), {})                                       # two naked sales are not a vertical
    assert both.code == "stop_shape"
    two_futures = prop(leg(NQ), leg(MNQ), stop_loss=D("20990"))
    assert refused(two_futures, {NQ: D(21000), MNQ: D(21000)}).code == "stop_shape"


def test_a_close_needs_no_stop():
    check_stop(prop(leg(NQ, "sell", "close")), sheets(NQ), {NQ: D(21000)})


def test_the_words_of_every_refusal_exist_in_both_languages():
    for lang in ("es", "en"):
        words = catalog(lang)
        for code, params in (("stop_required", {"contract": "NQ 2026-12"}), ("stop_invalid", {"contract": "NQ", "stop": 1, "entry": 2, "side": "x"}),
                             ("stop_unpriced", {"contract": "NQ"}), ("stop_shape", {"contract": "NQ"}), ("stop_qty", {"contract": "NQ", "qty": 2, "stop_qty": 1})):
            assert words[f"risk.{code}"].format(**params)
        assert "stop" in words["risk.stop_required"].lower() and "reserv" not in words["risk.stop_unpriced"].lower()


def test_the_stop_guard_is_always_on_and_sits_before_the_rules_of_the_broker():
    names = [g.name for g in registry.build({"guards": []})]
    assert "stop" in registry.ALWAYS and names.index("stop") == names.index("broker_rules") - 1
    assert "risk_budget" not in registry.known_types()


# ---- at the door -----------------------------------------------------------------------------------------------------------------

def send(r, *, qty="1", stop="20990", side="buy"):
    p = proposal(r, qty=qty, side=side, origin="chat", created_by="owner", stop_loss=None if stop is None else D(stop))
    return hexec.submit(p, who=r.owner, db=r.db)


def test_A16_an_entry_without_a_stop_or_with_a_wrong_one_is_refused_and_with_a_stop_and_no_target_it_passes_and_nothing_is_reserved(ready):
    ready.board.feed(NQ, bid="21000", ask="21000", last="21000")
    none = send(ready, stop=None)
    assert (none.status, none.code) == ("refused", "stop_required") and "stop" in none.why
    wrong = send(ready, stop="21010")
    assert (wrong.status, wrong.code) == ("refused", "stop_invalid")
    ok = send(ready)
    assert ok.status == "filled"
    with ready.db.read() as u:
        assert not u.session.table_exists("risk" + "_reservations")                                              # no reservation table in this chain: nothing is reserved


def test_A17_a_long_share_with_no_stop_passes(ready):
    out = hexec.submit(proposal(ready, contract=SPY, qty="10", origin="chat", created_by="owner"), who=ready.owner, db=ready.db)
    assert out.status == "filled"


def test_A18_a_short_sale_with_no_stop_is_refused(ready):
    out = hexec.submit(proposal(ready, contract=SPY, side="sell", qty="10", origin="chat", created_by="owner", stop_loss=None), who=ready.owner, db=ready.db)
    assert (out.status, out.code) == ("refused", "stop_required")
    wrong = hexec.submit(proposal(ready, contract=SPY, side="sell", qty="10", origin="chat", created_by="owner", stop_loss=D(590)), who=ready.owner, db=ready.db)
    assert (wrong.status, wrong.code) == ("refused", "stop_invalid")


def test_a_closing_order_never_meets_the_stop_guard(ready):
    ready.board.feed(NQ, bid="21000", ask="21000", last="21000")
    assert send(ready).status == "filled"
    out = hexec.submit(proposal(ready, side="sell", effect="close", qty="1", origin="chat", created_by="owner"), who=ready.owner, db=ready.db)
    assert out.status == "filled"

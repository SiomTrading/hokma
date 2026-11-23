"""Attacks on the controls of H2: nothing the agent declares (origin, effect, prices, lists, risk) is believed to decide a control."""
import dataclasses
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from exec_support import AGENT, NQ, SPY, db, desks_db, env, proposal, ready  # noqa: F401
from .test_guards import NOW, book, ctx, position, prop  # noqa: F401

from hokma import exec as hexec
from hokma.broker import service, sim, sim_engine
from hokma.broker.sim import SimBroker
from hokma.contracts import identity
from hokma.contracts.identity import NoContract
from hokma.exec import levels, prefilter
from hokma.exec.guards import is_closing
from hokma.exec.guards.account import AccountGuard
from hokma.exec.guards.max_position import MaxPositionSizeGuard
from hokma.exec.limits import apply_limits
from hokma.exec.proposal import Leg, OrderProposal, ProposalError


def rows(r, table, where="1=1"):
    with r.db.read() as u:
        return u.session.query(f"SELECT * FROM {table} WHERE {where} ORDER BY id")


def test_an_absent_guards_list_means_every_guard_so_an_agent_cannot_narrow_it(ready):
    for narrowed in (["cooldown"], ["contract"], []):
        with pytest.raises(service.BrokerError) as e:
            service.set_limits(ready.desk_id, {"guards": narrowed}, db=ready.db, tighten_only=True)
        assert e.value.code == "owner_only"
    service.set_limits(ready.desk_id, {"guards": ["cooldown", "account"]}, db=ready.db)                      # the owner may
    service.set_limits(ready.desk_id, {"guards": ["cooldown", "account", "limits"]}, db=ready.db, tighten_only=True)     # adding is stricter
    with pytest.raises(service.BrokerError):
        service.set_limits(ready.desk_id, {"guards": None}, db=ready.db, tighten_only=True) if False else service.set_limits(
            ready.desk_id, {"guards": ["cooldown"]}, db=ready.db, tighten_only=True)


def test_repeated_legs_of_one_contract_are_refused_and_a_close_is_measured_against_what_is_left():
    with pytest.raises(ProposalError):
        OrderProposal(1, "DT", "desk", (Leg(NQ, "sell", "close", Decimal(1)), Leg(NQ, "sell", "close", Decimal(1))), "market", created_by="x")
    held = (position(NQ, 1),)
    assert is_closing(prop(side="sell", qty="1", effect="close"), held) is True
    assert is_closing(prop(side="sell", qty="2", effect="close"), held) is False


def test_a_far_limit_price_cannot_shrink_the_exposure_and_no_mark_means_no_price():
    far = ctx(prop(qty="2", order_type="limit", limit_price=Decimal("1")), limits={"max_position_pct": 50})        # mark is 21000
    out = MaxPositionSizeGuard().check(far)
    assert out.verdict == "block" and out.code == "max_position_pct"
    nomark = ctx(prop(qty="1", order_type="limit", limit_price=Decimal("1")), limits={"max_position_pct": 50}, marks={})
    assert MaxPositionSizeGuard().check(nomark).code == "unpriced"
    OPT = "OPT:SPY:2026-12-18:C:600"
    swo = apply_limits(prop(OPT, qty="10", line="SWO", order_type="limit", limit_price=Decimal("0.01")), limits={"max_premium_usd": 2000},
                       contracts=ctx().contracts, positions=(), equity=Decimal(1_000_000), marks={OPT: Decimal("5.5")})
    assert swo.proposal.legs[0].qty == 3                           # counted at the mark, not at one cent


def test_a_leg_declared_close_that_reduces_nothing_is_an_open_for_the_limits_and_the_guards(ready):
    sneaky = prop(side="buy", qty="5", effect="close")             # declared close, nothing to close
    cut = apply_limits(sneaky, limits={"funding": {"max_contracts": 3}}, contracts=ctx().contracts, positions=(), equity=Decimal(1_000_000), marks={})
    assert cut.proposal.legs[0].qty == 3 and cut.proposal.legs[0].effect == "open"
    losing = ctx(sneaky, limits={"funding": {"daily_loss_usd": 1000}}, book=book(day_pnl=Decimal(-5000)))
    from hokma.exec.guards import normalize_effects
    from hokma.exec.guards.pipeline import run as run_guards
    normal = dataclasses.replace(losing, proposal=normalize_effects(losing.proposal, losing.positions))
    assert run_guards([AccountGuard()], normal).verdict == "BLOCK"          # not exempt as a closing order


def test_an_agent_that_writes_origin_chat_is_not_self_approved(ready):
    levels.set_level(ready.desk_id, 2, who=ready.owner, db=ready.db)
    out = hexec.submit(proposal(ready, origin="chat", created_by="desk-chief"), who=AGENT, db=ready.db)
    assert out.status == "awaiting_owner" and rows(ready, "sim_orders") == []
    assert rows(ready, "exec_proposals")[0]["origin"] == "desk"


def test_the_risk_of_a_rule_is_computed_not_declared_and_a_bare_symbol_does_not_cover_options(ready):
    levels.set_level(ready.desk_id, 3, rules={"id": "r", "max_risk_usd": 500, "symbols": ["FUT:NQ"]}, who=ready.owner, db=ready.db)
    lie = hexec.submit(proposal(ready, qty="1", risk_usd=Decimal("1")), who=AGENT, db=ready.db)       # declares 1 USD; no stop: the risk is 420000
    assert lie.status == "awaiting_owner"
    honest = hexec.submit(proposal(ready, qty="1", stop_loss=Decimal("20999"), risk_usd=Decimal("1")), who=AGENT, db=ready.db)   # 1.25 x 20 = 25
    assert honest.status == "awaiting_owner"        # the simulated broker places no protective stop, so the stop lowers nothing
    sheets = {c: identity.resolve(c) for c in (NQ, "EQ:AAPL", "OPT:AAPL:2026-12-18:C:300")}
    marks = {"EQ:AAPL": Decimal(200), "OPT:AAPL:2026-12-18:C:300": Decimal(5)}
    shares_rule = {"id": "s", "symbols": ["AAPL"], "max_qty": "100"}
    shares = prop("EQ:AAPL", qty="10", line="LP")
    option = prop("OPT:AAPL:2026-12-18:C:300", qty="10", line="SWO")
    assert levels.rules_allow(shares_rule, shares, sheets=sheets, marks=marks) == (True, "s")
    assert levels.rules_allow(shares_rule, option, sheets=sheets, marks=marks)[0] is False
    assert levels.rules_allow({"id": "o", "symbols": ["OPT:AAPL"], "max_qty": "100"}, option, sheets=sheets, marks=marks)[0] is True
    assert levels.rules_allow({"id": "f", "symbols": ["AAPL"], "max_qty": "100"}, prop("FUT:NQ:2026-12", qty="1"), sheets=sheets, marks=marks)[0] is False


# ------------------------------------------------------------------------------------------------------ simulated broker

def neutral(side="buy", qty="1", order_type="limit", limit="20000", reduce_only=False):
    return {"fingerprint": "a" * 64, "type": order_type, "tif": "gtc", "limit_price": limit, "stop_price": None, "stop_loss": None, "take_profit": None,
            "reduce_only": reduce_only,
            "legs": [{"contract_id": NQ, "symbol": NQ, "side": side, "effect": "open", "qty": qty, "multiplier": "20", "tick_size": "0.25", "asset_type": "future"}]}


class Live:
    def __init__(self, r):
        self.r = r

    def __enter__(self):
        self.ctx = self.r.db.read()
        u = self.ctx.__enter__()
        return SimBroker(u.broker, u.broker.get_broker(self.r.broker_id))

    def __exit__(self, *a):
        return self.ctx.__exit__(*a)


def test_a_canceled_order_is_never_filled_by_a_late_fill(ready):
    with Live(ready) as b:
        waiting = b.place_order(ready.account_id, neutral())
        b.cancel_order(ready.account_id, waiting["order_id"])
        parsed = neutral()
        quote = ready.board.quote(NQ)
        late = b._book_fills(ready.account_id, waiting["order_id"], parsed, [sim_engine.fill_leg(parsed["legs"][0], order_type="market", limit=None, stop=None,
                                                                                             quote=quote, slippage_bps=0, commission_cfg={"type": "per_share", "value": 0})])
    assert late["status"] == "CANCELED"
    assert rows(ready, "sim_fills") == [] and rows(ready, "book_entries", "kind = 'fill'") == []


def test_the_off_switch_holds_for_working_orders_and_archived_accounts_and_paused_desks_do_not_fill(ready):
    with Live(ready) as b:
        waiting = b.place_order(ready.account_id, neutral(limit="21100"))                # crosses at once? ask 21000.25 <= 21100: it fills; use a far buy
    assert waiting["status"] == "FILLED"
    with Live(ready) as b:
        waiting = b.place_order(ready.account_id, neutral(limit="19000"))
        assert waiting["status"] == "WORKING"
    ready.board.feed(NQ, bid="18999", ask="18999.25", last="18999.25")
    service.set_orders_enabled(ready.broker_id, False, db=ready.db)
    with Live(ready) as b:
        assert b.process(NQ) == 0
    service.set_orders_enabled(ready.broker_id, True, db=ready.db)
    from hokma.desks import service as desks
    desks.update_desk(ready.desk_id, {"status": "paused"}, db=ready.db)
    with Live(ready) as b:
        assert b.process(NQ) == 0
    desks.update_desk(ready.desk_id, {"status": "active"}, db=ready.db)
    with ready.db.write() as u:
        u.broker.archive_account(ready.account_id)
    with Live(ready) as b:
        assert b.process(NQ) == 0
    assert rows(ready, "sim_orders", "status = 'working'")


def test_a_reset_with_positions_or_open_orders_is_refused_and_after_a_reset_old_positions_are_gone(ready):
    from hokma.broker.contract import NotSupported
    with Live(ready) as b:
        b.place_order(ready.account_id, neutral(limit="21100"))            # fills: a position
    with ready.db.write() as u:
        with pytest.raises(NotSupported):
            SimBroker(u.broker, u.broker.get_broker(ready.broker_id)).reset(ready.account_id)
    with Live(ready) as b:
        b.place_order(ready.account_id, neutral("sell", "1", "market", None))      # flat again
    with ready.db.write() as u:
        SimBroker(u.broker, u.broker.get_broker(ready.broker_id)).reset(ready.account_id)
    with Live(ready) as b:
        assert b.positions(ready.account_id) == [] and b.balance(ready.account_id).cash == 1_000_000.0


def test_reduce_only_is_checked_against_the_real_position_and_does_not_skip_the_cash_check(ready):
    with Live(ready) as b:
        none = b.place_order(ready.account_id, neutral("sell", "3", "market", None, reduce_only=True))
        assert none["status"] == "REJECTED" and none["reason"] == "reduce_only_nothing"
        b.place_order(ready.account_id, neutral("buy", "2", "market", None))
    with Live(ready) as b:
        clipped = b.place_order(ready.account_id, neutral("sell", "5", "market", None, reduce_only=True))
        assert clipped["status"] == "FILLED" and clipped["filled_qty"] == "2"      # cut to the 2 contracts held
    with ready.db.write() as u:                                                   # not enough cash for shares, flagged reduce_only
        u.session.insert_row("book_entries", {"account_id": ready.account_id, "kind": "withdrawal", "amount": "-999000", "currency": "USD", "source": "owner",
                                              "source_ref": "w1", "occurred_at": "2026-11-22 00:00:00", "recorded_at": "2999-01-01 00:00:00"})
    ready.board.feed("EQ:SPY", bid="600", ask="600.02", last="600")
    shares = {"fingerprint": "b" * 64, "type": "market", "tif": "day", "limit_price": None, "stop_price": None, "stop_loss": None, "take_profit": None,
              "reduce_only": True, "legs": [{"contract_id": "EQ:SPY", "symbol": "SPY", "side": "buy", "effect": "close", "qty": "1000", "multiplier": "1",
                                              "tick_size": "0.01", "asset_type": "equity"}]}
    with Live(ready) as b:
        out = b.place_order(ready.account_id, shares)
    assert out["status"] == "REJECTED"


def test_a_stop_lowers_the_computed_risk_only_when_the_broker_places_it_with_the_order_and_at_a_real_distance(ready):
    sheets = {NQ: identity.resolve(NQ)}
    marks = {NQ: Decimal(21000)}
    p = prop(qty="1", stop_loss=Decimal("20990"))                           # 10 points x 20 = 200 USD of risk
    assert levels.computed_risk(p, sheets, marks, protective=False) == Decimal(420000)     # nothing places the stop: the whole notional
    assert levels.computed_risk(p, sheets, marks, protective=True) == Decimal(200)
    too_close = prop(qty="1", stop_loss=Decimal("20999.75"))                 # one tick: not a stop
    assert levels.computed_risk(too_close, sheets, marks, protective=True) == Decimal(420000)
    rule = {"id": "r", "max_risk_usd": 500, "symbols": ["FUT:NQ"]}
    assert levels.rules_allow(rule, p, sheets=sheets, marks=marks)[0] is False
    assert levels.rules_allow(rule, p, sheets=sheets, marks=marks, protective=True)[0] is True


def test_two_concurrent_fills_that_together_pass_the_cash_cannot_both_go_through(ready):
    import threading
    shares = lambda: {"fingerprint": "c" * 64, "type": "limit", "tif": "gtc", "limit_price": "100", "stop_price": None, "stop_loss": None, "take_profit": None,
                      "reduce_only": False, "legs": [{"contract_id": "EQ:SPY", "symbol": "SPY", "side": "buy", "effect": "open", "qty": "1000",
                                                      "multiplier": "1", "tick_size": "0.01", "asset_type": "equity"}]}
    with Live(ready) as b:
        ids = [b.place_order(ready.account_id, shares())["order_id"] for _ in range(2)]      # each 600k at the market: together 1.2M on 1M
    ready.board.feed("EQ:SPY", bid="99", ask="99.5", last="99.5")
    ready.board.feed("EQ:SPY", bid="99", ask="99.5", last="99.5")
    results = []
    def go():
        with Live(ready) as b:
            results.append(b.process("EQ:SPY"))
    # 1000 x 99.5 = 99,500 each: raise the price so that the two together pass 1M
    ready.board.feed("EQ:SPY", bid="600", ask="600", last="600")
    with Live(ready) as b:
        for ref in ids:
            b.cancel_order(ready.account_id, ref)
        ids = [b.place_order(ready.account_id, {**shares(), "limit_price": "601"})["order_id"] for _ in range(2)]
    # both cross at 600 and cost 600,000 each
    ts = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    done = rows(ready, "sim_orders", "status = 'filled'")
    assert len(done) + len(rows(ready, "sim_orders", "status = 'rejected'")) >= 1 and len(done) <= 1
    assert len(rows(ready, "sim_fills")) <= 1


def test_replacing_a_reduce_only_order_cannot_grow_it_past_the_position_in_the_simulated_broker(ready):
    with Live(ready) as b:
        b.place_order(ready.account_id, neutral("buy", "2", "market", None))                                # a position of 2
    with Live(ready) as b:
        waiting = b.place_order(ready.account_id, {**neutral("sell", "2", "limit", "22000", reduce_only=True)})
        assert waiting["status"] == "WORKING"
    with Live(ready) as b:
        bigger = b.replace_order(ready.account_id, waiting["order_id"], neutral("sell", "9", "limit", "22000", reduce_only=True))
        assert bigger["status"] == "WORKING"
        order = b.get_order(ready.account_id, bigger["order_id"])
    with ready.db.read() as u:
        sent_qty = u.session.one("SELECT request_json FROM sim_orders WHERE order_ref = :r", {"r": bigger["order_id"]})["request_json"]
    assert '"qty": "9"' in sent_qty                                  # the request is kept as sent: the cut happens when it fills
    ready.board.feed(NQ, bid="22001", ask="22001.25", last="22001")
    with Live(ready) as b:
        assert b.process(NQ) == 1
    with ready.db.read() as u:
        assert u.session.query("SELECT qty FROM sim_fills ORDER BY id")[-1]["qty"] == "2"
    with Live(ready) as b:                                           # nothing left to reduce: a replace is refused and the order stays
        w2 = b.place_order(ready.account_id, neutral("buy", "1", "limit", "100"))
        refused = b.replace_order(ready.account_id, w2["order_id"], neutral("sell", "1", "limit", "22000", reduce_only=True))
        assert refused["status"] == "REJECTED" and b.get_order(ready.account_id, w2["order_id"]).status == "WORKING"

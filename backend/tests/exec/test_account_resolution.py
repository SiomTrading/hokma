"""Which account an order goes out by (SPEC-027 piece 027b: section 5, C2 backend, C6, C7) and the account lock, the killswitch by account."""
import json
import threading
from decimal import Decimal

import pytest
from exec_support import AGENT, NQ, Ready, db, desks_db, env, make_desk, proposal, ready  # noqa: F401

from hokma import exec as hexec
from hokma.broker import rules, service
from hokma.exec import approvals, killswitch, levels, submit as submit_module
from hokma.exec.proposal import OrderProposal, ProposalError
from account_links_support import unlink_all


def rows(r, table, where="1=1"):
    with r.db.read() as u:
        return u.session.query(f"SELECT * FROM {table} WHERE {where} ORDER BY id")


def add_account(r, name="Sim B", capital=500_000, desk_id=None):
    with r.db.write() as u:
        account = u.broker.create_account(r.broker_id, name, capital)
        u.broker.link(desk_id or r.desk_id, account.id)
    return account.id


def with_strategy(r, key):
    return proposal(r, strategy_key=key)


def test_a_desk_without_accounts_keeps_the_proposal_as_analysis_only_and_sends_nothing(ready):
    with ready.db.write() as u:
        unlink_all(u, ready.desk_id)
    levels.set_level(ready.desk_id, 2, who=ready.owner, db=ready.db)
    out = hexec.submit(proposal(ready), who=AGENT, db=ready.db)
    assert (out.status, out.code) == ("analysis_only", "no_account")
    assert out.why
    saved = rows(ready, "exec_proposals")
    assert [(x["status"], x["account_id"]) for x in saved] == [("analysis_only", None)]
    assert rows(ready, "broker_orders") == [] and rows(ready, "sim_orders") == []         # no order and no refusal ticket


def test_the_order_goes_out_by_the_account_of_its_strategy_and_records_desk_strategy_and_account(ready):
    other = add_account(ready)
    with ready.db.write() as u:
        u.broker.bind_strategy(ready.desk_id, "carry", other)
    levels.set_level(ready.desk_id, 2, who=ready.owner, db=ready.db)
    out = hexec.submit(with_strategy(ready, "carry"), who=AGENT, db=ready.db)
    assert out.status == "awaiting_owner"
    assert rows(ready, "exec_proposals")[0]["account_id"] == other
    done = hexec.approve(out.proposal_id, who=ready.owner, db=ready.db)
    assert done.status == "filled"
    placed = rows(ready, "broker_orders", "action = 'place' AND rule_result = 'auto'")[0]
    assert (placed["desk_id"], placed["strategy_key"], placed["account_id"]) == (ready.desk_id, "carry", other)
    saved = rows(ready, "exec_proposals")[0]
    assert (saved["desk_id"], saved["strategy_key"], saved["account_id"]) == (ready.desk_id, "carry", other)
    assert {x["account_id"] for x in rows(ready, "sim_orders")} == {other}                 # the broker saw the account of the strategy


def test_the_principal_strategy_uses_its_own_account_not_the_first_or_the_newest(ready):
    add_account(ready)                                    # a second account exists: it must not take the order
    levels.set_level(ready.desk_id, 2, who=ready.owner, db=ready.db)
    out = hexec.submit(proposal(ready), who=AGENT, db=ready.db)
    assert hexec.approve(out.proposal_id, who=ready.owner, db=ready.db).status == "filled"
    assert {x["account_id"] for x in rows(ready, "sim_orders")} == {ready.account_id}


def test_a_strategy_without_an_account_waits_for_the_owner_with_needs_account_and_nothing_is_sent(ready):
    add_account(ready)
    levels.set_level(ready.desk_id, 2, who=ready.owner, db=ready.db)
    out = hexec.submit(with_strategy(ready, "carry"), who=AGENT, db=ready.db)
    assert (out.status, out.code) == ("awaiting_owner", "needs_account") and out.decision_id
    assert rows(ready, "broker_orders") == [] and rows(ready, "sim_orders") == []
    pending = approvals.list_pending(ready.desk_id, db=ready.db)
    assert pending[0]["needs_account"] is True and pending[0]["strategy_key"] == "carry"
    with pytest.raises(service.BrokerError) as e:         # approving is not choosing: the account is still missing
        hexec.approve(out.proposal_id, who=ready.owner, db=ready.db)
    assert e.value.code == "needs_account"
    assert rows(ready, "sim_orders") == []


def test_the_owner_chooses_the_account_for_this_order_only_or_ties_the_strategy(ready):
    other = add_account(ready)
    levels.set_level(ready.desk_id, 2, who=ready.owner, db=ready.db)
    first = hexec.submit(with_strategy(ready, "carry"), who=AGENT, db=ready.db)
    asked = approvals.choose_account(first.proposal_id, other, who=ready.owner, db=ready.db)
    assert asked.status == "awaiting_owner" and asked.code == "awaiting_owner"            # level 2: it now waits for the usual approval
    assert [x["status"] for x in rows(ready, "exec_proposals")] == ["canceled", "awaiting_owner"]
    assert rows(ready, "exec_proposals")[1]["account_id"] == other
    with ready.db.read() as u:
        assert u.broker.account_of_strategy(ready.desk_id, "carry") is None                # only this order
    assert hexec.approve(asked.proposal_id, who=ready.owner, db=ready.db).status == "filled"
    again = hexec.submit(with_strategy(ready, "carry"), who=AGENT, db=ready.db)
    assert again.code == "needs_account"                                                   # asked again: nothing was tied
    tied = approvals.choose_account(again.proposal_id, other, who=ready.owner, bind=True, db=ready.db)
    assert tied.status == "awaiting_owner"
    with ready.db.read() as u:
        assert u.broker.account_of_strategy(ready.desk_id, "carry").id == other
    third = hexec.submit(with_strategy(ready, "carry"), who=AGENT, db=ready.db)
    assert third.code == "awaiting_owner"                                                  # no question this time


def test_after_the_strategy_is_tied_in_the_chat_approving_the_waiting_proposal_sends_it_by_that_account(ready):
    other = add_account(ready)
    levels.set_level(ready.desk_id, 2, who=ready.owner, db=ready.db)
    out = hexec.submit(with_strategy(ready, "carry"), who=AGENT, db=ready.db)
    with ready.db.write() as u:
        u.broker.bind_strategy(ready.desk_id, "carry", other)
    moved = hexec.approve(out.proposal_id, who=ready.owner, db=ready.db)
    assert moved.status == "awaiting_owner"
    assert hexec.approve(moved.proposal_id, who=ready.owner, db=ready.db).status == "filled"
    assert {x["account_id"] for x in rows(ready, "sim_orders")} == {other}


def test_only_the_owner_chooses_and_only_an_account_of_the_desk(ready):
    other = add_account(ready)
    with ready.db.write() as u:
        stranger = u.broker.create_account(ready.broker_id, "Sim C", 1000).id
    levels.set_level(ready.desk_id, 2, who=ready.owner, db=ready.db)
    out = hexec.submit(with_strategy(ready, "carry"), who=AGENT, db=ready.db)
    with pytest.raises(service.BrokerError) as e:
        approvals.choose_account(out.proposal_id, other, who=AGENT, db=ready.db)
    assert e.value.code == "owner_required"
    with pytest.raises(service.BrokerError) as e:
        approvals.choose_account(out.proposal_id, stranger, who=ready.owner, db=ready.db)
    assert e.value.code == "account_not_in_desk"
    assert rows(ready, "sim_orders") == []


def test_an_agent_cannot_write_the_account_in_the_proposal():
    body = {"line": "DT", "order_type": "market", "legs": [{"contract_id": NQ, "side": "buy", "effect": "open", "qty": "1"}], "account_id": 7}
    with pytest.raises(ProposalError):
        OrderProposal.from_dict(body, desk_id=1, origin="desk", created_by="desk-chief")


def test_the_principal_strategy_keeps_the_fingerprint_and_another_strategy_changes_it():
    from exec_support import Leg
    legs = (Leg(NQ, "buy", "open", Decimal("1")),)
    base = OrderProposal(1, "DT", "desk", legs, "market")
    assert "strategy_key" not in base.canonical()
    assert OrderProposal(1, "DT", "desk", legs, "market", strategy_key="carry").fingerprint() != base.fingerprint()
    with pytest.raises(ProposalError):
        OrderProposal(1, "DT", "desk", legs, "market", strategy_key="")


def test_resolve_account_states_and_the_gate(ready):
    with ready.db.read() as u:
        assert service.resolve_account(u, ready.desk_id).state == "ok"
    other = add_account(ready)
    with ready.db.read() as u:
        found = service.resolve_account(u, ready.desk_id, "carry")
        assert (found.state, found.account, set(found.accounts)) == ("needs_account", None, {ready.account_id, other})
        assert service.resolve_account(u, ready.desk_id, "carry", other).account.id == other
        with pytest.raises(service.BrokerError) as e:
            service.resolve_account(u, ready.desk_id, "carry", 99999)
        assert e.value.code == "account_not_in_desk"
        with pytest.raises(service.BrokerError):
            service.broker_of(u, ready.desk_id, 99999)
    with ready.db.write() as u:
        unlink_all(u, ready.desk_id)
    with ready.db.read() as u:
        assert service.resolve_account(u, ready.desk_id).state == "analysis_only"
    assert rules.gate(True, "active", True, "needs_account")["code"] == "needs_account"
    assert rules.gate(True, "active", True) is None


def test_the_lock_is_per_account_and_the_dispatch_holds_it(ready):
    assert submit_module.account_lock(1) is submit_module.account_lock(1) and submit_module.account_lock(1) is not submit_module.account_lock(2)
    seen = {}

    class Probe:
        def place_order(self, account_id, order):
            result = []
            t = threading.Thread(target=lambda: result.append(submit_module.account_lock(account_id).acquire(blocking=False)))
            t.start(); t.join()
            seen["free_for_others"] = result[0]
            return {"order_id": "1", "status": "WORKING"}

    submit_module.dispatch(Probe(), 41, "place", {})
    assert seen["free_for_others"] is False                # another thread could not take the account while the order was going out
    assert submit_module.account_lock(41).acquire(blocking=False)       # and it is free afterwards
    submit_module.account_lock(41).release()


def test_the_native_check_and_send_of_two_desks_on_one_account_never_interleave(ready, monkeypatch):
    """SPEC-027 5: service._run takes the account lock around the prefilter check and the send, so a second desk checks what the first sent."""
    second = make_desk(ready.db, name="Second", capital=1_000_000)
    second_id = second["id"] if isinstance(second, dict) else second.id
    with ready.db.write() as u:
        u.broker.link(second_id, ready.account_id)
    log, guard = [], threading.Lock()
    real_check = service.exec_native.check

    def note(what):
        with guard:
            log.append((threading.current_thread().name, what))

    def check(*args, **kw):
        note("check_start")
        threading.Event().wait(0.15)                                   # a window wide enough for the other desk to enter if nothing stops it
        result = real_check(*args, **kw)
        note("check_end")
        return result

    def dispatch(broker, account_id, action, order=None, order_id=None):
        note("send")
        return {"order_id": "1", "correl_id": "c"}, 201, "sent"

    monkeypatch.setattr(service.exec_native, "check", check)
    monkeypatch.setattr(service, "exec_dispatch", dispatch)
    monkeypatch.setattr(service.SimBroker, "preview_order", lambda self, account_id, order: {})      # the steps under test are the check and the send
    monkeypatch.setattr(service.SimBroker, "positions", lambda self, account_id: [])
    monkeypatch.setattr(service.SimBroker, "orders", lambda self, account_id: [])
    monkeypatch.setattr(service.rules, "evaluate", lambda **kw: {"result": "allowed", "code": "", "flags": [], "numbers": {}, "why": ""})
    order = {"orderType": "LIMIT", "session": "NORMAL", "duration": "DAY", "orderStrategyType": "SINGLE", "price": "1.00",
             "orderLegCollection": [{"instruction": "BUY", "quantity": 1, "instrument": {"symbol": "SPY", "assetType": "EQUITY"}}]}
    errors = []

    def go(desk_id):
        try:
            errors.append(service.place(desk_id, order, "two desks", service.owner_who(), db=ready.db, account_id=ready.account_id))
        except Exception as error:                                     # the refusal is not the point: the order of the steps is
            errors.append(error)

    threads = [threading.Thread(target=go, args=(d,), name=f"desk-{d}") for d in (ready.desk_id, second_id)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    seen = [name for name, _ in log]
    if len(log) < 4:
        pytest.fail(str([repr(e)[:500] for e in errors])) and seen == sorted(seen, key=seen.index)      # every thread's steps are one block: the other desk waited for the lock
    for name in set(seen):
        mine = [i for i, (n, _) in enumerate(log) if n == name]
        assert mine == list(range(mine[0], mine[-1] + 1)), log


def test_a_trip_by_account_closes_the_account_for_every_desk_that_uses_it_and_pauses_none(ready):
    second = make_desk(ready.db, name="Second", capital=1000)
    second_id = second["id"] if isinstance(second, dict) else second.id
    third = make_desk(ready.db, name="Third", capital=1000)
    third_id = third["id"] if isinstance(third, dict) else third.id
    with ready.db.write() as u:
        u.broker.link(second_id, ready.account_id)                          # the account is shared by two desks
    killswitch.trip(ready.desk_id, "day loss", db=ready.db, account_id=ready.account_id)
    with ready.db.read() as u:
        assert {d: u.desks.find(d).status for d in (ready.desk_id, second_id, third_id)} == {ready.desk_id: "active", second_id: "active", third_id: "active"}
        from hokma.broker import day_close

        assert day_close.is_closed(u, ready.account_id)                 # SPEC-026, 3390c: the stop is the ACCOUNT's (it stops both desks in it, the desks stay active)
    assert rows(ready, "sim_fills") == []                                      # nothing was closed or sent


def test_the_killswitch_reads_every_account_of_the_desk_and_keeps_the_worst(ready, monkeypatch):
    other = add_account(ready)
    with ready.db.write() as u:
        u.broker.set_limits(ready.desk_id, {"max_drawdown_pct": 10})
    levels_by_account = {ready.account_id: killswitch.monitor.Level("ok", "", measured=True), other: killswitch.monitor.Level("halt", "too deep", measured=True)}
    monkeypatch.setattr(killswitch, "_check_account", lambda desk_id, account, limits, database: levels_by_account[account.id])
    account_id, level = killswitch.check_desk_accounts(ready.desk_id, db=ready.db)
    assert (account_id, level.name, level.measured) == (other, "halt", True)
    assert killswitch.check_desk(ready.desk_id, db=ready.db).name == "halt"
    with ready.db.write() as u:
        unlink_all(u, ready.desk_id)
    assert killswitch.check_desk_accounts(ready.desk_id, db=ready.db)[1].name == "ok"      # no account: nothing to stop


def test_the_new_messages_exist_in_both_languages_and_name_no_provider():
    import pathlib
    import hokma
    folder = pathlib.Path(hokma.__file__).parent / "catalog"
    for lang in ("es", "en"):
        data = json.loads((folder / f"{lang}.json").read_text(encoding="utf-8"))
        for key in ("broker.err.needs_account", "broker.err.account_not_in_desk", "exec.why.needs_account", "exec.why.analysis_only_no_account"):
            assert data[key] and "schwab" not in data[key].lower() and "\u2014" not in data[key]

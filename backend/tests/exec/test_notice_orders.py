"""The notice mode with buttons (SPEC-026, piece 3390b, task 3390): a rule in notice mode that trips leaves the order pending; the owner answers Follow, No or
Close in the chat or in Telegram; Follow asks the scope every time; the wait is the one of the rule and an unanswered order is NOT executed; "the rest of the
day" is a temporary permission that ends with the session and never edits the rule; nobody answers for the owner and a Telegram token is used once."""
import asyncio
import datetime as dt
import sqlite3
import time
from types import SimpleNamespace

import pytest
from exec_support import AGENT, NQ, SPY, db, desks_db, env, proposal, ready  # noqa: F401

from hokma import exec as hexec
from hokma.broker import service
from hokma.broker.service import BrokerError, Who
from hokma.db import db_path, migrate
from hokma.db.downgrade import rollback_0029, rollback_0030, rollback_0031, rollback_0032, rollback_0033
from hokma.db.timeutil import utc_in
from hokma.exec import approvals, levels, notices, telegram_approval

SIZE_NOTICE = {"funding": {"max_contracts": 1}, "rule_modes": {"size_contracts": {"mode": "notice", "timeout_min": 7}}}
LONG_AGO = "2000-01-01 00:00:00"


def _limits(r, cfg):
    with r.db.write() as u:
        u.broker.set_limits(r.desk_id, cfg)


def _pending(r, qty="2"):
    """An order of 2 contracts where the limit is 1 and the rule is a notice: it waits."""
    levels.set_level(r.desk_id, 2, who=r.owner, db=r.db)
    _limits(r, SIZE_NOTICE)
    out = hexec.submit(proposal(r, qty=qty), who=AGENT, db=r.db)
    assert out.status == "awaiting_owner", out
    return out


def _row(r, pid):
    return approvals.proposal_row(pid, db=r.db)


def _positions(r):
    with r.db.read() as u:
        broker, account = service.broker_of(u, r.desk_id, r.account_id)
        return [p for p in broker.positions(account.id) if p.qty != 0]


def _make_late(r, pid):
    with r.db.write() as u:
        u.session.execute("UPDATE exec_proposals SET expires_at = :t WHERE id = :i", {"t": LONG_AGO, "i": pid})


@pytest.fixture
def events():
    got = []
    notices.set_publisher(got.append)
    yield got
    notices.set_publisher(None)
    for pid in list(notices._timers):
        notices.disarm(pid)


# ---- the order waits with what the owner needs to answer ----------------------------------------------------------------------------------

def test_a_tripped_notice_leaves_the_order_pending_with_its_rule_its_own_deadline_a_token_and_a_card(ready, events):
    out = _pending(ready)
    row = _row(ready, out.proposal_id)
    notice = notices.notice_of(row)
    assert notice["rules"] == ["size_contracts"] and notice["timeout_min"] == 7 and notice["stage"] == "ask" and len(row["notice_nonce"]) == 12
    left = (dt.datetime.fromisoformat(row["expires_at"]).replace(tzinfo=dt.UTC) - dt.datetime.now(dt.UTC)).total_seconds()
    assert 6 * 60 < left <= 7 * 60 + 1
    card = next(e for e in events if e["type"] == "order_notice")
    assert card["proposal_id"] == out.proposal_id and card["rules"] == ["size_contracts"] and card["timeout_min"] == 7
    assert "nonce" not in card and "notice_nonce" not in card                                   # the chat answers through the app: the token is only for Telegram
    listed = approvals.list_pending(ready.desk_id, db=ready.db)[0]
    assert listed["notice"]["rules"] == ["size_contracts"] and listed["expires_at"] == row["expires_at"]
    assert [c["proposal_id"] for c in notices.pending_cards(ready.db)] == [out.proposal_id]


def test_the_default_wait_is_30_minutes_when_the_rule_gives_none(ready, events):
    levels.set_level(ready.desk_id, 2, who=ready.owner, db=ready.db)
    _limits(ready, {"funding": {"max_contracts": 1}, "rule_modes": {"size_contracts": {"mode": "notice"}}})
    out = hexec.submit(proposal(ready, qty="2"), who=AGENT, db=ready.db)
    assert notices.notice_of(_row(ready, out.proposal_id))["timeout_min"] == 30


# ---- Follow asks the scope every time ------------------------------------------------------------------------------------------------------

def test_follow_is_not_approved_without_a_scope_and_only_this_order_asks_again_on_the_next(ready, events):
    first = _pending(ready)
    with pytest.raises(BrokerError) as e:
        approvals.approve(first.proposal_id, who=ready.owner, db=ready.db)
    assert e.value.code == "scope_required" and e.value.status == 409
    with pytest.raises(BrokerError):
        approvals.approve(first.proposal_id, who=ready.owner, db=ready.db, scope="forever")
    assert _row(ready, first.proposal_id)["status"] == "awaiting_owner"
    assert approvals.approve(first.proposal_id, who=ready.owner, db=ready.db, scope="order").status in ("filled", "sent")
    again = hexec.submit(proposal(ready, qty="2"), who=AGENT, db=ready.db)
    assert notices.notice_of(_row(ready, again.proposal_id)) is not None                       # A19: with "only this order" the notice jumps on the next one
    with ready.db.read() as u:
        assert u.session.query("SELECT id FROM notice_grants") == []
    assert {"type": "order_notice_closed", "proposal_id": first.proposal_id, "outcome": "followed"} in events


def test_the_rest_of_the_day_is_a_recorded_temporary_permission_that_never_edits_the_rule(ready, events):
    first = _pending(ready)
    with ready.db.read() as u:
        limits_before = u.broker.get_limits(ready.desk_id)
    assert approvals.approve(first.proposal_id, who=ready.owner, db=ready.db, scope="day").status in ("filled", "sent")
    with ready.db.read() as u:
        grants = u.session.query("SELECT * FROM notice_grants")
        assert u.broker.get_limits(ready.desk_id) == limits_before                              # the rule is exactly as it was
    assert len(grants) == 1 and grants[0]["rule_id"] == "size_contracts" and grants[0]["granted_by"] == "owner" and grants[0]["account_id"] == ready.account_id
    assert grants[0]["proposal_id"] == first.proposal_id and grants[0]["ended_at"] is None
    ends = dt.datetime.fromisoformat(grants[0]["expires_at"]).replace(tzinfo=dt.UTC)
    assert dt.datetime.now(dt.UTC) < ends <= dt.datetime.now(dt.UTC) + dt.timedelta(days=1, hours=1)
    nxt = hexec.submit(proposal(ready, qty="2"), who=AGENT, db=ready.db)
    assert notices.notice_of(_row(ready, nxt.proposal_id)) is None                              # A19: the notice rule did not jump again (level 2 still asks to send)
    with ready.db.write() as u:                                                                  # when the session ends the permission ends with it
        u.session.execute("UPDATE notice_grants SET expires_at = :t", {"t": LONG_AGO})
    after = hexec.submit(proposal(ready, qty="2"), who=AGENT, db=ready.db)
    assert notices.notice_of(_row(ready, after.proposal_id)) is not None


def test_a_grant_covers_only_its_rule_account_and_desk_and_never_a_restriction(ready):
    out = _pending(ready)
    approvals.approve(out.proposal_id, who=ready.owner, db=ready.db, scope="day")
    with ready.db.read() as u:
        assert notices.covered(u, ready.desk_id, ready.account_id, ["size_contracts"]) is True
        assert notices.covered(u, ready.desk_id, ready.account_id, ["size_contracts", "daily_loss"]) is False      # every rule that tripped must be covered
        assert notices.covered(u, ready.desk_id + 1, ready.account_id, ["size_contracts"]) is False
        assert notices.covered(u, ready.desk_id, ready.account_id + 1, ["size_contracts"]) is False
        assert notices.covered(u, ready.desk_id, ready.account_id, []) is False
    _limits(ready, {"funding": {"max_contracts": 1}, "rule_modes": {"size_contracts": {"mode": "restriction"}}})       # the owner made it a restriction
    assert hexec.submit(proposal(ready, qty="2"), who=AGENT, db=ready.db).status == "refused"             # the grant does not soften a restriction


def test_the_session_end_is_the_next_close_of_the_market_of_the_order():
    now = dt.datetime(2026, 11, 25, 14, 0, tzinfo=dt.UTC)
    assert notices.session_end(["FUT:NQ:2026-12"], now) == dt.datetime(2026, 11, 25, 23, 0, tzinfo=dt.UTC)          # 17:00 in Chicago (UTC-6)
    assert notices.session_end(["EQ:SPY"], dt.datetime(2026, 11, 25, 16, 0, tzinfo=dt.UTC)) == dt.datetime(2026, 11, 25, 21, 0, tzinfo=dt.UTC)      # 11:00 in New York, in session: 16:00 (UTC-5)
    friday_close = dt.datetime(2026, 11, 27, 22, 0, tzinfo=dt.UTC)                                                # Friday 17:00 in New York: closed
    assert notices.session_end(["EQ:SPY"], friday_close) == dt.datetime(2026, 11, 30, 14, 30, tzinfo=dt.UTC)       # the permission ends when Monday opens, 09:30
    assert notices.session_end(["EQ:SPY"], dt.datetime(2026, 11, 25, 22, 0, tzinfo=dt.UTC)) == dt.datetime(2026, 11, 26, 14, 30, tzinfo=dt.UTC)      # after the close of a weekday
    assert notices.session_end(["FUT:NQ:2026-12"], dt.datetime(2026, 11, 27, 23, 30, tzinfo=dt.UTC)) == dt.datetime(2026, 11, 30, 0, 0, tzinfo=dt.UTC)   # Friday after 17:00 Chicago: Sunday 18:00
    assert notices.session_end(["FUT:NQ:2026-12"], dt.datetime(2026, 11, 26, 2, 0, tzinfo=dt.UTC)) == dt.datetime(2026, 11, 26, 23, 0, tzinfo=dt.UTC)     # the evening session runs to the next 17:00
    assert notices.session_end(["EQ:SPY"], dt.datetime(2026, 11, 23, 13, 0, tzinfo=dt.UTC)) == dt.datetime(2026, 11, 23, 14, 30, tzinfo=dt.UTC)    # Monday 08:00 NY, before the open: ends at 09:30, not 16:00
    assert notices.session_end(["EQ:SPY"], dt.datetime(2026, 11, 23, 14, 30, tzinfo=dt.UTC)) == dt.datetime(2026, 11, 23, 21, 0, tzinfo=dt.UTC)    # at the open itself it is the session
    late = dt.datetime(2026, 11, 25, 23, 30, tzinfo=dt.UTC)
    assert notices.session_end(["FUT:NQ:2026-12"], late) == dt.datetime(2026, 11, 26, 0, 0, tzinfo=dt.UTC)          # in the break of 17:00 to 18:00 it ends when the session opens


# ---- No and Close -----------------------------------------------------------------------------------------------------------------------------------

def test_no_rejects_and_the_order_is_never_sent(ready, events):
    out = _pending(ready)
    assert approvals.reject(out.proposal_id, who=ready.owner, db=ready.db).status == "canceled"
    assert _row(ready, out.proposal_id)["status"] == "canceled" and not _positions(ready)
    assert {"type": "order_notice_closed", "proposal_id": out.proposal_id, "outcome": "no"} in events
    with pytest.raises(BrokerError) as e:
        approvals.approve(out.proposal_id, who=ready.owner, db=ready.db, scope="order")
    assert e.value.code == "decision_closed"


def _hold(r, desk_id, contract, qty):
    """A position the owner approved for a desk, so the book of the account knows which desk holds it."""
    from hokma.exec.proposal import Leg, OrderProposal
    from decimal import Decimal as D

    levels.set_level(desk_id, 2, who=r.owner, db=r.db)
    p = OrderProposal(desk_id=desk_id, line="DT", origin="desk", legs=(Leg(contract, "buy", "open", D(qty)),), order_type="market", thesis="test", created_by="desk-chief",
                      stop_loss=D(100) if contract == NQ else None)          # a future needs its stop since 3390e3
    out = hexec.submit(p, who=AGENT, db=r.db)
    assert approvals.approve(out.proposal_id, who=r.owner, db=r.db).status in ("filled", "sent"), out


def _shared_account(r):
    """Desk 1 holds 1 NQ and 10 SPY long; a second desk of the same account holds 1 NQ. Then a notice is waiting on desk 1."""
    from account_links_support import link_principal
    from desks_support import make_desk

    other = make_desk(r.db, name="Other desk", capital=1_000_000)
    other_id = other["id"] if isinstance(other, dict) else other.id
    with r.db.write() as u:
        link_principal(u, other_id, r.account_id)
        u.session.insert_row("desk_profile", {"desk_id": other_id, "line": "DT"})
    _hold(r, r.desk_id, NQ, "1")
    _hold(r, r.desk_id, SPY, "10")
    _hold(r, other_id, NQ, "1")
    return _pending(r)


def _held(r):
    return {p.contract_id: p.qty for p in _positions(r)}


def test_close_does_not_close_anything_without_the_second_confirmation(ready, events):
    out = _shared_account(ready)
    before = _held(ready)
    assert before == {NQ: 2, SPY: 10}
    with pytest.raises(BrokerError) as e:
        approvals.close_account(out.proposal_id, who=ready.owner, db=ready.db)
    assert e.value.code == "close_not_confirmed" and e.value.status == 409
    assert _row(ready, out.proposal_id)["status"] == "awaiting_owner" and _held(ready) == before
    asked = approvals.close_ask(out.proposal_id, who=ready.owner, db=ready.db)                   # the first step only shows the list
    assert asked["stage"] == "close" and _held(ready) == before and _row(ready, out.proposal_id)["status"] == "awaiting_owner"
    assert SPY in asked["text"] and NQ in asked["text"]
    with pytest.raises(BrokerError) as e:
        approvals.close_account(out.proposal_id, who=ready.owner, db=ready.db, plan="0" * 12)       # a list that is not the one shown
    assert e.value.code == "stale_plan" and _held(ready) == before and _row(ready, out.proposal_id)["status"] == "awaiting_owner"


def test_the_default_close_sells_neither_the_long_shares_nor_what_another_desk_holds(ready, events):
    out = _shared_account(ready)
    asked = approvals.close_ask(out.proposal_id, who=ready.owner, db=ready.db)
    plan = asked["plan"]
    assert plan["all"] is False
    assert [(x["contract_id"], x["qty"], x["desks"]) for x in plan["close"]] == [(NQ, "1", ["NQ desk"])]
    assert {(x["contract_id"], x["why"]) for x in plan["keep"]} == {(SPY, "long_holding"), (NQ, "other_desk")}
    assert approvals.close_account(out.proposal_id, who=ready.owner, db=ready.db, plan=asked["plan_token"]) == "rejected_flat"
    assert _row(ready, out.proposal_id)["status"] == "canceled"
    assert _held(ready) == {NQ: 1, SPY: 10}                                                      # the other desk's lot and the shares are still there


def test_a_rule_that_closes_everything_closes_it_and_the_confirmation_says_so(ready, events):
    out = _shared_account(ready)
    _limits(ready, {"funding": {"max_contracts": 1, "daily_loss_usd": 5000},
                    "rule_modes": {"size_contracts": {"mode": "notice", "timeout_min": 7}, "daily_loss": {"mode": "restriction", "on_trip": "block_and_flatten_all"}}})
    asked = approvals.close_ask(out.proposal_id, who=ready.owner, db=ready.db)
    plan = asked["plan"]
    assert plan["all"] is True and plan["keep"] == []
    assert {(x["contract_id"], x["qty"]) for x in plan["close"]} == {(NQ, "2"), (SPY, "10")}
    assert any("Other desk" in x["desks"] for x in plan["close"] if x["contract_id"] == NQ)       # it shows whose lots go
    assert approvals.close_account(out.proposal_id, who=ready.owner, db=ready.db, plan=asked["plan_token"]) == "rejected_flat"
    assert _held(ready) == {}


def test_the_telegram_close_asks_first_on_a_new_token_and_each_button_only_works_in_its_step(ready):
    out = _shared_account(ready)
    pid, nonce = out.proposal_id, _row(ready, out.proposal_id)["notice_nonce"]

    def tap(choice, token):
        return telegram_approval.answer_notice(notices.callback_data(choice, pid, token), db=ready.db)

    assert tap("x", nonce).code == "wrong_stage" and _held(ready) == {NQ: 2, SPY: 10}              # confirm before the list was shown
    first = tap("c", nonce)
    assert first.code == "close_confirm" and first.ask_close and first.nonce and first.nonce != nonce and SPY in first.text
    assert _held(ready) == {NQ: 2, SPY: 10} and _row(ready, pid)["status"] == "awaiting_owner"
    assert tap("c", nonce).code == "stale" and tap("x", nonce).code == "stale"                    # the old token is spent
    assert tap("f", first.nonce).code == "wrong_stage" and tap("d", first.nonce).code == "wrong_stage"
    assert tap("x", first.nonce).code == "rejected_flat"
    assert _held(ready) == {NQ: 1, SPY: 10}
    assert tap("x", first.nonce).code == "stale"


def test_close_is_not_offered_in_the_scope_step_and_follow_is_not_taken_in_the_close_step(ready):
    out = _pending(ready)
    pid, nonce = out.proposal_id, _row(ready, out.proposal_id)["notice_nonce"]
    scope = telegram_approval.answer_notice(notices.callback_data("f", pid, nonce), db=ready.db)
    assert telegram_approval.answer_notice(notices.callback_data("c", pid, scope.nonce), db=ready.db).code == "wrong_stage"
    assert telegram_approval.answer_notice(notices.callback_data("x", pid, scope.nonce), db=ready.db).code == "wrong_stage"
    with pytest.raises(BrokerError) as e:
        approvals.close_ask(pid, who=ready.owner, db=ready.db)
    assert e.value.code == "wrong_stage" and _row(ready, pid)["status"] == "awaiting_owner"


def test_the_close_buttons_and_the_answers_are_in_the_language_of_the_owner(ready):
    out = _pending(ready)
    nonce = _row(ready, out.proposal_id)["notice_nonce"]
    labels = ("Follow", "No", "Close", "Only this order", "The rest of the day", "Confirm close")
    _, kb = telegram_approval.build_notice_message(out.proposal_id, nonce, "list", labels, stage="close")
    assert [b.text for b in kb.inline_keyboard[0]] == ["Confirm close", "No"]
    from hokma.i18n import t

    for code in telegram_approval.ANSWER_CODES:
        assert t(f"telegram.notice_answer.{code}") != f"telegram.notice_answer.{code}"           # no code reaches the phone raw


def test_a_position_that_changed_between_seeing_the_list_and_confirming_closes_the_minimum_and_omits_what_is_gone(ready, events):
    from decimal import Decimal as D

    out = _shared_account(ready)
    _limits(ready, {"funding": {"max_contracts": 1, "daily_loss_usd": 5000},
                    "rule_modes": {"size_contracts": {"mode": "notice", "timeout_min": 7}, "daily_loss": {"mode": "restriction", "on_trip": "block_and_flatten_all"}}})
    asked = approvals.close_ask(out.proposal_id, who=ready.owner, db=ready.db)
    assert {(x["contract_id"], x["qty"]) for x in asked["plan"]["close"]} == {(NQ, "2"), (SPY, "10")}
    from hokma.exec import submit
    submit.flatten(ready.desk_id, reason="between", who=ready.owner, db=ready.db, account_id=ready.account_id, only={NQ: D(2), SPY: D(4)})      # NQ is gone, SPY is 6
    assert _held(ready) == {SPY: 6}
    assert approvals.close_account(out.proposal_id, who=ready.owner, db=ready.db, plan=asked["plan_token"]) == "rejected_flat"
    assert _held(ready) == {}                                                                    # it closed the 6 that were left, and nothing was sold short


def test_a_live_order_that_cannot_be_read_stops_the_confirmed_close_without_sending_anything(ready, events, monkeypatch):
    from hokma.exec import submit

    out = _shared_account(ready)
    asked = approvals.close_ask(out.proposal_id, who=ready.owner, db=ready.db)
    before = _held(ready)
    unreadable = SimpleNamespace(order_id="777", status="WORKING", contract_id=None, legs=({"symbol": "NOT A CONTRACT", "instruction": "SELL_TO_CLOSE", "quantity": 1},))
    monkeypatch.setattr(submit, "_live_orders", lambda broker, account_id: [unreadable])
    sent = []
    monkeypatch.setattr(submit, "dispatch", lambda *a, **k: sent.append((a, k)))
    assert approvals.close_account(out.proposal_id, who=ready.owner, db=ready.db, plan=asked["plan_token"]) == "rejected_flatten_failed"      # cancel_unconfirmed: told, not sent
    assert sent == [] and _held(ready) == before


def test_a_size_permission_covers_only_the_size_rule_that_tripped(ready):
    out = _pending(ready)
    approvals.approve(out.proposal_id, who=ready.owner, db=ready.db, scope="day")
    with ready.db.read() as u:
        assert notices.covered(u, ready.desk_id, ready.account_id, ["size_contracts"]) is True
        for other in ("size_premium", "size_margin", "size_weight"):
            assert notices.covered(u, ready.desk_id, ready.account_id, [other]) is False
        assert notices.covered(u, ready.desk_id, ready.account_id, ["size_contracts", "size_weight"]) is False


def test_the_notice_names_the_size_control_that_tripped_and_not_the_others_that_are_also_notices(ready):
    levels.set_level(ready.desk_id, 2, who=ready.owner, db=ready.db)
    _limits(ready, {"funding": {"max_contracts": 1}, "max_weight_pct": 0.01,
                    "rule_modes": {"size_contracts": {"mode": "notice"}, "size_weight": {"mode": "notice"}}})
    with ready.db.write() as u:
        u.session.execute("UPDATE desk_profile SET line = 'LP' WHERE desk_id = :d", {"d": ready.desk_id})
    out = hexec.submit(proposal(ready, contract=SPY, qty="10", line="LP"), who=AGENT, db=ready.db)
    assert out.status == "awaiting_owner"
    assert notices.notice_of(_row(ready, out.proposal_id))["rules"] == ["size_weight"]               # the contracts control is a notice too, but it did not trip


# ---- the wait ends: the order is NOT executed -------------------------------------------------------------------------------

def test_when_the_wait_of_the_rule_passes_the_order_expires_and_is_not_sent(ready, events):
    out = _pending(ready)
    _make_late(ready, out.proposal_id)
    assert approvals.list_pending(ready.desk_id, db=ready.db) == []                         # the sweep of the list expires it
    row = _row(ready, out.proposal_id)
    assert row["status"] == "expired" and not _positions(ready)
    with ready.db.read() as u:
        assert u.session.query("SELECT id FROM broker_orders WHERE broker_order_id IS NOT NULL") == []             # nothing reached the broker
    assert {"type": "order_notice_closed", "proposal_id": out.proposal_id, "outcome": "expired"} in events


def test_an_order_answered_late_is_refused_with_expired_and_grants_nothing(ready):
    out = _pending(ready)
    _make_late(ready, out.proposal_id)
    with pytest.raises(BrokerError) as e:
        approvals.approve(out.proposal_id, who=ready.owner, db=ready.db, scope="day")
    assert e.value.code == "decision_expired"
    assert _row(ready, out.proposal_id)["status"] == "expired" and not _positions(ready)
    with ready.db.read() as u:
        assert u.session.query("SELECT id FROM notice_grants") == []


def test_the_timer_of_the_process_expires_the_order_at_its_time_with_no_other_trigger(ready, events):
    out = _pending(ready)
    end = utc_in(1)
    with ready.db.write() as u:
        u.session.execute("UPDATE exec_proposals SET expires_at = :t WHERE id = :i", {"t": end, "i": out.proposal_id})
    notices.arm(out.proposal_id, end, ready.db)
    assert _row(ready, out.proposal_id)["status"] == "awaiting_owner"
    for _ in range(50):
        time.sleep(0.1)
        if _row(ready, out.proposal_id)["status"] == "expired":
            break
    assert _row(ready, out.proposal_id)["status"] == "expired"
    assert any(e["type"] == "order_notice_closed" and e["outcome"] == "expired" for e in events)


def test_a_restart_arms_the_timers_again_and_expires_the_late_ones(ready):
    late = _pending(ready)
    live = hexec.submit(proposal(ready, qty="4"), who=AGENT, db=ready.db)
    _make_late(ready, late.proposal_id)
    try:
        assert notices.rearm_all(ready.db) == 1
        assert _row(ready, late.proposal_id)["status"] == "expired" and _row(ready, live.proposal_id)["status"] == "awaiting_owner"
        assert live.proposal_id in notices._timers
    finally:
        notices.disarm(live.proposal_id)


# ---- nobody answers for the owner ----------------------------------------------------------------------------------------------------------------

def test_an_agent_cannot_follow_refuse_close_or_tap_a_button(ready):
    out = _pending(ready)
    nonce = _row(ready, out.proposal_id)["notice_nonce"]
    forged_owner = Who(agent="x", owner=True)                                                 # owner=True without the secret of the app is an agent
    for call in (lambda: approvals.approve(out.proposal_id, who=AGENT, db=ready.db, scope="day"),
                 lambda: approvals.reject(out.proposal_id, who=AGENT, db=ready.db),
                 lambda: approvals.close_account(out.proposal_id, who=AGENT, db=ready.db),
                 lambda: notices.tap(notices.callback_data("f", out.proposal_id, nonce), who=AGENT, db=ready.db),
                 lambda: approvals.approve(out.proposal_id, who=forged_owner, db=ready.db, scope="day")):
        with pytest.raises(BrokerError) as e:
            call()
        assert e.value.code == "owner_required" and e.value.status == 403
    assert _row(ready, out.proposal_id)["status"] == "awaiting_owner"
    with ready.db.read() as u:
        assert u.session.query("SELECT id FROM notice_grants") == []


def test_the_telegram_token_is_checked_rotated_and_spent_so_a_copy_of_a_button_does_nothing(ready, events):
    out = _pending(ready)
    pid, nonce = out.proposal_id, _row(ready, out.proposal_id)["notice_nonce"]

    def tap(choice, token):
        return telegram_approval.answer_notice(notices.callback_data(choice, pid, token), db=ready.db)

    assert tap("f", "0" * 12).code == "stale"                                                  # forged token
    assert tap("d", nonce).code == "wrong_stage"                                                # a scope answer before Follow was asked
    first = tap("f", nonce)
    assert first.code == "scope" and first.ask_scope and first.nonce and first.nonce != nonce
    assert tap("f", nonce).code == "stale"                                                     # the same Follow again: the token was spent
    assert tap("d", nonce).code == "stale"                                                     # and the old token never reaches the scope answer
    assert _row(ready, pid)["status"] == "awaiting_owner"
    done = tap("d", first.nonce)
    assert done.code in ("filled", "sent")
    assert tap("d", first.nonce).code == "stale"                                               # replayed after the order went: nothing happens
    assert tap("n", first.nonce).code == "stale"
    with ready.db.read() as u:
        assert len(u.session.query("SELECT id FROM notice_grants")) == 1


def test_the_telegram_close_and_no_buttons_answer_with_the_same_token_rule(ready):
    one, two = _pending(ready), None
    nonce = _row(ready, one.proposal_id)["notice_nonce"]
    assert telegram_approval.answer_notice(notices.callback_data("n", one.proposal_id, nonce), db=ready.db).code == "canceled"
    assert telegram_approval.answer_notice(notices.callback_data("n", one.proposal_id, nonce), db=ready.db).code == "stale"
    two = hexec.submit(proposal(ready, qty="2"), who=AGENT, db=ready.db)
    token = _row(ready, two.proposal_id)["notice_nonce"]
    asked = telegram_approval.answer_notice(notices.callback_data("c", two.proposal_id, token), db=ready.db)
    assert asked.code == "close_confirm"
    assert telegram_approval.answer_notice(notices.callback_data("x", two.proposal_id, asked.nonce), db=ready.db).code == "rejected_flat"
    assert telegram_approval.answer_notice("on:f:99999:" + "a" * 12, db=ready.db).code == "stale"
    assert telegram_approval.answer_notice("garbage", db=ready.db).code == "invalid"


def test_the_buttons_have_three_answers_then_the_scope_and_fit_the_limit_of_telegram(ready):
    out = _pending(ready)
    nonce = _row(ready, out.proposal_id)["notice_nonce"]
    labels = ("Follow", "No", "Close", "Only this order", "The rest of the day")
    message, kb = telegram_approval.build_notice_message(out.proposal_id, nonce, "a <b> & b", labels)
    assert [b.text for b in kb.inline_keyboard[0]] == ["Follow", "No", "Close"] and "&lt;b&gt;" in message
    _, scope_kb = telegram_approval.build_notice_message(out.proposal_id, nonce, "q", labels, stage="scope")
    assert [b.text for row in scope_kb.inline_keyboard for b in row] == ["Only this order", "The rest of the day", "No"]
    for button in (*kb.inline_keyboard[0], *[b for row in scope_kb.inline_keyboard for b in row]):
        assert len(button.callback_data.encode()) <= 64 and notices.parse_callback(button.callback_data) is not None
    assert notices.parse_callback("on:z:1:" + "a" * 12) is None and notices.parse_callback("on:f:1:zz") is None


def test_an_unauthorized_tap_of_a_notice_is_refused_before_anything_runs(ready):
    out = _pending(ready)
    nonce = _row(ready, out.proposal_id)["notice_nonce"]

    class Base:
        def __init__(self, *a, **k):
            self._t = lambda key, **kw: key

        async def _callback_authorized(self, query, ctx, text):
            return query.allowed

        def _callback_ctx(self, query):
            return {}

    ui = telegram_approval.make_ui_class(Base)()
    query = SimpleNamespace(data=notices.callback_data("f", out.proposal_id, nonce), allowed=False, answered=[])

    async def answer(text=None):
        query.answered.append(text)
    query.answer = answer
    asyncio.run(ui.handle_callback_query(SimpleNamespace(callback_query=query)))
    assert query.answered == [] and _row(ready, out.proposal_id)["notice_nonce"] == nonce         # the gate refused: the token was not even spent


def test_the_pending_api_path_also_asks_the_scope(ready):
    out = _pending(ready)
    decision_id = _row(ready, out.proposal_id)["decision_id"]
    with pytest.raises(BrokerError) as e:
        service.approve_pending(decision_id, who=ready.owner, db=ready.db)
    assert e.value.code == "scope_required"
    assert service.approve_pending(decision_id, who=ready.owner, db=ready.db, scope="order")["status"] == "sent"


# ---- the migration and its way back -----------------------------------------------------------------------------------------------------------------

def test_0029_goes_back_to_28_and_forward_again(env):
    migrate()
    assert rollback_0033() == 32 and rollback_0032() == 31 and rollback_0031() == 30 and rollback_0030() == 29           # the newer steps first: a way back is one version at a time
    assert rollback_0029() == 28
    con = sqlite3.connect(str(db_path()))
    cols = [r[1] for r in con.execute("PRAGMA table_info(exec_proposals)")]
    assert "notice_json" not in cols and "notice_nonce" not in cols and "expires_at" not in cols
    assert not con.execute("SELECT name FROM sqlite_master WHERE name = 'notice_grants'").fetchall()
    con.close()
    assert migrate().applied == [29, 30, 31, 32, 33]


def test_a_bad_row_does_not_leave_the_next_ones_without_their_timer(ready, monkeypatch):
    first = _pending(ready)
    second = hexec.submit(proposal(ready, qty="4"), who=AGENT, db=ready.db)
    notices.disarm(first.proposal_id)                                                           # a restart: the timers are gone
    notices.disarm(second.proposal_id)
    real = notices.arm

    def arm(pid, expires_at, db=None):
        if pid == first.proposal_id:
            raise ValueError("bad expires_at")
        return real(pid, expires_at, db)

    monkeypatch.setattr(notices, "arm", arm)
    try:
        assert notices.rearm_all(ready.db) == 1
        assert second.proposal_id in notices._timers and first.proposal_id not in notices._timers
    finally:
        notices.disarm(first.proposal_id)
        notices.disarm(second.proposal_id)


@pytest.mark.parametrize("code", ["cancel_unconfirmed", "stale", "wrong_stage", "not_a_notice"])
def test_the_codes_of_the_notice_have_their_text_in_both_languages_and_never_reach_http_as_a_500(code):
    from hokma.i18n import use_language

    texts = []
    for lang in ("es", "en"):
        with use_language(lang):
            texts.append(BrokerError(code, 409).message)
    assert all(texts) and texts[0] != texts[1] and code not in texts[0] and code not in texts[1]
    assert BrokerError("owner_only", 403).readable == "owner_only"                 # a code with no text of its own is the last resort, never a KeyError

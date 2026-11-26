"""The notice of a cap with no order in the way, with buttons (SPEC-026 v2, piece 3390e4, task 3390). A rule in NOTICE mode reaches its cap in the reading of the broker: the owner gets one
notice with the account, the rule, the broker's number, the cap and the time of the reading, and three answers in the chat and in Telegram (keep trading today, block entries today, close
positions in two steps). With no answer when the wait ends it does nothing else; a second reach after "keep trading" does not repeat; closes are never blocked."""
from __future__ import annotations

import re
from decimal import Decimal

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from desks_support import desks_db  # noqa: F401  (fixtures)
from exec_support import AGENT, NQ, SPY, proposal, ready  # noqa: F401  (fixtures)
from test_desk_rules import OWNER
from test_live_blockers import open_qty
from test_live_freeze import check, feed, fill, quote, START  # noqa: F401  (fixtures)

from hokma.broker import account_rules, day_close
from hokma.broker.service import BrokerError
from hokma.db.downgrade import rollback_0033
from hokma.exec import cap_notices, live, notices, prefilter, telegram_approval
from hokma.i18n import catalog, t, use_language

D = Decimal


@pytest.fixture
def wired(ready, feed, monkeypatch):
    """A notice rule of 500, two open positions, the chat's publisher, the Telegram sender and the text of the outbox, all recorded."""
    told: list[str] = []
    events: list[dict] = []
    sent: list[tuple] = []
    monkeypatch.setattr(live, "_notify", lambda database, text: told.append(text))
    monkeypatch.setattr(live, "notify_reached", lambda database, text: told.append(text) or True)
    notices.set_publisher(events.append)
    telegram_approval.set_cap_sender(lambda cap_id, nonce, text: sent.append((cap_id, nonce, text)))
    account_rules.set_rule(ready.desk_id, ready.account_id, "daily_loss", 500, OWNER, mode="notice", timeout_min=5, db=ready.db)
    fill(ready, ready.account_id, ready.desk_id, NQ, "buy", 1, 21000, "c1")
    fill(ready, ready.account_id, ready.desk_id, SPY, "buy", 10, 600, "c2", mult=1)
    quote(ready, NQ, "21000")
    quote(ready, SPY, "600")
    mon = live.start(ready.db)
    ready.told, ready.events, ready.sent, ready.mon = told, events, sent, mon
    yield ready
    live.stop()
    notices.set_publisher(None)
    telegram_approval.set_cap_sender(None)
    for cap_id in list(cap_notices._timers):
        cap_notices.disarm(cap_id)


def cross(r, feed, loss=500):
    feed.loss(r.account_id, loss)
    live.on_reading(r.account_id)


def rows(r):
    with r.db.read() as u:
        return u.session.query("SELECT * FROM cap_notices ORDER BY id")


def grants(r):
    with r.db.read() as u:
        return u.session.query("SELECT * FROM notice_grants ORDER BY id")


# ---- the notice: what it says, chat and Telegram with the three buttons ----------------------------------------------------------------------

def test_a_cap_reached_in_notice_mode_raises_one_notice_with_its_data_and_the_three_buttons_in_chat_and_telegram(wired, feed):
    r = wired
    cross(r, feed, 499)
    assert rows(r) == [] and r.sent == [] and r.events == []                                          # below the cap: nothing
    cross(r, feed, 520)
    (row,) = rows(r)
    assert row["status"] == "waiting" and row["stage"] == "ask" and row["timeout_min"] == 5 and len(row["nonce"]) == 12 and D(row["value"]) == D(520) and D(row["cap"]) == D(500)
    card = next(e for e in r.events if e["type"] == "cap_notice")                                     # the chat
    assert card["cap_id"] == row["id"] and card["value"] == "520.00" and card["cap"] == "500.00" and card["rule_id"].endswith("daily_loss") and "nonce" not in card
    assert card["read_at"] and card["expires_at"] == row["expires_at"] and card["account"]
    (cap_id, nonce, text) = r.sent[0]                                                                  # Telegram
    assert cap_id == row["id"] and nonce == row["nonce"]
    assert row["rule_id"] in text and "520.00" in text and "500.00" in text and card["read_at"] in text and card["account"] in text and "5 min" in text
    assert r.told == [text]                                                                            # and the text that waits in the outbox when there is no link
    labels = (t("telegram.cap_btn_follow"), t("telegram.cap_btn_block"), t("telegram.cap_btn_close"), t("telegram.cap_btn_confirm"), t("telegram.cap_btn_back"))
    _body, keyboard = telegram_approval.build_cap_message(cap_id, nonce, text, labels)
    buttons = [b for line in keyboard.inline_keyboard for b in line]
    assert [b.text for b in buttons] == list(labels[:3])
    assert [cap_notices.parse_callback(b.callback_data)[0] for b in buttons] == [cap_notices.FOLLOW, cap_notices.BLOCK, cap_notices.CLOSE]
    assert all(len(b.callback_data.encode()) <= 64 for b in buttons)
    assert cap_notices.pending_cards(r.db)[0]["cap_id"] == row["id"]
    with r.db.read() as u:
        assert not day_close.is_closed(u, r.account_id)                                                # raising it closes and blocks nothing
    assert open_qty(r) == {NQ: D(1), SPY: D(10)}


def test_it_is_raised_once_while_it_waits_even_if_the_number_falls_and_reaches_the_cap_again(wired, feed):
    cross(wired, feed, 520)
    cross(wired, feed, 700)
    wired.mon.tick()
    cross(wired, feed, 100)
    cross(wired, feed, 600)
    assert len(rows(wired)) == 1 and len(wired.sent) == 1


# ---- keep trading today ----------------------------------------------------------------------------------------------------------------------

def test_keep_trading_writes_the_permission_of_the_day_and_a_second_reach_does_not_repeat_but_a_higher_cap_does(wired, feed):
    r = wired
    cross(r, feed, 520)
    (row,) = rows(r)
    assert cap_notices.follow(row["id"], who=r.owner, db=r.db)["status"] == "followed"
    (grant,) = grants(r)
    assert grant["account_id"] == r.account_id and grant["desk_id"] == r.desk_id and grant["rule_id"] == "daily_loss" and grant["granted_by"] == "owner" and grant["ended_at"] is None
    assert grant["expires_at"] == row["session_ends_at"]
    assert rows(r)[0]["status"] == "followed" and any(e["type"] == "cap_notice_closed" and e["cap_id"] == row["id"] and e["outcome"] == "followed" for e in r.events)
    assert cap_notices.pending_cards(r.db) == []
    cross(r, feed, 100)                                                                                 # it falls below and reaches the cap again, in the same session
    cross(r, feed, 800)
    r.mon.tick()
    assert len(rows(r)) == 1 and len(r.sent) == 1                                                       # it does not warn again
    with r.db.read() as u:
        assert not day_close.is_closed(u, r.account_id)
    m = live.measure(r.account_id, db=r.db)
    ev = live.Eval(_rule_of(r), "reached", D(800), D(900), "", "2026-11-25 15:00:00")
    assert cap_notices.raise_notice(r.db, m, ev) is not None                                            # a higher cap that the number reaches is a new question
    assert len(rows(r)) == 2


def _rule_of(r):
    with r.db.read() as u:
        return live.rules_of_account(u, r.account_id, live.WATCHED_MODES)[0]


# ---- block entries today ---------------------------------------------------------------------------------------------------------------------

def test_block_entries_turns_the_session_into_a_restriction_that_only_blocks_entries_and_never_a_close(wired, feed):
    r = wired
    cross(r, feed, 520)
    (row,) = rows(r)
    assert cap_notices.block(row["id"], who=r.owner, db=r.db)["status"] == "blocked"
    with r.db.read() as u:
        state = day_close.state_of(u, r.account_id)
        assert day_close.is_closed(u, r.account_id) and state["rule_id"].endswith("daily_loss")
    result = check(r)
    assert result.verdict == "BLOCK" and result.blocked_by.code == "day_closed"                         # entries blocked
    closing = prefilter.run(r.desk_id, proposal(r, side="sell", effect="close"), db=r.db, skip=("monitor",))
    assert closing.blocked_by is None or closing.blocked_by.code != "day_closed"                        # closes always pass
    assert open_qty(r) == {NQ: D(1), SPY: D(10)}                                                        # nothing was closed
    assert rows(r)[0]["status"] == "blocked" and cap_notices.pending_cards(r.db) == []
    cross(r, feed, 900)
    assert len(rows(r)) == 1


def test_a_block_that_fails_leaves_the_notice_waiting_tells_the_owner_and_can_be_pressed_again(wired, feed, monkeypatch):
    r = wired
    cross(r, feed, 520)
    (row,) = rows(r)
    real = live.trip

    def broken(*args, **kwargs):
        raise RuntimeError("broker down")

    monkeypatch.setattr(live, "trip", broken)
    assert cap_notices.block(row["id"], who=r.owner, db=r.db)["status"] == "block_failed"
    (after,) = rows(r)
    assert after["status"] == "waiting" and after["outcome"] is None and after["nonce"] == row["nonce"]      # nothing answered: the buttons still work
    assert "broker down" in r.told[-1] and t("cap.notice.block_failed", reason="broker down") == r.told[-1]
    with r.db.read() as u:
        assert not day_close.is_closed(u, r.account_id)                                                  # the entries are open and the monitor does not think they are blocked
    assert [c["cap_id"] for c in cap_notices.pending_cards(r.db)] == [row["id"]]                         # the card stays in the chat
    assert not any(e.get("type") == "cap_notice_closed" for e in r.events)
    assert cap_notices.tap(cap_notices.callback_data("b", row["id"], row["nonce"]), who=r.owner, db=r.db).code == "block_failed"      # Telegram: the same token tries again
    monkeypatch.setattr(live, "trip", real)
    assert cap_notices.block(row["id"], who=r.owner, db=r.db)["status"] == "blocked"                      # the retry works
    assert rows(r)[0]["status"] == "blocked"
    with r.db.read() as u:
        assert day_close.is_closed(u, r.account_id)


def test_a_failed_block_that_nobody_retries_does_not_silence_the_notices_after_it_expires(wired, feed, monkeypatch):
    r = wired
    cross(r, feed, 520)
    (row,) = rows(r)
    monkeypatch.setattr(live, "trip", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    cap_notices.block(row["id"], who=r.owner, db=r.db)
    with r.db.write() as u:
        u.session.execute("UPDATE cap_notices SET expires_at = '2000-01-01 00:00:00' WHERE id = :i", {"i": row["id"]})
    assert cap_notices.expire_due(r.db) == 1
    cross(r, feed, 100)                                                                                   # the number falls below the cap and reaches it again
    cross(r, feed, 520)
    assert [x["status"] for x in rows(r)] == ["expired", "waiting"]                                       # a new notice, not a silence


# ---- a notice that fails is raised again at the next reading --------------------------------------------------------------------------------

def test_if_the_notice_cannot_be_written_the_next_reading_raises_it_again(wired, feed, monkeypatch):
    r = wired
    real = cap_notices.new_nonce
    monkeypatch.setattr(cap_notices, "new_nonce", lambda: "bad")                                         # the table refuses a token of another length: the insert fails
    cross(r, feed, 520)
    assert rows(r) == [] and r.sent == [] and r.events == []
    monkeypatch.setattr(cap_notices, "new_nonce", real)
    live.on_reading(r.account_id)
    (row,) = rows(r)
    assert row["status"] == "waiting" and [e["cap_id"] for e in r.events] == [row["id"]] and len(r.sent) == 1


def test_raise_notice_tells_a_failure_from_a_deliberate_suppression(wired, feed, monkeypatch):
    r = wired
    cross(r, feed, 520)
    m = r.mon.database
    with r.db.read() as u:
        rule = live.rules_of_account(u, r.account_id, live.WATCHED_MODES)[0]
    meas = live.measure(r.account_id, db=r.db)
    ev = live.Eval(rule, "reached", D(520), D(500), "", None)
    assert cap_notices.raise_notice(r.db, meas, ev) is None                                              # one already waits: suppressed on purpose
    monkeypatch.setattr(cap_notices, "expire_due", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")))
    assert cap_notices.raise_notice(r.db, meas, ev) == cap_notices.FAILED
    assert m is not None


def test_a_telegram_failure_alone_does_not_stop_the_notice_in_the_chat(wired, feed):
    r = wired
    telegram_approval.set_cap_sender(lambda *a: (_ for _ in ()).throw(RuntimeError("telegram down")))
    cross(r, feed, 520)
    (row,) = rows(r)
    assert row["status"] == "waiting" and [e["cap_id"] for e in r.events] == [row["id"]] and len(r.told) == 1
    cross(r, feed, 600)
    assert len(rows(r)) == 1                                                                              # it counted as raised: no second one


def test_if_no_channel_reaches_the_owner_the_notice_is_closed_as_undelivered_and_the_next_reading_raises_it(wired, feed, monkeypatch):
    r = wired
    def boom(*a, **k):
        raise RuntimeError("down")

    working = (live.notify_reached, telegram_approval.request_cap_buttons, notices.publish_reached)
    monkeypatch.setattr(live, "notify_reached", boom)
    monkeypatch.setattr(telegram_approval, "request_cap_buttons", boom)
    monkeypatch.setattr(notices, "publish_reached", boom)
    cross(r, feed, 520)
    (row,) = rows(r)
    assert row["status"] == "expired" and row["outcome"] == "undelivered" and cap_notices.pending_cards(r.db) == []
    monkeypatch.setattr(live, "notify_reached", working[0])
    monkeypatch.setattr(telegram_approval, "request_cap_buttons", working[1])
    monkeypatch.setattr(notices, "publish_reached", working[2])
    live.on_reading(r.account_id)
    assert [x["status"] for x in rows(r)] == ["expired", "waiting"] and len(r.events) == 1


# ---- the channels report real success: nothing here patches the channels of the notice (the real queue, the real sender hook and the real publish are what fail) ----------

@pytest.fixture
def real(ready, feed, monkeypatch):
    """The same rule and book as `wired`, but with the REAL outbox queue (a Telegram link exists), the real sender hook and the real publish: tests break them, not patch them."""
    account_rules.set_rule(ready.desk_id, ready.account_id, "daily_loss", 500, OWNER, mode="notice", timeout_min=5, db=ready.db)
    fill(ready, ready.account_id, ready.desk_id, NQ, "buy", 1, 21000, "c1")
    quote(ready, NQ, "21000")
    with ready.db.write() as u:
        u.session.execute("INSERT INTO telegram_link (id, chat_id) VALUES (1, '1')")
    ready.events = []
    notices.set_publisher(ready.events.append)
    telegram_approval.set_cap_sender(lambda *a: None)
    ready.mon = live.start(ready.db)
    yield ready
    live.stop()
    notices.set_publisher(None)
    telegram_approval.set_cap_sender(None)
    for cap_id in list(cap_notices._timers):
        cap_notices.disarm(cap_id)


def break_queue(r):
    with r.db.write() as u:
        u.session.execute("ALTER TABLE telegram_outbox RENAME TO telegram_outbox_broken")


def fix_queue(r):
    with r.db.write() as u:
        u.session.execute("ALTER TABLE telegram_outbox_broken RENAME TO telegram_outbox")


def queued(r):
    with r.db.read() as u:
        return u.session.query("SELECT * FROM telegram_outbox")


def test_with_the_real_queue_the_sender_and_the_chat_all_failing_the_notice_is_undelivered_and_the_next_reading_raises_it_again(real, feed):
    r = real
    break_queue(r)                                                                                         # the real outbox fails
    def down(*a):
        raise RuntimeError("telegram down")

    def chat_down(event):
        raise RuntimeError("chat down")

    telegram_approval.set_cap_sender(down)                                                                 # the real Telegram sender fails
    notices.set_publisher(chat_down)                                                                       # the real publish fails
    cross(r, feed, 520)
    (row,) = rows(r)
    assert row["status"] == "expired" and row["outcome"] == "undelivered" and cap_notices.pending_cards(r.db) == []
    fix_queue(r)
    notices.set_publisher(r.events.append)
    live.on_reading(r.account_id)                                                                          # the next reading
    assert [x["status"] for x in rows(r)] == ["expired", "waiting"] and len(r.events) == 1 and len(queued(r)) == 1


def test_the_real_chat_alone_is_enough_when_the_queue_and_telegram_fail_and_it_is_not_retried_in_a_loop(real, feed):
    r = real
    break_queue(r)
    telegram_approval.set_cap_sender(lambda *a: (_ for _ in ()).throw(RuntimeError("telegram down")))
    cross(r, feed, 520)
    (row,) = rows(r)
    assert row["status"] == "waiting" and [e["cap_id"] for e in r.events] == [row["id"]]
    live.on_reading(r.account_id)
    live.on_reading(r.account_id)
    assert len(rows(r)) == 1 and len(r.events) == 1                                                         # no second notice, no loop


def test_a_channel_that_is_not_set_up_does_not_count_nor_fail(real, feed):
    r = real
    telegram_approval.set_cap_sender(None)                                                                 # no sender: None, it does not count
    with r.db.write() as u:
        u.session.execute("DELETE FROM telegram_link")                                                     # no link: the queue says None as well
    cross(r, feed, 520)
    (row,) = rows(r)
    assert row["status"] == "waiting" and len(r.events) == 1                                               # only the chat counted, and it was enough


def test_the_channels_say_true_false_or_none(real):
    r = real
    assert telegram_approval.request_cap_buttons(1, "n", "t") is True
    telegram_approval.set_cap_sender(None)
    assert telegram_approval.request_cap_buttons(1, "n", "t") is None
    telegram_approval.set_cap_sender(lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    assert telegram_approval.request_cap_buttons(1, "n", "t") is False
    assert live.notify_reached(r.db, "hi") is True
    break_queue(r)
    assert live.notify_reached(r.db, "hi") is False
    notices.set_publisher(None)
    assert notices.publish_reached({"a": 1}) is None
    notices.set_publisher(lambda e: (_ for _ in ()).throw(RuntimeError("x")))
    assert notices.publish_reached({"a": 1}) is False


# ---- close positions: two steps ---------------------------------------------------------------------------------------------------------------

def test_close_positions_shows_the_list_first_closes_futures_only_and_never_the_shares_held_long(wired, feed):
    r = wired
    cross(r, feed, 520)
    (row,) = rows(r)
    asked = cap_notices.close_ask(row["id"], who=r.owner, db=r.db)
    assert open_qty(r) == {NQ: D(1), SPY: D(10)}                                                        # the first step closes nothing
    assert [x["contract_id"] for x in asked["plan"]["close"]] == [NQ] and [x["contract_id"] for x in asked["plan"]["keep"]] == [SPY] and asked["plan"]["keep"][0]["why"] == "long_holding"
    assert NQ in asked["text"] and SPY in asked["text"] and asked["nonce"] != row["nonce"]
    assert rows(r)[0]["stage"] == "close" and rows(r)[0]["status"] == "waiting"
    with pytest.raises(BrokerError) as e:
        cap_notices.close_confirm(row["id"], who=r.owner, db=r.db, plan="000000000000")                  # a token that is not the list that was shown
    assert e.value.code == "stale_plan" and open_qty(r) == {NQ: D(1), SPY: D(10)}
    assert cap_notices.close_confirm(row["id"], who=r.owner, db=r.db, plan=asked["plan_token"]) == "closed"
    assert open_qty(r) == {SPY: D(10)}                                                                  # the future closed, the shares stayed
    assert rows(r)[0]["status"] == "closed"
    with r.db.read() as u:
        assert not day_close.is_closed(u, r.account_id)                                                  # Close does not block by itself


def test_close_positions_sells_the_shares_only_when_the_rule_says_so(ready, feed, monkeypatch):
    monkeypatch.setattr(live, "_notify", lambda database, text: None)
    monkeypatch.setattr(live, "notify_reached", lambda database, text: True)
    account_rules.set_rule(ready.desk_id, ready.account_id, "daily_loss", 500, OWNER, mode="notice", db=ready.db)
    account_rules.set_rule(ready.desk_id, ready.account_id, "drawdown", 9000, OWNER, mode="restriction", on_trip="block_and_flatten_all", db=ready.db)          # the owner chose to close everything at that line
    fill(ready, ready.account_id, ready.desk_id, SPY, "buy", 10, 600, "a2", mult=1)
    quote(ready, SPY, "600")
    live.start(ready.db)
    try:
        cross(ready, feed, 520)
        (row,) = rows(ready)
        asked = cap_notices.close_ask(row["id"], who=ready.owner, db=ready.db)
        assert asked["plan"]["all"] is True and [x["contract_id"] for x in asked["plan"]["close"]] == [SPY] and asked["plan"]["keep"] == []
        assert cap_notices.close_confirm(row["id"], who=ready.owner, db=ready.db, plan=asked["plan_token"]) == "closed"
        assert open_qty(ready) == {}
    finally:
        live.stop()
        for cap_id in list(cap_notices._timers):
            cap_notices.disarm(cap_id)


def test_if_the_book_changed_between_the_list_and_the_confirmation_nothing_is_closed(wired, feed):
    r = wired
    cross(r, feed, 520)
    (row,) = rows(r)
    asked = cap_notices.close_ask(row["id"], who=r.owner, db=r.db)
    fill(r, r.account_id, r.desk_id, NQ, "buy", 1, 21010, "late")                                       # another position arrived after the owner read the list
    with pytest.raises(BrokerError) as e:
        cap_notices.close_confirm(row["id"], who=r.owner, db=r.db, plan=asked["plan_token"])
    assert e.value.code == "stale_plan" and e.value.text == t("cap.close.changed") and open_qty(r)[NQ] == D(2)
    assert rows(r)[0]["status"] == "waiting"
    went = cap_notices.back(row["id"], who=r.owner, db=r.db)                                            # and he can go back to the three answers
    assert went["stage"] == "ask" and rows(r)[0]["stage"] == "ask" and went["nonce"] != asked["nonce"]


def test_confirming_without_having_seen_the_list_closes_nothing(wired, feed):
    cross(wired, feed, 520)
    (row,) = rows(wired)
    with pytest.raises(BrokerError) as e:
        cap_notices.close_confirm(row["id"], who=wired.owner, db=wired.db)
    assert e.value.code == "close_not_confirmed" and open_qty(wired) == {NQ: D(1), SPY: D(10)}


# ---- no answer: it does nothing else ---------------------------------------------------------------------------------------------------------

def test_without_an_answer_when_the_wait_ends_the_notice_expires_and_closes_and_blocks_nothing(wired, feed):
    r = wired
    cross(r, feed, 520)
    (row,) = rows(r)
    with r.db.write() as u:
        u.session.execute("UPDATE cap_notices SET expires_at = '2000-01-01 00:00:00'")
    assert cap_notices.expire_due(r.db) == 1
    got = rows(r)[0]
    assert got["status"] == "expired" and got["outcome"] == "expired" and got["answered_by"] == "hokma" and got["nonce"] is None
    assert open_qty(r) == {NQ: D(1), SPY: D(10)} and grants(r) == []
    with r.db.read() as u:
        assert not day_close.is_closed(u, r.account_id)
    assert any(e["type"] == "cap_notice_closed" and e["outcome"] == "expired" for e in r.events) and cap_notices.pending_cards(r.db) == []
    assert row["rule_id"] in r.told[-1] and "expired" in r.told[-1].lower() or "venci" in r.told[-1].lower()
    for answer in (cap_notices.follow, cap_notices.block):                                               # a late tap answers nothing
        with pytest.raises(BrokerError) as e:
            answer(row["id"], who=r.owner, db=r.db)
        assert e.value.code == "decision_closed"
    assert open_qty(r) == {NQ: D(1), SPY: D(10)}


def test_a_late_answer_expires_the_notice_instead_of_acting(wired, feed):
    r = wired
    cross(r, feed, 520)
    (row,) = rows(r)
    with r.db.write() as u:
        u.session.execute("UPDATE cap_notices SET expires_at = '2000-01-01 00:00:00'")
    with pytest.raises(BrokerError) as e:
        cap_notices.block(row["id"], who=r.owner, db=r.db)
    assert e.value.code == "decision_expired" and rows(r)[0]["status"] == "expired"
    with r.db.read() as u:
        assert not day_close.is_closed(u, r.account_id)


def test_the_tick_sweeps_a_notice_nobody_answered_and_a_restart_sets_its_timer_again(wired, feed):
    r = wired
    cross(r, feed, 520)
    assert len(cap_notices._timers) == 1
    assert cap_notices.rearm_all(r.db) == 1 and len(cap_notices._timers) == 1
    with r.db.write() as u:
        u.session.execute("UPDATE cap_notices SET expires_at = '2000-01-01 00:00:00'")
    r.mon.tick()
    assert rows(r)[0]["status"] == "expired"


# ---- who can answer ---------------------------------------------------------------------------------------------------------------------------

def test_an_agent_cannot_answer_and_a_telegram_token_is_used_once(wired, feed):
    r = wired
    cross(r, feed, 520)
    (row,) = rows(r)
    for answer in (cap_notices.follow, cap_notices.block, cap_notices.close_ask):
        with pytest.raises(BrokerError) as e:
            answer(row["id"], who=AGENT, db=r.db)
        assert e.value.code == "owner_required" and e.value.status == 403
    assert rows(r)[0]["status"] == "waiting"
    assert cap_notices.tap(cap_notices.callback_data("s", row["id"], "0" * 12), who=r.owner, db=r.db).code == "stale"                # a copy with another token
    assert cap_notices.tap("oc:s:1", who=r.owner, db=r.db).code == "invalid"
    asked = cap_notices.tap(cap_notices.callback_data("c", row["id"], row["nonce"]), who=r.owner, db=r.db)
    assert asked.code == "close_confirm" and asked.stage == "close" and asked.nonce and NQ in asked.text
    assert cap_notices.tap(cap_notices.callback_data("c", row["id"], row["nonce"]), who=r.owner, db=r.db).code == "stale"           # the old button does nothing now
    assert cap_notices.tap(cap_notices.callback_data("s", row["id"], row["nonce"]), who=r.owner, db=r.db).code == "stale"
    back = cap_notices.tap(cap_notices.callback_data("k", row["id"], asked.nonce), who=r.owner, db=r.db)
    assert back.code == "back" and back.stage == "ask" and back.nonce and open_qty(r) == {NQ: D(1), SPY: D(10)}
    done = cap_notices.tap(cap_notices.callback_data("b", row["id"], back.nonce), who=r.owner, db=r.db)
    assert done.code == "blocked"
    assert cap_notices.tap(cap_notices.callback_data("b", row["id"], back.nonce), who=r.owner, db=r.db).code == "stale"


def test_the_telegram_confirm_closes_the_listed_positions(wired, feed):
    r = wired
    cross(r, feed, 520)
    (row,) = rows(r)
    asked = cap_notices.tap(cap_notices.callback_data("c", row["id"], row["nonce"]), who=r.owner, db=r.db)
    assert cap_notices.tap(cap_notices.callback_data("x", row["id"], asked.nonce), who=r.owner, db=r.db).code == "closed"
    assert open_qty(r) == {SPY: D(10)}


def test_the_routes_answer_for_the_owner_only_through_the_app(wired, feed):
    from hokma.broker import api

    r = wired
    cross(r, feed, 520)
    (row,) = rows(r)
    paths = {route.path for route in api.router.routes}
    for tail in ("", "/{cap_id}/follow", "/{cap_id}/block", "/{cap_id}/close", "/{cap_id}/close/confirm", "/{cap_id}/back"):
        assert "/api/orders/cap-notices" + tail in paths
    assert api.pending_cap_notices()["notices"][0]["cap_id"] == row["id"]


# ---- texts and the way back -------------------------------------------------------------------------------------------------------------------

def test_the_texts_are_in_both_languages_with_the_same_placeholders_and_no_long_dash():
    keys = [k for k in catalog("es") if k.startswith(("cap.", "telegram.cap_")) or k == "live.notice.reached"]
    assert len(keys) > 25
    for key in keys:
        es, en = catalog("es")[key], catalog("en")[key]
        assert set(re.findall(r"\{(\w+)\}", es)) == set(re.findall(r"\{(\w+)\}", en)) and "\u2014" not in es + en
    for code in sorted(telegram_approval.CAP_ANSWER_CODES | {"other"}):
        assert t(f"telegram.cap_answer.{code}") != f"telegram.cap_answer.{code}"                      # no code reaches the phone raw
    with use_language("es"):
        assert t("telegram.cap_btn_follow") == "Seguir operando hoy" and t("telegram.cap_btn_block") == "Bloquear entradas hoy" and t("telegram.cap_btn_close") == "Cerrar posiciones"
    with use_language("en"):
        assert t("telegram.cap_btn_follow") == "Keep trading today"


def test_0033_goes_back_to_32_and_forward_again(env, db):
    from hokma.db import db_path, migrate
    import sqlite3

    assert rollback_0033() == 32
    con = sqlite3.connect(str(db_path()))
    assert not con.execute("SELECT name FROM sqlite_master WHERE name = 'cap_notices'").fetchall()
    con.close()
    assert migrate().applied == [33]

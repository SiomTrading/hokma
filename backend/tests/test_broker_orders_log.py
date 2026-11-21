"""The record of every order attempt (task 3299, SPEC-015 18.6, criterion 30): preview, place, replace, cancel, the refused ones, the
ones that wait for the owner and the ones Schwab did not take all leave a row in broker_orders with the agent, the desk, the reason
and the answer; no row, journal entry, decision, approval, outbox text, log line or answer holds the number of the account or its
hash (the sentinels of the fake Schwab, which also come inside its answers); order_log returns the same rows."""
import json
import logging

import pytest
from broker_orders_support import (ACCOUNT, ORDERS, PREVIEW_PATH, SECRETS, account_body, calls, connections, created, desks_db,  # noqa: F401
                                   db, env, fake, keys, limit, link_telegram, order_body, pos, preview_body, ready, rows, server,
                                   set_capital, whole_database_text)

from hokma.broker import service
from hokma.broker.service import BrokerError, Who
from hokma.db import ValidationError
from hokma.kit import hokma_tools

AGENT = Who(agent="desk-chief", conversation_id=11, task_id=None)


def attempt(fn, *args, **kwargs):
    """Run a service call and return its answer or the BrokerError it raised."""
    try:
        return fn(*args, **kwargs)
    except BrokerError as error:
        return error


def every_kind_of_attempt(r):
    """One of each: preview, a sent order, a refused one (the switch), one that waits, a replace, a cancel, a failed send and a
    refusal of Schwab. Returns the answers in that order."""
    out = [service.preview(r.desk_id, limit(), AGENT, db=r.db),
           service.place(r.desk_id, limit(), "reason 1", AGENT, db=r.db)]
    r.fake.on("POST", PREVIEW_PATH, preview_body(rejects=["Insufficient funds"]))
    out.append(attempt(service.place, r.desk_id, limit(), "reason 2", AGENT, db=r.db))                 # Schwab says no
    r.fake.on("POST", PREVIEW_PATH, preview_body(value=50000.0))
    out.append(service.place(r.desk_id, limit(), "reason 3", AGENT, db=r.db))                          # over the capital: waits
    r.fake.on("POST", PREVIEW_PATH, preview_body())
    r.fake.on("GET", f"{ORDERS}/555", order_body(555))
    created(r.fake, 556, f"{ORDERS}/555", "PUT")
    out.append(service.replace(r.desk_id, 555, limit("BUY", 1, "11.00"), "reason 4", AGENT, db=r.db))
    r.fake.on("DELETE", f"{ORDERS}/556", (200, None))
    r.fake.on("GET", f"{ORDERS}/556", order_body(556))
    out.append(service.cancel(r.desk_id, 556, "reason 5", AGENT, db=r.db))
    r.fake.on("POST", ORDERS, (400, {"message": "no", "errors": ["x"]}))
    out.append(attempt(service.place, r.desk_id, limit(), "reason 6", AGENT, db=r.db))                 # the send fails
    service.set_orders_enabled(r.broker_id, False, db=r.db)
    out.append(attempt(service.place, r.desk_id, limit(), "reason 7", AGENT, db=r.db))                 # the switch is off
    return out


def test_every_attempt_leaves_a_row_with_agent_desk_reason_and_answer(ready):
    set_capital(ready, 5000)
    link_telegram(ready.db)
    every_kind_of_attempt(ready)
    log = list(reversed(rows(ready)))                                                                   # oldest first
    assert [(r.action, r.rule_result, r.reason) for r in log] == [
        ("preview", "auto", None), ("place", "auto", "reason 1"), ("place", "refused", "reason 2"),
        ("place", "needs_owner", "reason 3"), ("replace", "auto", "reason 4"), ("cancel", "auto", "reason 5"),
        ("place", "auto", "reason 6"), ("place", "refused", "reason 7")]
    assert all((r.desk_id, r.account_id, r.agent_slug, r.conversation_id, r.broker_kind) == (
        ready.desk_id, ready.account_id, "desk-chief", 11, "schwab") for r in log)
    preview, placed, rejected, waiting, replaced, canceled, failed, off = log
    assert (preview.http_status, preview.broker_order_id) == (None, None) and "preview" in preview.response_json
    assert (placed.http_status, placed.approved_by, placed.broker_order_id) == (201, "auto", "600000123")
    assert placed.request_json == limit() and placed.response_json["order_id"] == 600000123 and placed.correl_id == "test-correl-id"
    assert "Insufficient funds" in rejected.rule_why and rejected.response_json["preview"] and rejected.http_status is None
    assert waiting.decision_id is not None and waiting.approved_by is None and waiting.http_status is None
    assert waiting.response_json["rule"]["flags"] == ["capital"]
    assert (replaced.replaces_order_id, replaced.broker_order_id, replaced.http_status) == ("555", "556", 201)
    assert (canceled.broker_order_id, canceled.http_status, canceled.request_json) == ("556", 200, None)
    assert (failed.http_status, failed.broker_order_id) == (400, None) and failed.response_json["error"]["status"] == 400
    assert off.rule_why and off.request_json == limit() and off.http_status is None
    ids = [r.id for r in log]
    assert ids == sorted(ids) and all(r.created_at for r in log)


def test_every_attempt_that_is_an_action_leaves_an_entry_in_the_journal_of_the_desk_project(ready):
    set_capital(ready, 5000)
    every_kind_of_attempt(ready)
    with ready.db.read() as u:
        entries = list(reversed(u.journal.list(project_id=ready.project_id, limit=50)))
    assert [e.kind for e in entries] == ["action"] * 7                                                  # the preview is not an action
    assert [e.outcome for e in entries] == ["success", "failure", "info", "success", "success", "failure", "failure"]
    assert all(e.agent_slug == "desk-chief" and e.actor_kind == "agent" for e in entries)
    assert "BUY 1 AAPL LIMIT 10.00" in entries[0].title and "reason 1" in entries[0].body and "600000123" in entries[0].body


def test_no_secret_reaches_any_table_log_answer_or_notice(ready, caplog):                              # criterion 30
    set_capital(ready, 5000)
    link_telegram(ready.db)
    ready.fake.on("GET", ORDERS, [order_body(555)])                                                     # an order with the number in it
    ready.fake.on("GET", ACCOUNT, account_body([pos("AAPL", long=1, value=10.0)]))
    with caplog.at_level(logging.DEBUG):
        answers = every_kind_of_attempt(ready)
        answers.append(service.order_log(ready.desk_id, db=ready.db))
        answers.append(service.order_list(ready.desk_id, db=ready.db))
        answers.append(service.position_list(ready.desk_id, db=ready.db))
        answers.append(service.list_pending(ready.desk_id, db=ready.db))
    shown = json.dumps([a if not isinstance(a, BrokerError) else {"m": a.message, "c": a.code} for a in answers if a is not None],
                       default=str, ensure_ascii=False)
    stored = whole_database_text(ready.db)
    for secret in SECRETS:
        assert secret not in shown, f"{secret} in an answer"
        assert secret not in stored, f"{secret} in the database"
        assert secret not in caplog.text, f"{secret} in a log line"
    assert "A1" in shown and "••••5678" in shown                                                       # what is shown instead


def test_the_response_of_the_preview_is_saved_masked(ready):
    service.place(ready.desk_id, limit(), "t", AGENT, db=ready.db)
    row = rows(ready)[0]
    strategy = row.response_json["preview"]["orderStrategy"]
    assert strategy["account_ref"] == "A1" and strategy["mask"] == "••••5678" and "accountNumber" not in strategy


def test_the_record_refuses_a_body_that_still_has_an_account_number_or_a_hash(ready):
    for key in ("accountNumber", "hashValue"):
        with ready.db.write() as u:
            with pytest.raises(ValidationError):
                u.broker.record_order(broker_kind="schwab", action="place", desk_id=ready.desk_id, request={"x": [{key: "1"}]})
            with pytest.raises(ValidationError):
                u.broker.record_order(broker_kind="schwab", action="place", desk_id=ready.desk_id, response={key: "1"})


def test_order_log_returns_the_same_rows_newest_first_with_since_and_limit(ready):
    set_capital(ready, 5000)
    every_kind_of_attempt(ready)
    answer = service.order_log(ready.desk_id, db=ready.db)
    assert [o["action"] for o in answer["orders"]] == ["place", "place", "cancel", "replace", "place", "place", "place", "preview"]
    assert answer["orders"] == [r.to_dict() for r in rows(ready)]
    assert [o["id"] for o in answer["orders"]] == sorted((o["id"] for o in answer["orders"]), reverse=True)
    assert len(service.order_log(ready.desk_id, limit=3, db=ready.db)["orders"]) == 3
    assert service.order_log(ready.desk_id, since="2999-01-01", db=ready.db)["orders"] == []
    assert len(service.order_log(ready.desk_id, since="2000-01-01T00:00:00Z", db=ready.db)["orders"]) == 8
    with pytest.raises(BrokerError) as bad:
        service.order_log(ready.desk_id, since="not a date", db=ready.db)
    assert bad.value.status == 422
    with pytest.raises(BrokerError) as missing:
        service.order_log(999, db=ready.db)
    assert missing.value.code == "desk_not_found"


def test_the_tool_order_log_answers_what_the_service_answers_and_only_for_its_desk(ready):
    set_capital(ready, 5000)
    every_kind_of_attempt(ready)
    text, err = hokma_tools.call("order_log", {"desk_id": ready.desk_id, "limit": 4}, hokma_tools.Caller({"HOKMA_AGENT": "hokma"}))
    assert err is False
    assert json.loads(text) == json.loads(json.dumps(service.order_log(ready.desk_id, limit=4, db=ready.db), default=str))
    text, err = hokma_tools.call("order_log", {"desk_id": 999}, hokma_tools.Caller({"HOKMA_AGENT": "hokma"}))
    assert err is True and "LookupError" in text


def test_the_count_of_sent_orders_and_the_pending_list_come_from_the_same_table(ready):
    set_capital(ready, 5000)
    every_kind_of_attempt(ready)
    with ready.db.read() as u:
        assert u.broker.count_sent_orders(ready.desk_id, "2000-01-01 00:00:00") == 2                   # the sent place and the replace; the 400 and the refused ones do not count
        assert len(u.broker.pending_order_records(ready.desk_id)) == 1
        assert u.broker.order_record_for_decision(u.broker.pending_order_records(ready.desk_id)[0].decision_id).reason == "reason 3"

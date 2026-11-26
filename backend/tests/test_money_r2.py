"""Money, round 2 (task 3299, SPEC-015, bugs 1220 to 1224): the owner fixes the envelope of the money and the agents operate inside
it (A1), only the app can be the owner (A2), an order whose value cannot be worked out asks (B2), a 5xx while sending an order leaves
the outcome unknown (B3), an approved order that grew past 5% asks again (C2), the reason of Schwab reaches the agent (C4) and the
same pending order is not asked twice (C4b). Nothing touches the network: Schwab is the local fake."""
import dataclasses
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from broker_orders_support import (ORDERS, PREVIEW_PATH, calls, connections, created, db, desks_db, env, fake, keys, limit,  # noqa: F401
                                   link_telegram, order_body, outbox, pos, account_body, preview_body, ready, rows, sent, server,
                                   set_capital)

from hokma.broker import rules, service
from hokma.broker.service import BrokerError, Who
from hokma.desks import service as desks
from hokma.engines.direct import loop  # noqa: F401  (the consumer of modes.asks)
from hokma.i18n import t
from hokma.kit import hardblock, hokma_tools
from hokma.kit.hokma_tools import Caller
from hokma.permissions import modes
from hokma.schwab import reader
from hokma.schwab.http import BAD_REQUEST, UPSTREAM, SchwabError

AGENT = Who(agent="desk-chief", conversation_id=3, task_id=None)
OWNER = service.owner_who()
BACKEND = Path(__file__).resolve().parents[1]


def run(name, args, agent="hokma", **env_vars):
    text, is_error = hokma_tools.call(name, args, Caller({"HOKMA_AGENT": agent, **env_vars}))
    return (json.loads(text) if not is_error else text), is_error


def capital_of(r) -> int:
    return desks.get_desk(r.desk_id, r.db)["capital"]


def pending(r):
    with r.db.read() as u:
        return u.broker.pending_order_records(r.desk_id)


# ======================================================================================================= A1: the envelope

MONEY_TOOLS = ("desk_money_set", "desk_add_demo_account", "desk_add_account", "desk_remove_account", "desk_bind_strategy")


@pytest.mark.parametrize("name", MONEY_TOOLS)
@pytest.mark.parametrize("mode", ["ask", "auto"])
def test_the_tools_that_move_the_envelope_ask_the_owner_in_every_mode(name, mode):
    assert modes.asks(mode, name) and modes.asks(mode, f"mcp__hokma__{name}")
    assert f"mcp__hokma__{name}" in modes.ALWAYS_ASK and f"mcp__hokma__{name}" in hardblock.ALWAYS_ASK   # the Claude Code hook too


def test_the_ordinary_edit_of_a_desk_does_not_ask_in_auto_and_the_switch_has_no_tool_at_all():
    assert not modes.asks("auto", "desk_configure") and modes.asks("ask", "desk_configure")
    for name, entry in hokma_tools.TOOLS.items():                      # nothing an agent can call turns the orders of a broker on
        code = inspect.getsource(entry[3])
        assert "set_orders_enabled" not in code and "orders_enabled" not in code and "set_broker_config" not in code, name


def test_desk_configure_cannot_raise_the_capital_even_in_steps_and_the_capital_still_goes_down(ready):
    d = ready.desk_id
    assert capital_of(ready) == 10000
    for raised in (10001, 12000, 10**9):
        text, err = run("desk_configure", {"desk_id": d, "capital": raised})
        assert err and "raising the capital" in text and "owner" in text and capital_of(ready) == 10000
    lowered, err = run("desk_configure", {"desk_id": d, "capital": 6000})
    assert not err and lowered["capital"] == 6000
    for step in (6001, 8000, 10000):                                  # lower, then "put it back" in pieces: every raise is refused
        assert run("desk_configure", {"desk_id": d, "capital": step})[1] and capital_of(ready) == 6000
    assert not run("desk_configure", {"desk_id": d, "capital": 6000})[1]                          # the same value is no change


def test_a_raise_mixed_with_other_fields_changes_nothing(ready):
    d = ready.desk_id
    text, err = run("desk_configure", {"desk_id": d, "name": "Renombrada", "mandate": "otro", "capital": 20000,
                                       "limits": {"max_orders_per_day": 3}})
    assert err
    shown = desks.get_desk(d, ready.db)
    assert shown["name"] == "Futuros NQ" and shown["mandate"] != "otro" and shown["limits"] == {"guards": []} and shown["capital"] == 10000


def test_the_service_checks_against_the_saved_value_inside_the_transaction(ready):
    # what a race would do: the check of the tool saw an old value; the guard of the service compares with the one saved now
    with pytest.raises(BrokerError) as refused:
        desks.update_desk(ready.desk_id, {"capital": 10001}, db=ready.db, lower_only=True)
    assert refused.value.code == "owner_only" and refused.value.status == 403 and capital_of(ready) == 10000
    desks.update_desk(ready.desk_id, {"capital": 9000}, db=ready.db, lower_only=True)
    with pytest.raises(BrokerError):
        desks.update_desk(ready.desk_id, {"capital": 9500}, db=ready.db, lower_only=True)      # up from what is saved NOW
    assert desks.update_desk(ready.desk_id, {"capital": 9500}, db=ready.db)["capital"] == 9500   # the app of the owner may


@pytest.mark.parametrize("start,loosen", [
    ({"max_order_pct": 5}, {"max_order_pct": 6}), ({"max_order_pct": 5}, {"max_order_pct": None}),
    ({"max_daily_loss_pct": 2}, {"max_daily_loss_pct": 3}), ({"max_daily_loss_pct": 2}, {"max_daily_loss_pct": None}),
    ({"max_orders_per_day": 3}, {"max_orders_per_day": 4}), ({"max_orders_per_day": 3}, {"max_orders_per_day": None}),
    ({"assets_only": True}, {"assets_only": False}), ({"assets_only": True}, {"assets_only": None}),
    ({"options_allowed": False}, {"options_allowed": True}), ({"options_allowed": False}, {"options_allowed": None}),
    ({"short_allowed": False}, {"short_allowed": True}), ({"short_allowed": False}, {"short_allowed": None}),
    ({"extended_hours_allowed": False}, {"extended_hours_allowed": True}),
    ({"extended_hours_allowed": False}, {"extended_hours_allowed": None})])
def test_every_way_of_loosening_a_limit_is_refused_by_desk_configure_and_goes_through_the_tool_that_asks(ready, start, loosen):
    d = ready.desk_id
    service.set_limits(d, start, db=ready.db)                                   # the owner (app) set it
    saved = service.get_limits(d, db=ready.db)
    text, err = run("desk_configure", {"desk_id": d, "limits": loosen})
    assert err and "owner" in text and service.get_limits(d, db=ready.db) == saved
    with pytest.raises(BrokerError) as direct:                                  # also at the service, for any other caller
        service.set_limits(d, loosen, db=ready.db, tighten_only=True)
    assert direct.value.code == "owner_only"
    done, err = run("desk_money_set", {"desk_id": d, "limits": loosen})         # the only way, and it always asks
    assert not err and done["limits"] != saved


def test_tightening_a_limit_goes_through_without_the_owner(ready):
    d = ready.desk_id
    assert not run("desk_configure", {"desk_id": d, "limits": {"max_order_pct": 10, "short_allowed": False,
                                                                 "assets_only": True, "max_orders_per_day": 5}})[1]
    done, err = run("desk_configure", {"desk_id": d, "limits": {"max_order_pct": 4, "max_orders_per_day": 5,
                                                                 "options_allowed": False}})
    assert not err and done["limits"]["max_order_pct"] == 4.0 and done["limits"]["options_allowed"] is False
    assert rules.loosened_limits({"max_order_pct": 5}, {"max_order_pct": 5}) == []              # the same value is no change
    assert rules.loosened_limits({}, {"max_order_pct": 5}) == [] and rules.loosened_limits({"short_allowed": False}, {}) == [
        "short_allowed"]


def test_assets_are_a_limit_while_assets_only_is_on(ready):
    d = ready.desk_id
    desks.set_assets(d, ["MSFT"], db=ready.db)
    assert not run("desk_configure", {"desk_id": d, "assets": ["MSFT", "AAPL"]})[1]            # no limit yet: nothing to loosen
    service.set_limits(d, {"assets_only": True}, db=ready.db)
    text, err = run("desk_configure", {"desk_id": d, "assets": ["MSFT", "AAPL", "TSLA"]})
    assert err and "assets_only" in text
    assert not run("desk_configure", {"desk_id": d, "assets": ["AAPL"]})[1]                    # fewer assets is stricter
    assert [a["symbol"] for a in desks.get_desk(d, ready.db)["assets"]] == ["AAPL"]
    with pytest.raises(BrokerError):
        desks.set_assets(d, ["AAPL", "NVDA"], db=ready.db, keep_assets_only=True)
    assert not run("desk_money_set", {"desk_id": d, "assets": ["AAPL", "NVDA"]})[1]


def test_desk_money_set_takes_only_the_money_fields_and_something_to_change(ready):
    d = ready.desk_id
    assert "unknown argument" in run("desk_money_set", {"desk_id": d, "name": "x"})[0]          # the strict schema
    assert "nothing to change" in run("desk_money_set", {"desk_id": d})[0]
    raised, err = run("desk_money_set", {"desk_id": d, "capital": 20000})
    assert not err and raised["capital"] == 20000


def test_an_agent_in_auto_mode_that_gets_needs_owner_for_capital_cannot_raise_it_and_resend(ready):
    set_capital(ready, 1000)
    ready.fake.on("POST", PREVIEW_PATH, preview_body(value=1500.0))
    chief = {"agent": "desk-chief", "HOKMA_CONVERSATION_ID": str(desks.open_conversation(ready.desk_id)["conversation_id"])}
    order = {"desk_id": ready.desk_id, "order": limit("BUY", 150, "10.00"), "reason": "idea"}
    held, err = run("order_place", order, **chief)
    assert not err and held["status"] == "needs_owner" and "1,500.00" in held["why"]
    text, err = run("desk_configure", {"desk_id": ready.desk_id, "capital": 5000}, **chief)      # the way out the agent would try
    assert err and capital_of(ready) == 1000
    assert modes.asks("auto", "desk_money_set")                                                  # the only way asks, in auto too
    again, err = run("order_place", order, **chief)
    assert not err and again["status"] == "needs_owner" and sent(ready.fake) == []


def test_the_state_of_a_desk_is_not_something_a_tool_can_reactivate(ready):
    desks.update_desk(ready.desk_id, {"status": "paused"}, db=ready.db)
    with pytest.raises(BrokerError):
        desks.update_desk(ready.desk_id, {"status": "active"}, db=ready.db, lower_only=True)


# =================================================================================================== A2: the start secret

def test_owner_true_without_the_secret_is_an_agent_and_asks(ready):
    set_capital(ready, 1000)
    ready.fake.on("POST", PREVIEW_PATH, preview_body(value=1500.0))
    forged = Who(owner=True)
    assert forged.owner is False and forged.proof is None
    for wrong in (Who(owner=True, proof="guess"), Who(owner=True, proof=""), Who(owner=True, proof=123)):
        assert wrong.owner is False
    held = service.place(ready.desk_id, limit("BUY", 150, "10.00"), "forged owner", forged, db=ready.db)
    assert held["status"] == "needs_owner" and sent(ready.fake) == []
    sent_by_owner = service.place(ready.desk_id, limit("BUY", 150, "10.00"), "real owner", OWNER, db=ready.db)
    assert sent_by_owner["status"] == "sent" and sent_by_owner["approved_by"] == "owner"


def test_a_copy_of_the_owner_with_the_flag_set_by_hand_does_not_keep_it():
    assert OWNER.owner is True
    assert dataclasses.replace(OWNER, agent="x").owner is False          # the secret is not kept in the object
    assert Who(**{**dataclasses.asdict(OWNER), "owner": True}).owner is False
    assert "proof" not in repr(OWNER) and OWNER.proof is None


def test_a_forged_owner_cannot_approve_or_reject_a_pending_order(ready):
    set_capital(ready, 1000)
    ready.fake.on("POST", PREVIEW_PATH, preview_body(value=1500.0))
    held = service.place(ready.desk_id, limit("BUY", 150, "10.00"), "idea", AGENT, db=ready.db)
    for who in (None, AGENT, Who(owner=True)):
        for fn in (service.approve_pending, service.reject_pending):
            with pytest.raises(BrokerError) as refused:
                fn(held["decision_id"], db=ready.db, who=who)
            assert refused.value.code == "owner_required" and refused.value.status == 403
    with pytest.raises(BrokerError):
        service.approve_pending(held["decision_id"], db=ready.db)                      # no `who` at all
    assert sent(ready.fake) == [] and len(pending(ready)) == 1
    assert service.approve_pending(held["decision_id"], db=ready.db, who=OWNER)["status"] == "sent"


def test_the_secret_is_compared_in_constant_time_and_is_never_in_the_environment_a_log_or_the_arguments(monkeypatch, caplog):
    seen = []
    real = service.hmac.compare_digest
    monkeypatch.setattr(service.hmac, "compare_digest", lambda a, b: seen.append((type(a), type(b))) or real(a, b))
    secret = service._OWNER_KEY
    assert secret and len(secret) >= 40
    with caplog.at_level("DEBUG"):
        assert Who(owner=True, proof=secret).owner is True
        assert Who(owner=True, proof="x" * len(secret)).owner is False
        assert Who(owner=True, proof="\u00f1").owner is False                                 # not ASCII: no exception either
    assert seen and all(kinds == (bytes, bytes) for kinds in seen)
    assert secret not in caplog.text and "x" * 20 not in caplog.text
    assert not any(secret in value for value in os.environ.values()) and not any(secret in arg for arg in sys.argv)
    assert not any(secret in str(value) for value in modes.work_env("/tmp/p", "1", "2", "joel").values())


def test_a_child_process_does_not_inherit_the_secret_and_cannot_build_an_owner():
    secret = service._OWNER_KEY
    probe = ("import os, sys\n"
             "from hokma.core import processguard\n"
             "processguard._role = processguard.SERVER\n"            # a server stand-in: without it the import is refused (test_processguard)
             "import hokma.broker.service as s\n"
             "key = sys.stdin.read()\n"
             "print('inherited' if any(key in v for v in os.environ.values()) else 'clean')\n"
             "print('in-argv' if key in ' '.join(sys.argv) else 'argv-clean')\n"
             "print('generated' if s._OWNER_KEY is not None else 'none')\n"
             "print(s.Who(owner=True, proof='anything').owner)\n")
    done = subprocess.run([sys.executable, "-c", probe], input=secret, capture_output=True, text=True, cwd=BACKEND, timeout=120,
                          env={**os.environ, "PYTHONPATH": str(BACKEND)})
    assert done.returncode == 0, done.stderr[-800:]
    assert done.stdout.split() == ["clean", "argv-clean", "none", "False"]                 # nothing to inherit, none generated
    assert secret not in done.stdout + done.stderr


def test_no_tool_or_agent_module_can_ask_for_the_owner():
    for module in ("hokma_tools", "mail_tools", "company_tools"):
        path = BACKEND / "hokma" / "kit" / f"{module}.py"
        if path.exists():
            text = path.read_text(encoding="utf-8")
            assert "owner_who" not in text and "owner=True" not in text and "_OWNER_KEY" not in text, module


# ============================================================================================ B2: a value that cannot be worked out

def market_order(**more):
    order = limit(**more)
    order.pop("price")
    order["orderType"] = "MARKET"
    return order


def test_an_order_that_opens_with_no_price_to_value_it_needs_the_owner_in_both_modes_with_the_reason():
    for action in ("place", "replace"):
        answer = rules.evaluate(action=action, orders_enabled=True, desk_status="active", account_ok=True,
                                order=market_order(), preview=preview_body(value=0.0), capital=10**9)
        assert answer["result"] == "needs_owner" and answer["flags"] == ["unpriced"] and answer["code"] == "unpriced"
        assert "AAPL" in answer["why"] and answer["numbers"]["unpriced"] == ["AAPL"]
    priced = rules.evaluate(action="place", orders_enabled=True, desk_status="active", account_ok=True, order=limit(),
                            preview=preview_body(value=0.0), capital=10**9)
    assert priced["result"] == "auto"                                                          # a price: nothing to hold
    closing = rules.evaluate(action="place", orders_enabled=True, desk_status="active", account_ok=True,
                             order=market_order(instruction="SELL", quantity=5), preview=preview_body(value=0.0),
                             capital=10**9, positions=[_long("AAPL", 5)])
    assert closing["result"] == "auto"                                                         # closing is never held back


def _long(symbol, qty):
    from hokma.broker.types import Position

    return Position(account_id=1, symbol=symbol, qty=qty, avg_price=10.0, asset_type="EQUITY", long_qty=qty, short_qty=0.0,
                    market_value=qty * 10.0)


def test_the_service_holds_an_unpriced_opening_order_for_the_agent_and_for_both_modes(ready):
    ready.fake.on("POST", PREVIEW_PATH, preview_body(value=0.0))
    held = service.place(ready.desk_id, market_order(), "no price", AGENT, db=ready.db)
    assert held["status"] == "needs_owner" and "AAPL" in held["why"] and sent(ready.fake) == []
    answer, err = run("order_place", {"desk_id": ready.desk_id, "order": market_order(quantity=2), "reason": "m"})
    assert not err and answer["status"] == "needs_owner"
    assert not modes.asks("auto", "order_place") and modes.asks("ask", "order_place")           # the rule is the same in both


# ================================================================================================= B3: a 5xx of an order

def one_attempt(fake, method="POST"):
    return [c for c in calls(fake, method) if c.path.endswith("/orders") or "/orders/" in c.path]


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_a_5xx_while_placing_leaves_the_outcome_unknown_and_is_never_repeated(ready, status):
    ready.fake.on("POST", ORDERS, (status, {"errors": [{"status": str(status), "title": "Server"}]}))
    with pytest.raises(BrokerError) as unknown:
        service.place(ready.desk_id, limit(), "send", AGENT, db=ready.db)
    assert "outcome is unknown" in unknown.value.message and "read the orders" in unknown.value.message
    assert unknown.value.code == UPSTREAM
    assert len(one_attempt(ready.fake)) == 1                                                  # one POST, no retry anywhere
    log = rows(ready)[0]
    assert log.action == "place" and log.approved_by == "auto" and log.http_status == 502


def test_a_500_while_replacing_is_the_same_and_one_attempt(ready):
    ready.fake.on("GET", f"{ORDERS}/555", order_body(555))
    ready.fake.on("PUT", f"{ORDERS}/555", (500, {"errors": [{"status": "500"}]}))
    with pytest.raises(BrokerError) as unknown:
        service.replace(ready.desk_id, 555, limit(), "replace", AGENT, db=ready.db)
    assert "outcome is unknown" in unknown.value.message
    assert len(one_attempt(ready.fake, "PUT")) == 1


def test_the_tool_does_not_retry_either_and_a_4xx_is_a_plain_refusal(ready):
    ready.fake.on("POST", ORDERS, (503, None))
    text, err = run("order_place", {"desk_id": ready.desk_id, "order": limit(), "reason": "x"})
    assert err and "outcome is unknown" in text and len(one_attempt(ready.fake)) == 1
    ready.fake.calls.clear()
    ready.fake.on("POST", ORDERS, (400, {"errors": [{"status": "400", "title": "Bad", "detail": "no"}]}))
    text, err = run("order_place", {"desk_id": ready.desk_id, "order": limit(), "reason": "x"})
    assert err and "outcome is unknown" not in text and len(one_attempt(ready.fake)) == 1


def test_a_5xx_of_the_preview_is_not_an_unknown_outcome(ready):
    ready.fake.on("POST", PREVIEW_PATH, (500, None))
    with pytest.raises(BrokerError) as failed:
        service.place(ready.desk_id, limit(), "x", AGENT, db=ready.db)
    assert "outcome is unknown" not in failed.value.message and sent(ready.fake) == []


# ===================================================================================== C2: an approved order that grew

@pytest.mark.parametrize("now,goes", [(1040.0, True), (1050.0, True), (1060.0, False)])
def test_an_approved_order_worth_more_than_5_percent_above_what_the_owner_saw_asks_again(ready, now, goes):
    set_capital(ready, 500)
    link_telegram(ready.db)
    ready.fake.on("POST", PREVIEW_PATH, preview_body(value=1000.0))
    held = service.place(ready.desk_id, limit("BUY", 100, "10.00"), "idea", AGENT, db=ready.db)
    assert held["status"] == "needs_owner" and len(outbox(ready.db)) == 1
    ready.fake.on("POST", PREVIEW_PATH, preview_body(value=now))
    answer = service.approve_pending(held["decision_id"], db=ready.db, who=OWNER)
    if goes:
        assert answer["status"] == "sent" and answer["approved_by"] == "owner"
        return
    assert answer["status"] == "needs_owner" and answer["decision_id"] != held["decision_id"] and "5%" in answer["why"]
    assert "1,060.00" in answer["why"] and "1,000.00" in answer["why"] and sent(ready.fake) == []
    assert len(outbox(ready.db)) == 2 and [r.decision_id for r in pending(ready)] == [answer["decision_id"]]
    with ready.db.read() as u:
        assert u.decisions.get(held["decision_id"]).status == "approved"
    ready.fake.on("POST", PREVIEW_PATH, preview_body(value=1062.0))                           # 0.2% above what he saw now
    assert service.approve_pending(answer["decision_id"], db=ready.db, who=OWNER)["status"] == "sent"


def test_a_lower_value_and_a_closing_order_never_ask_again(ready):
    set_capital(ready, 500)
    ready.fake.on("POST", PREVIEW_PATH, preview_body(value=1000.0))
    held = service.place(ready.desk_id, limit("BUY", 100, "10.00"), "idea", AGENT, db=ready.db)
    ready.fake.on("POST", PREVIEW_PATH, preview_body(value=700.0))
    assert service.approve_pending(held["decision_id"], db=ready.db, who=OWNER)["status"] == "sent"


def test_the_value_the_owner_saw_is_kept_in_the_record_of_the_decision(ready):
    set_capital(ready, 500)
    ready.fake.on("POST", PREVIEW_PATH, preview_body(value=1000.0))
    held = service.place(ready.desk_id, limit("BUY", 100, "10.00"), "idea", AGENT, db=ready.db)
    assert pending(ready)[0].response_json["rule"]["numbers"]["order_value"] == 1000.0 and held["decision_id"]


# ============================================================================================ C4: the reason of Schwab

REASON = [{"id": "abc-123", "status": "400", "title": "Bad Request", "detail": "quantity above the limit",
           "source": {"pointer": "/orderLegCollection/0/quantity"}}]


def test_message_of_keeps_the_errors_of_schwab_and_the_catalog_text():
    text = reader.message_of(SchwabError(BAD_REQUEST, 400, errors=REASON))
    assert text.startswith(t("schwab.err.bad_request"))
    for part in ("Bad Request", "quantity above the limit", "abc-123", "400"):
        assert part in text
    assert "/orderLegCollection/0/quantity" not in text                    # a pointer is cut to its field name (1226)
    assert reader.message_of(SchwabError(BAD_REQUEST, 400)) == t("schwab.err.bad_request")
    assert reader.message_of(SchwabError(UPSTREAM, 502, errors=[{"detail": "x" * 5000}] * 20)).count("x") <= reader.REASON_TOTAL
    assert "\n" not in reader.message_of(SchwabError(BAD_REQUEST, 400, errors=[{"title": "a\nb\x00c"}]))


def test_the_reason_hides_what_looks_like_an_identifier_but_keeps_the_sentence():
    hostile = [{"title": "Account 73910482 and 9F8E7D6C5B4A39281706F5E4D3C2B1A0 sent-login-5102 AT-SECRET-1",
                "detail": "Invalid symbol AAPL  261218C00210000, quantity above 1000 for a@b.com"}]
    text = reader.reason_of(SchwabError(BAD_REQUEST, 400, errors=hostile))
    for secret in ("73910482", "9F8E7D6C5B4A39281706F5E4D3C2B1A0", "sent-login-5102", "AT-SECRET-1", "a@b.com"):
        assert secret not in text
    assert "[hidden]" in text and "Invalid symbol" in text and "261218C00210000" in text and "above 1000" in text


def test_the_agent_and_the_log_get_the_reason_when_schwab_refuses_an_order(ready):
    ready.fake.on("POST", PREVIEW_PATH, (400, {"errors": REASON}))
    text, err = run("order_place", {"desk_id": ready.desk_id, "order": limit(), "reason": "x"})
    assert err and "quantity above the limit" in text and t("schwab.err.bad_request") in text
    assert "quantity above the limit" in (rows(ready)[0].rule_why or "")


# ===================================================================================== C4b: the same order is asked once

def test_the_same_pending_order_does_not_make_a_second_decision_or_a_second_notice(ready):
    set_capital(ready, 1000)
    link_telegram(ready.db)
    ready.fake.on("POST", PREVIEW_PATH, preview_body(value=1500.0))
    first = service.place(ready.desk_id, limit("BUY", 150, "10.00"), "one reason", AGENT, db=ready.db)
    second = service.place(ready.desk_id, limit("BUY", 150, "10.00"), "another reason", AGENT, db=ready.db)
    reordered = {key: limit("BUY", 150, "10.00")[key] for key in reversed(list(limit("BUY", 150, "10.00")))}
    third = service.place(ready.desk_id, reordered, "third", AGENT, db=ready.db)                      # same body, other key order
    assert first["status"] == second["status"] == third["status"] == "needs_owner"
    assert second["decision_id"] == third["decision_id"] == first["decision_id"]
    assert second.get("already_pending") is True and third.get("already_pending") is True and "already_pending" not in first
    assert len(outbox(ready.db)) == 1 and len(pending(ready)) == 1
    with ready.db.read() as u:
        assert u.session.scalar("SELECT count(*) FROM decisions") == 1
    assert len(rows(ready)) == 3                                                                     # the attempts stay in the log
    other = service.place(ready.desk_id, limit("BUY", 151, "10.00"), "different order", AGENT, db=ready.db)
    assert other["decision_id"] != first["decision_id"] and "already_pending" not in other and len(outbox(ready.db)) == 2


def test_after_the_answer_or_the_expiry_the_same_order_asks_again(ready, monkeypatch):
    import datetime as dt

    set_capital(ready, 1000)
    ready.fake.on("POST", PREVIEW_PATH, preview_body(value=1500.0))
    first = service.place(ready.desk_id, limit("BUY", 150, "10.00"), "a", AGENT, db=ready.db)
    service.reject_pending(first["decision_id"], db=ready.db, who=OWNER)
    second = service.place(ready.desk_id, limit("BUY", 150, "10.00"), "b", AGENT, db=ready.db)
    assert second["decision_id"] != first["decision_id"] and "already_pending" not in second
    monkeypatch.setattr(service, "_now", lambda: dt.datetime.now(dt.UTC) + dt.timedelta(minutes=45))
    third = service.place(ready.desk_id, limit("BUY", 150, "10.00"), "c", AGENT, db=ready.db)        # the second one expired
    assert third["decision_id"] not in (first["decision_id"], second["decision_id"]) and "already_pending" not in third

"""broker_list / account_create / account_reset / desk_add_account in the chat (task 3294, C7-5c): write tools ask
for permission, the same service as the routes, strict schema, the change visible in the permission request."""
import json

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)

from hokma.desks import service as desks
from hokma.engine.stream import summarize_tool
from hokma.kit import hokma_tools
from hokma.kit.hokma_tools import Caller

NAMES = ("broker_list", "account_create", "account_reset", "desk_add_account")


def call(name, args, agent="hokma"):
    text, is_error = hokma_tools.call(name, args, Caller({"HOKMA_AGENT": agent}))
    return (json.loads(text) if not is_error else text), is_error


def test_declared_strict_and_only_the_writes_ask_permission(db):
    listed = {d["name"]: d for d in hokma_tools.definitions()}
    assert [hokma_tools.TOOLS[n][4] for n in NAMES] == [False, True, True, True]
    for n in NAMES:
        assert listed[n]["inputSchema"]["additionalProperties"] is False and n in hokma_tools.NO_UNIT
    assert listed["account_create"]["inputSchema"]["required"] == ["name", "capital"]


def test_the_request_shows_the_owner_what_will_change():
    shown = summarize_tool("mcp__hokma__account_create", {"name": "Swing", "capital": 25000})
    assert shown == {"kind": "other", "target": 'name="Swing" | capital=25000'}
    assert summarize_tool("mcp__hokma__desk_add_account", {"desk_id": 3, "account_id": 7})["target"] == "desk_id=3 | account_id=7"


def test_create_an_account_of_25000_assign_it_to_the_desk_and_list_it(db):
    desk = desks.create_desk("Swing", 10000, template="minimal")
    made, err = call("account_create", {"name": "Cuenta 25k", "capital": 25000})
    assert not err and made["cash"] == 25000 and made["broker"] == "Hokma"
    got, err = call("desk_add_account", {"desk_id": desk["id"], "account_id": made["id"]})
    assert not err and got["cash"] == 25000 and got["name"] == "Cuenta 25k" and got["bound_strategies"] == ["principal"]
    listed, err = call("broker_list", {})
    assert not err and listed[0]["accounts"][0]["desk_ids"] == [desk["id"]] and listed[0]["slippage_bps"] == 2.0
    assert desks.get_desk(desk["id"])["accounts"][0]["id"] == made["id"]


def test_reset_puts_the_account_back_and_a_bad_request_is_a_clear_error(db):
    made, _ = call("account_create", {"name": "A", "capital": 1000})
    again, err = call("account_reset", {"account_id": made["id"]})
    assert not err and again["cash"] == 1000
    for name, args in (("account_reset", {"account_id": 99}), ("account_create", {"name": "A", "capital": 1000}),
                       ("account_create", {"name": "B", "capital": 5}), ("account_create", {"name": "C", "capital": 1000, "broker_id": 99}),
                       ("desk_add_account", {"desk_id": 99, "account_id": made["id"]})):
        text, err = call(name, args)
        assert err and text, (name, args)


def test_a_desk_chief_cannot_touch_accounts(db):
    made, _ = call("account_create", {"name": "A", "capital": 1000})
    for name, args in (("account_create", {"name": "B", "capital": 1000}), ("account_reset", {"account_id": made["id"]})):
        text, err = call(name, args, agent="desk-chief")
        assert err and "desk chief" in text

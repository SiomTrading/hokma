"""A desk without accounts, and accounts that come later (SPEC-027, piece 027c, task 3335): the desk is born with no account and an optional
capital; the tools of the chief add a demo account in one transaction, add an account that already exists (it may serve other desks),
take one out and tie a strategy to one; nothing is created without the tool, and each tool asks the owner."""
import json

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hokma.broker import desk_accounts, service
from hokma.broker.api import router as broker_router
from hokma.desks import service as desks
from hokma.desks.api import router as desks_router
from hokma.desks.errors import DeskError
from hokma.desks.swo.create import create_swo_desk
from hokma.engine.stream import summarize_tool
from hokma.kit import hokma_tools
from hokma.kit.hokma_tools import Caller
from hokma.permissions import modes
from hokma.rpc.ws import SESSION_TOKEN
from account_links_support import link_principal

TOOLS = ("desk_add_demo_account", "desk_add_account", "desk_remove_account", "desk_bind_strategy")


def call(name, args, agent="hokma"):
    text, is_error = hokma_tools.call(name, args, Caller({"HOKMA_AGENT": agent}))
    return (json.loads(text) if not is_error else text), is_error


def new_desk(name="Mesa", **kw):
    return desks.create_desk(name, template="minimal", **kw)


def sim_account(db, name="A", capital=5000):
    with db.write() as u:
        sim = next(b for b in u.broker.list_brokers() if b.kind == "sim")
        return u.broker.create_account(sim.id, name, capital)


def links(db, desk_id):
    with db.read() as u:
        return [r["account_id"] for r in u.session.query("SELECT account_id FROM desk_account_links WHERE desk_id = :d ORDER BY account_id", {"d": desk_id})]


def count_accounts(db):
    with db.read() as u:
        return u.session.scalar("SELECT count(*) FROM broker_accounts")


# ---- C1: a desk is born without account and without capital -----------------------------------------------------------------------

def test_c1_a_new_desk_has_no_account_and_no_capital_unless_one_is_given(db):
    desk = new_desk()
    assert desk["capital"] is None and "account" not in desk and desk["accounts"] == [] and desk["strategies"] == {}
    assert links(db, desk["id"]) == [] and count_accounts(db) == 0
    assert new_desk("Con capital", capital=5000)["capital"] == 5000                    # optional, never a default
    with pytest.raises(DeskError):
        new_desk("Mala", capital=5)


def test_c1_the_swo_desk_is_born_without_account_too(db):
    desk = create_swo_desk("Opciones", db=db)
    assert desk["capital"] is None and desk["accounts"] == [] and count_accounts(db) == 0


def test_c1_the_chat_tools_never_create_an_account_and_take_no_required_capital(db):
    listed = {d["name"]: d for d in hokma_tools.definitions()}
    assert listed["desk_create"]["inputSchema"]["required"] == ["name"] and listed["desk_create_swo"]["inputSchema"]["required"] == ["name"]
    made, err = call("desk_create", {"name": "Por chat", "template": "minimal"})
    assert not err and made["capital"] is None and made["accounts"] == [] and count_accounts(db) == 0
    swo, err = call("desk_create_swo", {"name": "Swo chat"})
    assert not err and count_accounts(db) == 0
    with db.read() as u:
        assert u.session.scalar("SELECT count(*) FROM desk_account_links") == 0


def test_a_desk_without_capital_can_get_one_and_a_lower_only_caller_cannot_remove_or_raise_it(db):
    desk = new_desk()
    assert desks.update_desk(desk["id"], {"capital": 5000}, lower_only=True)["capital"] == 5000     # a number is a limit
    assert desks.update_desk(desk["id"], {"capital": 4000}, lower_only=True)["capital"] == 4000
    with pytest.raises(service.BrokerError):
        desks.update_desk(desk["id"], {"capital": 9000}, lower_only=True)
    with pytest.raises(service.BrokerError):
        desks.update_desk(desk["id"], {"capital": None}, lower_only=True)                          # taking the number away loosens
    assert desks.update_desk(desk["id"], {"capital": None})["capital"] is None                      # the owner may


# ---- C3 / C4: the demo account is created and added in one transaction --------------------------------------------------------------

def test_c3_the_first_account_is_added_and_ties_the_principal_strategy(db):
    desk = new_desk()
    got, err = call("desk_add_demo_account", {"desk_id": desk["id"], "name": "Demo uno", "initial_capital": 10000})
    assert not err and got["cash"] == 10000 and got["bound_strategies"] == ["principal"] and got["shared_with"] == []
    shown = desks.get_desk(desk["id"])
    assert [a["name"] for a in shown["accounts"]] == ["Demo uno"] and shown["strategies"] == {"principal": got["id"]} and shown["accounts"][0]["id"] == got["id"]
    with db.read() as u:
        assert u.session.scalar("SELECT added_by FROM desk_account_links WHERE desk_id = :d", {"d": desk["id"]}) == "chat"
    second, err = call("desk_add_demo_account", {"desk_id": desk["id"], "name": "Demo dos", "initial_capital": 2000})
    assert not err and second["bound_strategies"] == []                                  # more accounts: each strategy is asked and tied on its own
    assert desks.get_desk(desk["id"])["strategies"] == {"principal": got["id"]}


@pytest.mark.parametrize("where", ["link", "bind"])
def test_c4_if_the_creation_fails_halfway_no_account_and_no_link_is_left(db, monkeypatch, where):
    from hokma.db.repos.broker import BrokerRepo

    desk = new_desk()

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(BrokerRepo, "link" if where == "link" else "bind_strategy", boom)
    with pytest.raises(RuntimeError):
        desk_accounts.add_demo_account(desk["id"], "Demo", 5000, db=db)
    assert count_accounts(db) == 0 and links(db, desk["id"]) == []


def test_c4_a_bad_demo_account_changes_nothing(db):
    desk = new_desk()
    for name, capital in (("", 5000), ("Demo", 5), ("Demo", "x"), ("D" * 61, 5000)):
        with pytest.raises(service.BrokerError):
            desk_accounts.add_demo_account(desk["id"], name, capital, db=db)
    sim_account(db, "Usada")
    with pytest.raises(service.BrokerError) as raised:
        desk_accounts.add_demo_account(desk["id"], "Usada", 5000, db=db)
    assert raised.value.code == "name_taken" and count_accounts(db) == 1 and links(db, desk["id"]) == []
    with pytest.raises(service.BrokerError) as raised:
        desk_accounts.add_demo_account(999, "Demo", 5000, db=db)
    assert raised.value.code == "desk_not_found" and count_accounts(db) == 1


# ---- C5: an account can be in two desks --------------------------------------------------------------------------------------

def test_c5_an_account_in_one_desk_can_be_added_to_another_and_both_say_so(db):
    d1, d2 = new_desk("Uno"), new_desk("Dos")
    account = sim_account(db)
    first, err = call("desk_add_account", {"desk_id": d1["id"], "account_id": account.id})
    assert not err and first["shared_with"] == [] and first["bound_strategies"] == ["principal"]
    second, err = call("desk_add_account", {"desk_id": d2["id"], "account_id": account.id})
    assert not err and second["shared_with"] == [d1["id"]] and second["desk_ids"] == [d1["id"], d2["id"]]
    assert desks.get_desk(d1["id"])["accounts"][0]["shared_with"] == [d2["id"]] and desks.get_desk(d2["id"])["accounts"][0]["shared_with"] == [d1["id"]]
    again, err = call("desk_add_account", {"desk_id": d2["id"], "account_id": account.id})
    assert not err and again["bound_strategies"] == [] and links(db, d2["id"]) == [account.id]       # the same twice changes nothing


def test_an_account_that_does_not_exist_or_is_archived_is_refused(db):
    desk = new_desk()
    account = sim_account(db)
    with db.write() as u:
        u.broker.set_account_status(account.id, "archived")
    for wanted in (999, account.id, "x"):
        with pytest.raises(service.BrokerError) as raised:
            desk_accounts.add_account(desk["id"], wanted, db=db)
        assert raised.value.code == "account_not_found"
    assert links(db, desk["id"]) == []


# ---- C8: taking an account out ------------------------------------------------------------------------------------------------

def test_c8_an_account_a_strategy_uses_cannot_be_removed_until_the_strategy_moves(db):
    desk = new_desk()
    a, b = sim_account(db, "A"), sim_account(db, "B")
    desk_accounts.add_account(desk["id"], a.id, db=db)
    desk_accounts.add_account(desk["id"], b.id, db=db)
    with pytest.raises(service.BrokerError) as raised:
        desk_accounts.remove_account(desk["id"], a.id, db=db)
    assert raised.value.status == 409 and raised.value.code == "account_in_use_by_strategy" and raised.value.message
    got, err = call("desk_remove_account", {"desk_id": desk["id"], "account_id": a.id})
    assert err and "strategy" in got
    moved, err = call("desk_bind_strategy", {"desk_id": desk["id"], "strategy_key": "principal", "account_id": b.id})
    assert not err and moved["bindings"] == {"principal": b.id}
    done, err = call("desk_remove_account", {"desk_id": desk["id"], "account_id": a.id})
    assert not err and done["accounts"] == [b.id] and links(db, desk["id"]) == [b.id]
    assert count_accounts(db) == 2                                                          # the account and its money stay


def test_a_strategy_cannot_be_tied_to_an_account_that_is_not_in_the_desk(db):
    desk, other = new_desk(), new_desk("Otra")
    account = sim_account(db)
    desk_accounts.add_account(other["id"], account.id, db=db)
    with pytest.raises(service.BrokerError) as raised:
        desk_accounts.bind_strategy(desk["id"], "principal", account.id, db=db)
    assert raised.value.code == "account_not_linked"
    with pytest.raises(service.BrokerError):
        desk_accounts.remove_account(desk["id"], account.id, db=db)
    desk_accounts.add_account(desk["id"], account.id, db=db)
    for key in ("", "x" * 81):
        with pytest.raises(service.BrokerError):
            desk_accounts.bind_strategy(desk["id"], key, account.id, db=db)
    assert desk_accounts.bind_strategy(desk["id"], "momentum", account.id, db=db)["bindings"] == {"momentum": account.id, "principal": account.id}


def _working_order(db, desk_id, account_id, status="working"):
    request = {"legs": [{"contract_id": "EQ:AAPL", "side": "buy", "qty": "1"}], "type": "limit", "limit_price": "1", "tif": "day"}
    with db.write() as u:
        u.session.insert("sim_orders", {"account_id": account_id, "order_ref": "S1", "request_json": json.dumps(request), "status": status})
        u.session.insert("broker_orders", {"desk_id": desk_id, "account_id": account_id, "broker_kind": "sim", "action": "place",
                                           "http_status": 200, "broker_order_id": "S1"})


def test_an_account_with_orders_alive_of_the_desk_cannot_be_removed_and_can_after_they_close(db):
    desk = new_desk()
    account = sim_account(db)
    desk_accounts.add_account(desk["id"], account.id, db=db)
    with db.write() as u:
        u.broker.unbind_strategy(desk["id"], "principal")
    _working_order(db, desk["id"], account.id)
    with pytest.raises(service.BrokerError) as raised:
        desk_accounts.remove_account(desk["id"], account.id, db=db)
    assert raised.value.status == 409 and raised.value.code == "working_orders"
    with db.write() as u:
        u.session.execute("UPDATE sim_orders SET status = 'canceled'")
    assert desk_accounts.remove_account(desk["id"], account.id, db=db)["accounts"] == []


def test_an_order_of_another_desk_in_the_same_account_does_not_hold_this_desk(db):
    one, two = new_desk("Uno"), new_desk("Dos")
    account = sim_account(db)
    for desk in (one, two):
        desk_accounts.add_account(desk["id"], account.id, db=db)
        with db.write() as u:
            u.broker.unbind_strategy(desk["id"], "principal")
    _working_order(db, one["id"], account.id)
    assert desk_accounts.remove_account(two["id"], account.id, db=db)["accounts"] == []
    with pytest.raises(service.BrokerError):
        desk_accounts.remove_account(one["id"], account.id, db=db)


# ---- the tools: they ask the owner, and the chief only touches its own desk ------------------------------------------------------

def test_every_account_tool_always_asks_the_owner_and_shows_what_it_does(db):
    listed = {d["name"]: d for d in hokma_tools.definitions()}
    for name in TOOLS:
        assert hokma_tools.TOOLS[name][4] is True and f"mcp__hokma__{name}" in modes.ALWAYS_ASK
        assert listed[name]["inputSchema"]["additionalProperties"] is False and name in hokma_tools.NO_UNIT
    assert not {"mcp__hokma__desk_assign_account", "mcp__hokma__desk_unassign_account"} & set(modes.ALWAYS_ASK)
    assert "desk_assign_account" not in hokma_tools.TOOLS and "desk_unassign_account" not in hokma_tools.TOOLS
    assert summarize_tool("mcp__hokma__desk_add_demo_account", {"desk_id": 3, "name": "Demo", "initial_capital": 1000})["target"] == \
        'desk_id=3 | name="Demo" | initial_capital=1000'
    assert listed["desk_add_demo_account"]["inputSchema"]["required"] == ["desk_id", "name", "initial_capital"]
    assert listed["desk_bind_strategy"]["inputSchema"]["required"] == ["desk_id", "strategy_key", "account_id"]


def test_the_chief_of_a_desk_works_only_on_its_own_desk(db):
    mine, other = new_desk("Mia"), new_desk("Otra")
    with db.read() as u:
        project = u.desks.get(mine["id"]).project_id
        conv = None
    with db.write() as u:
        conv = u.conversations.create(engine="claude", project_id=project).id
    chief = Caller({"HOKMA_AGENT": "desk-chief", "HOKMA_CONVERSATION_ID": str(conv)})
    text, err = hokma_tools.call("desk_add_demo_account", {"desk_id": other["id"], "name": "Ajena", "initial_capital": 1000}, chief)
    assert err and count_accounts(db) == 0
    text, err = hokma_tools.call("desk_add_demo_account", {"desk_id": mine["id"], "name": "Mia demo", "initial_capital": 1000}, chief)
    assert not err and links(db, mine["id"]) and not links(db, other["id"])
    text, err = hokma_tools.call("account_create", {"name": "B", "capital": 1000}, chief)
    assert err and "desk chief" in text                                                       # the broker's own accounts stay the owner's


# ---- the routes of the app ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def client(db):
    app = FastAPI()
    app.include_router(desks_router)
    app.include_router(broker_router)
    with TestClient(app) as c:
        c.headers.update({"X-Hokma-Token": SESSION_TOKEN})
        yield c


def test_the_routes_add_a_demo_or_an_existing_account_remove_it_and_tie_a_strategy(client, db):
    d1, d2 = new_desk("Uno"), new_desk("Dos")
    res = client.post(f"/api/desks/{d1['id']}/accounts", json={"demo": {"name": "Demo", "initial_capital": 3000}})
    assert res.status_code == 201 and res.json()["bound_strategies"] == ["principal"]
    with db.read() as u:
        assert u.session.scalar("SELECT added_by FROM desk_account_links WHERE desk_id = :d", {"d": d1["id"]}) == "owner"
    account_id = res.json()["id"]
    res = client.post(f"/api/desks/{d2['id']}/accounts", json={"account_id": account_id})
    assert res.status_code == 201 and res.json()["shared_with"] == [d1["id"]]
    for bad in ({}, {"account_id": 1, "demo": {"name": "X", "initial_capital": 100}}, {"demo": "x"}):
        assert client.post(f"/api/desks/{d1['id']}/accounts", json=bad).status_code == 400
    res = client.delete(f"/api/desks/{d1['id']}/accounts/{account_id}")
    assert res.status_code == 409 and res.json()["detail"]["code"] == "account_in_use_by_strategy"
    other = sim_account(db, "Otra cuenta")
    assert client.post(f"/api/desks/{d1['id']}/accounts", json={"account_id": other.id}).status_code == 201
    res = client.put(f"/api/desks/{d1['id']}/strategies/principal/account", json={"account_id": other.id})
    assert res.status_code == 200 and res.json()["bindings"] == {"principal": other.id}
    assert client.delete(f"/api/desks/{d1['id']}/accounts/{account_id}").status_code == 200
    assert client.delete(f"/api/desks/{d1['id']}/accounts/{account_id}").json()["detail"]["code"] == "account_not_linked"
    assert client.put(f"/api/desks/{d1['id']}/strategies/x/account", json={"account_id": account_id}).json()["detail"]["code"] == "account_not_linked"
    assert client.post("/api/desks/999/accounts", json={"account_id": other.id}).json()["detail"]["code"] == "desk_not_found"
    for method, url in (("post", "/api/desks/1/accounts"), ("delete", "/api/desks/1/accounts/1"), ("put", "/api/desks/1/strategies/principal/account")):
        assert getattr(client, method)(url, headers={"X-Hokma-Token": "no"}).status_code == 401


def test_the_one_account_buttons_of_before_are_gone_and_the_view_reads_the_list(client, db):
    desk = new_desk()
    account = sim_account(db)
    assert client.put(f"/api/desks/{desk['id']}/account", json={"account_id": account.id}).status_code in (404, 405)
    assert client.delete(f"/api/desks/{desk['id']}/account").status_code in (404, 405)
    assert client.post(f"/api/desks/{desk['id']}/accounts", json={"account_id": account.id}).status_code == 201
    shown = client.get(f"/api/desks/{desk['id']}").json()
    assert "account" not in shown and shown["accounts"][0]["id"] == account.id


def test_old_desks_are_unchanged_by_the_new_calls(db):
    desk = new_desk("Vieja", capital=7000)
    account = sim_account(db)
    with db.write() as u:
        link_principal(u, desk["id"], account.id)                                           # how a desk got its account before
    shown = desks.get_desk(desk["id"])
    assert shown["capital"] == 7000 and shown["accounts"][0]["id"] == account.id and shown["strategies"] == {"principal": account.id}

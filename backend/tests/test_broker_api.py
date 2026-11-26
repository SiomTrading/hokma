"""Brokers and accounts over HTTP (task 3294, C7-5c): every route and error code, the token, the reset confirmation,
one account per desk and the `account` field of the desk summary."""
import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hokma.broker.api import router as broker_router
from hokma.desks.api import router as desks_router
from hokma.desks import service as desks
from hokma.i18n import t
from hokma.rpc.ws import SESSION_TOKEN


@pytest.fixture
def client(db):
    with db.write() as u:
        u.profile.save(owner_name="Ana", fund_name="Fondo Prueba")
    app = FastAPI()
    app.include_router(desks_router)
    app.include_router(broker_router)
    with TestClient(app) as c:
        c.headers.update({"X-Hokma-Token": SESSION_TOKEN})
        yield c


def code(res):
    return res.json()["detail"]["code"]


def sim(client):
    return client.get("/api/brokers").json()["brokers"][0]


def account(client, name="Swing", capital=25000):
    res = client.post(f"/api/brokers/{sim(client)['id']}/accounts", json={"name": name, "capital": capital})
    assert res.status_code == 201, res.text
    return res.json()


def desk(client, name="Mesa"):
    return client.post("/api/desks", json={"name": name, "capital": 10000, "template": "minimal"}).json()


def test_every_route_needs_the_token(client):
    for method, url in (("get", "/api/brokers"), ("post", "/api/brokers"), ("get", "/api/accounts/1"),
                        ("post", "/api/accounts/1/reset"), ("post", "/api/desks/1/accounts"), ("delete", "/api/desks/1/accounts/1")):
        res = getattr(client, method)(url, headers={"X-Hokma-Token": "no"})
        assert res.status_code == 401 and code(res) == "bad_token", url


def test_the_simulated_broker_is_listed_with_its_costs_and_its_orders_switch_off(client):
    b = sim(client)
    assert b["name"] == "Hokma" and b["slippage_bps"] == 2.0 and b["commission"]["type"] == "bps"
    assert b["capabilities"]["orders"] is True and b["orders_enabled"] is False        # SPEC-018: it takes orders; the switch starts off


def test_create_a_broker_with_costs_and_refuse_a_repeated_name_or_bad_costs(client):
    res = client.post("/api/brokers", json={"name": "Practica", "commission": {"type": "per_share", "value": 0.005},
                                            "slippage_bps": 5})
    assert res.status_code == 201 and res.json()["slippage_bps"] == 5.0 and res.json()["commission"]["min"] == 0.0
    assert code(client.post("/api/brokers", json={"name": "Practica"})) == "name_taken"
    assert client.post("/api/brokers", json={"name": "Practica"}).status_code == 409
    for bad in ({"name": ""}, {"name": "X" * 61}, {"name": "Y", "slippage_bps": -1}, {"name": "Z", "commission": {"type": "?"}}):
        res = client.post("/api/brokers", json=bad)
        assert res.status_code == 400 and code(res) == "invalid", bad
    assert len(client.get("/api/brokers").json()["brokers"]) == 2        # the refused ones left nothing behind


def test_costs_are_edited_in_place_and_bad_ones_change_nothing(client):
    b = sim(client)
    res = client.put(f"/api/brokers/{b['id']}/costs", json={"commission": {"type": "bps", "value": 3}, "slippage_bps": 4})
    assert res.status_code == 200 and res.json()["slippage_bps"] == 4.0 and res.json()["commission"]["value"] == 3.0
    assert client.put(f"/api/brokers/{b['id']}/costs", json={"slippage_bps": 6}).json()["commission"]["value"] == 3.0
    res = client.put(f"/api/brokers/{b['id']}/costs", json={"slippage_bps": -1})
    assert res.status_code == 400 and code(res) == "invalid" and sim(client)["slippage_bps"] == 6.0
    assert code(client.put("/api/brokers/999/costs", json={})) == "broker_not_found"


def test_create_and_list_accounts_with_their_balance(client):
    a = account(client)
    assert a["cash"] == 25000 and a["equity"] == 25000 and a["currency"] == "USD" and a["desk_ids"] == [] and a["as_of"]
    listed = client.get(f"/api/brokers/{a['broker_id']}/accounts").json()["accounts"]
    assert [x["name"] for x in listed] == ["Swing"]
    assert client.get(f"/api/accounts/{a['id']}").json()["cash"] == 25000


def test_account_limits_and_repeated_name(client):
    broker = sim(client)["id"]
    for bad in ({"name": "A", "capital": 99}, {"name": "A", "capital": 1_000_000_001}, {"name": "A", "capital": 5.5},
                {"name": "A", "capital": "9"}, {"name": " ", "capital": 1000}, {"name": "A" * 61, "capital": 1000}):
        res = client.post(f"/api/brokers/{broker}/accounts", json=bad)
        assert res.status_code == 400 and code(res) == "invalid", bad
    account(client)
    res = client.post(f"/api/brokers/{broker}/accounts", json={"name": "Swing", "capital": 1000})
    assert res.status_code == 409 and code(res) == "name_taken"


def test_not_found_has_a_clear_message(client):
    assert code(client.get("/api/accounts/999")) == "account_not_found"
    assert client.post("/api/brokers/999/accounts", json={"name": "A", "capital": 1000}).status_code == 404
    res = client.get("/api/brokers/999/accounts")
    assert res.status_code == 404 and res.json()["detail"]["message"] == t("broker.err.broker_not_found")


def test_reset_asks_for_confirmation_and_touches_only_that_account(client):
    a, b = account(client, "A", 1000), account(client, "B", 2000)
    for body in ({}, {"confirmed": False}, {"confirmed": "true"}):
        res = client.post(f"/api/accounts/{a['id']}/reset", json=body)
        assert res.status_code == 400 and code(res) == "not_confirmed", body
    ok = client.post(f"/api/accounts/{a['id']}/reset", json={"confirmed": True})
    assert ok.status_code == 200 and ok.json()["cash"] == 1000
    assert client.post(f"/api/accounts/{a['id']}/reset", json={"confirmed": True}).json()["cash"] == 1000   # again: same
    assert client.get(f"/api/accounts/{b['id']}").json()["cash"] == 2000
    assert code(client.post("/api/accounts/999/reset", json={"confirmed": True})) == "account_not_found"


def add(client, desk_id, account_id):
    return client.post(f"/api/desks/{desk_id}/accounts", json={"account_id": account_id})


def test_adding_an_account_puts_the_balance_in_the_desks_and_removing_it_takes_it_out(client):
    d, a = desk(client), account(client)
    assert d["accounts"] == [] and "account" not in d and client.get("/api/desks").json()["desks"][0]["accounts"] == []
    res = add(client, d["id"], a["id"])
    assert res.status_code == 201
    shown = client.get(f"/api/desks/{d['id']}").json()["accounts"][0]
    assert shown == {"id": a["id"], "name": "Swing", "broker": "Hokma", "mask": None, "as_of": shown["as_of"], "shared_with": [], "strategies": ["principal"],
                     "initial_capital": 25000, "balance": {"cash": 25000, "equity": 25000, "currency": "USD"}}
    assert client.get("/api/desks").json()["desks"][0]["accounts"][0]["balance"]["cash"] == 25000
    assert client.get(f"/api/accounts/{a['id']}").json()["desk_ids"] == [d["id"]]
    assert add(client, d["id"], a["id"]).status_code == 201                                  # same again: nothing changes
    assert code(client.delete(f"/api/desks/{d['id']}/accounts/{a['id']}")) == "account_in_use_by_strategy"   # the principal strategy trades through it


def test_the_one_account_routes_of_before_are_gone(client):
    d = desk(client)
    assert client.put(f"/api/desks/{d['id']}/account", json={"account_id": 1}).status_code in (404, 405)
    assert client.delete(f"/api/desks/{d['id']}/account").status_code in (404, 405)


def test_an_account_serves_many_desks_and_a_desk_has_many_accounts(client):
    d1, d2, a, b = desk(client, "Uno"), desk(client, "Dos"), account(client, "A"), account(client, "B")
    assert add(client, d1["id"], a["id"]).status_code == 201
    assert add(client, d2["id"], a["id"]).status_code == 201                                  # shared (SPEC-027)
    assert add(client, d1["id"], b["id"]).status_code == 201                                  # a second account
    assert client.get(f"/api/desks/{d1['id']}").json()["accounts"][0]["shared_with"] == [d2["id"]]
    assert code(add(client, 999, a["id"])) == "desk_not_found"
    assert code(add(client, d2["id"], 999)) == "account_not_found"
    assert code(add(client, d2["id"], "x")) == "account_not_found"


def test_deleting_a_desk_frees_its_account(client):
    d, a = desk(client), account(client)
    add(client, d["id"], a["id"])
    assert client.delete(f"/api/desks/{d['id']}").status_code == 200
    assert client.get(f"/api/accounts/{a['id']}").json()["desk_ids"] == []


def test_the_desk_contract_keeps_its_old_fields(client):
    d = desk(client)
    assert {"id", "name", "capital", "status", "tokens_week", "agents_total"} <= set(client.get("/api/desks").json()["desks"][0])
    assert "accounts" in client.get(f"/api/desks/{d['id']}").json()

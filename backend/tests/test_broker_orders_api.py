"""The routes of the orders of a desk (task 3299, SPEC-015 18.2 to 18.6): the same service as the tools, behind the per-boot session
token. The app is the owner: a soft condition does not ask again, a refusal still applies. The pending decisions are approved or
rejected from here. No answer has an identifier of the account."""
import json

import pytest
from broker_orders_support import (ACCOUNT, ORDERS, PREVIEW_PATH, SECRETS, account_body, calls, connections, created, desks_db, db,  # noqa: F401
                                   env, fake, keys, limit, order_body, pos, preview_body, ready, rows, server, set_capital)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hokma.broker import service
from hokma.broker.api import router as broker_router
from hokma.broker.service import Who
from hokma.i18n import t
from hokma.rpc.ws import SESSION_TOKEN


@pytest.fixture
def client(ready):
    app = FastAPI()
    app.include_router(broker_router)
    with TestClient(app) as c:
        c.headers.update({"X-Hokma-Token": SESSION_TOKEN})
        yield c


def code(res):
    return res.json()["detail"]["code"]


def test_every_order_route_needs_the_token(client, ready):
    d = ready.desk_id
    for method, url in (("get", f"/api/desks/{d}/orders"), ("get", f"/api/desks/{d}/orders/log"), ("get", f"/api/desks/{d}/orders/pending"),
                        ("post", f"/api/desks/{d}/orders/preview"), ("post", f"/api/desks/{d}/orders"),
                        ("get", f"/api/desks/{d}/orders/5"), ("put", f"/api/desks/{d}/orders/5"),
                        ("post", f"/api/desks/{d}/orders/5/cancel"), ("post", f"/api/desks/{d}/orders/pending/1/approve"),
                        ("post", f"/api/desks/{d}/orders/pending/1/reject"), ("get", f"/api/desks/{d}/positions"),
                        ("get", f"/api/desks/{d}/limits"), ("put", f"/api/desks/{d}/limits")):
        res = getattr(client, method)(url, headers={"X-Hokma-Token": "no"})
        assert res.status_code == 401 and code(res) == "bad_token", url
    assert calls(ready.fake) == []


def test_preview_place_replace_status_cancel_list_and_log_over_http(client, ready):
    d = ready.desk_id
    ready.fake.on("GET", ORDERS, [order_body(555)])
    ready.fake.on("GET", f"{ORDERS}/555", order_body(555))
    previewed = client.post(f"/api/desks/{d}/orders/preview", json={"order": limit()})
    assert previewed.status_code == 200 and previewed.json()["rule"]["result"] == "auto"
    placed = client.post(f"/api/desks/{d}/orders", json={"order": limit(), "reason": "from the app"})
    assert placed.status_code == 200 and placed.json()["status"] == "sent" and placed.json()["approved_by"] == "owner"
    assert rows(ready)[0].agent_slug is None and rows(ready)[0].reason == "from the app"
    created(ready.fake, 556, f"{ORDERS}/555", "PUT")
    replaced = client.put(f"/api/desks/{d}/orders/555", json={"order": limit("BUY", 1, "9"), "reason": "reprice"})
    assert replaced.json()["status"] == "replaced" and replaced.json()["order_id"] == 556
    ready.fake.on("DELETE", f"{ORDERS}/555", (200, None))
    assert client.post(f"/api/desks/{d}/orders/555/cancel", json={"reason": "done"}).json()["status"] == "canceled"
    listed = client.get(f"/api/desks/{d}/orders", params={"status": "WORKING", "maxResults": 3, "fromEnteredTime": "2026-11-01T00:00:00.000Z"})
    assert listed.status_code == 200 and [o["orderId"] for o in listed.json()["orders"]] == [555] and listed.json()["account_ref"] == "A1"
    one = client.get(f"/api/desks/{d}/orders/555")
    assert one.json()["order"]["orderId"] == 555
    log = client.get(f"/api/desks/{d}/orders/log", params={"limit": 2})
    assert [o["action"] for o in log.json()["orders"]] == ["cancel", "replace"]
    for res in (previewed, placed, replaced, listed, one, log):
        assert not any(secret in res.text for secret in SECRETS)


def test_positions_over_http(client, ready):
    ready.fake.on("GET", ACCOUNT, account_body([pos("AAPL", long=3, value=450.0, day=-5.0, open_pl=12.0)]))
    res = client.get(f"/api/desks/{ready.desk_id}/positions")
    assert res.status_code == 200 and res.json()["positions"][0]["symbol"] == "AAPL" and res.json()["total"]["market_value"] == 450.0
    assert not any(secret in res.text for secret in SECRETS)


def test_errors_carry_a_stable_code_and_the_text_of_the_student(client, ready):
    d = ready.desk_id
    service.set_orders_enabled(ready.broker_id, False, db=ready.db)
    res = client.post(f"/api/desks/{d}/orders", json={"order": limit(), "reason": "x"})
    assert res.status_code == 409 and code(res) == "orders_off" and res.json()["detail"]["message"] == t("broker.err.orders_off")
    service.set_orders_enabled(ready.broker_id, True, db=ready.db)
    assert code(client.post(f"/api/desks/{d}/orders", json={"order": limit(), "reason": ""})) == "bad_reason"
    assert client.post(f"/api/desks/999/orders", json={"order": limit(), "reason": "x"}).status_code == 404
    assert code(client.get(f"/api/desks/{d}/orders/abc")) == "bad_order_id"
    ready.fake.on("POST", PREVIEW_PATH, preview_body(rejects=["no"]))
    refused = client.post(f"/api/desks/{d}/orders", json={"order": limit(), "reason": "x"})
    assert refused.status_code == 422 and code(refused) == "rejected"


def test_the_owner_in_the_app_is_not_asked_again_but_a_refusal_still_applies(client, ready):
    set_capital(ready, 1000)
    ready.fake.on("POST", PREVIEW_PATH, preview_body(value=1500.0))
    res = client.post(f"/api/desks/{ready.desk_id}/orders", json={"order": limit("BUY", 150), "reason": "mine"})
    assert res.json()["status"] == "sent" and res.json()["approved_by"] == "owner"


def test_pending_orders_are_listed_approved_and_rejected_from_the_desk(client, ready):
    d = ready.desk_id
    set_capital(ready, 1000)
    ready.fake.on("POST", PREVIEW_PATH, preview_body(value=1500.0))
    first = service.place(d, limit("BUY", 150), "one", Who(agent="desk-chief"), db=ready.db)
    second = service.place(d, limit("BUY", 151), "two", Who(agent="desk-chief"), db=ready.db)
    pending = client.get(f"/api/desks/{d}/orders/pending").json()["pending"]
    assert [p["decision_id"] for p in pending] == [first["decision_id"], second["decision_id"]]
    assert pending[0]["order"] == limit("BUY", 150) and pending[0]["reason"] == "one" and pending[0]["agent"] == "desk-chief"
    assert pending[0]["action"] == "place" and "1,500.00" in pending[0]["why"] and pending[0]["expires_at"] > pending[0]["created_at"]
    approved = client.post(f"/api/desks/{d}/orders/pending/{first['decision_id']}/approve")
    assert approved.status_code == 200 and approved.json()["status"] == "sent" and approved.json()["approved_by"] == "owner"
    rejected = client.post(f"/api/desks/{d}/orders/pending/{second['decision_id']}/reject")
    assert rejected.json() == {"status": "rejected", "decision_id": second["decision_id"]}
    assert client.get(f"/api/desks/{d}/orders/pending").json() == {"pending": []}
    again = client.post(f"/api/desks/{d}/orders/pending/{first['decision_id']}/approve")
    assert again.status_code == 409 and code(again) == "decision_closed"
    assert client.post(f"/api/desks/{d}/orders/pending/99/approve").status_code == 404


def test_a_decision_of_another_desk_is_not_found_from_this_desk(client, ready):
    from desks_support import make_desk

    set_capital(ready, 1000)
    ready.fake.on("POST", PREVIEW_PATH, preview_body(value=1500.0))
    held = service.place(ready.desk_id, limit("BUY", 150), "one", Who(agent="desk-chief"), db=ready.db)
    other = make_desk(ready.db, name="Otra")
    assert client.post(f"/api/desks/{other['id']}/orders/pending/{held['decision_id']}/approve").status_code == 404


def test_the_limits_are_read_and_changed_with_the_rule_of_merge_and_null(client, ready):
    d = ready.desk_id
    assert client.get(f"/api/desks/{d}/limits").json() == {"limits": {}}
    assert client.put(f"/api/desks/{d}/limits", json={"limits": {"max_order_pct": 10, "assets_only": True}}).json() == {
        "limits": {"max_order_pct": 10.0, "assets_only": True}}
    assert client.put(f"/api/desks/{d}/limits", json={"limits": {"max_order_pct": None}}).json() == {"limits": {"assets_only": True}}
    bad = client.put(f"/api/desks/{d}/limits", json={"limits": {"max_order_pct": -1}})
    assert bad.status_code == 400 and code(bad) == "bad_limits"
    assert client.put(f"/api/desks/{d}/limits", json={"limits": "x"}).status_code == 400
    assert client.get(f"/api/desks/{d}/limits").json() == {"limits": {"assets_only": True}}
    assert client.get("/api/desks/999/limits").status_code == 404

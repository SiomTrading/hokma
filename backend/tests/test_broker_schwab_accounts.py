"""Schwab as a broker (task 3299, SPEC-015 18.1, 18.2 points 1 to 5 and 13): the accounts that appear when Schwab is connected,
the fingerprint that replaces the identifier, the archive on removal, the real balance, no reset, the orders switch and
the rule that no number and no hash reaches a table, a file, a log or an answer. FakeSchwab answers; nothing leaves the machine."""
import hashlib
import hmac
import json
import sqlite3
import sys
import types

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from desks_support import desks_db, make_desk  # noqa: F401  (fixtures and helper)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from schwab_support import (FAKE_ACCOUNT_NUMBER, FAKE_ACCOUNT_NUMBER_2, FAKE_HASH, FAKE_HASH_2, FakeSchwab, clear_fake,
                            load, register_accounts)

from hokma.broker import service
from hokma.broker.api import router as broker_router
from hokma.broker.contract import BaseBroker, Broker, NotSupported, OrdersNotSupported
from hokma.broker.schwab import KEY_ENTRY, SchwabBroker, balance_values, fingerprint, load_key
from hokma.broker.sim import SimBroker
from hokma.connections import runtime
from hokma.connections.service import ConnectionService, ServiceError
from hokma.connections.store import ConnectionSettings, KeyStore, StoreUnavailable
from hokma.db import get_database
from hokma.db.paths import db_path
from hokma.desks.api import router as desks_router
from hokma.market.providers import get_catalog
from hokma.rpc.ws import SESSION_TOKEN
from hokma.schwab import accounts, http
from hokma.broker import desk_accounts

TR = "/trader/v1"
SECRETS = (FAKE_ACCOUNT_NUMBER, FAKE_ACCOUNT_NUMBER_2, FAKE_HASH, FAKE_HASH_2)
MASK_1, MASK_2 = "••••5678", "••••4321"


class MemoryKeys(KeyStore):
    def __init__(self, usable: bool = True):
        self.items: dict[str, str] = {}
        self.usable = usable

    def available(self):
        return self.usable

    def get(self, provider_id):
        return self.items.get(provider_id) if self.usable else None

    def set(self, provider_id, value):
        if not self.usable:
            raise StoreUnavailable()
        self.items[provider_id] = value

    def delete(self, provider_id):
        self.items.pop(provider_id, None)


@pytest.fixture(scope="module")
def server():
    with FakeSchwab() as running:
        yield running


@pytest.fixture
def fake(server):
    clear_fake(server)
    accounts.reset()
    http.reset_limits(limit=100000)
    register_accounts(server)
    server.on("GET", f"{TR}/accounts", load("accounts.json"))
    server.on("GET", f"{TR}/accounts/{FAKE_HASH}", load("account_margin.json"))
    server.on("GET", f"{TR}/accounts/{FAKE_HASH_2}", load("account_cash.json"))
    server.on("GET", f"{TR}/userPreference", load("user_preference.json"))
    server.on("GET", f"{TR}/accounts/{FAKE_HASH}/orders", load("orders.json"))
    yield server
    accounts.reset()


@pytest.fixture
def keys(env):
    return MemoryKeys()


@pytest.fixture
def connections(keys, env):
    svc = ConnectionService(get_catalog(), store=keys, settings=ConnectionSettings(env / "connections.json"))
    runtime.install(svc, None)
    yield svc
    runtime.reset()


@pytest.fixture
def linked(desks_db, fake, connections):
    """Schwab connected: the broker and its two accounts exist."""
    connections.mark_added("schwab")
    return desks_db


def schwab_broker(u):
    return next(r for r in u.broker.list_brokers() if r.kind == "schwab")


def ids(db):
    with db.read() as u:
        return [a.id for a in u.broker.list_accounts(schwab_broker(u).id)]


# ---------------------------------------------------------------------------------------------------- the alta


def test_connecting_schwab_creates_the_broker_off_and_one_account_per_schwab_account(linked, keys):
    with linked.read() as u:
        broker = schwab_broker(u)
        assert (broker.name, broker.config) == ("Schwab", {"orders_enabled": False})
        rows = u.broker.list_accounts(broker.id)
        # both nicknames are the same in user_preference.json: the second one takes its mask
        assert [(a.name, a.initial_capital, a.status) for a in rows] == [
            ("Cuenta de prueba", 218, "active"), ("Cuenta de prueba ••••4321", 464, "active")]
        links = [u.broker.external_of(a.id) for a in rows]
    assert [x.mask for x in links] == [MASK_1, MASK_2] and all(len(x.fingerprint) == 64 for x in links)
    assert len(keys.items[KEY_ENTRY]) == 64 and bytes.fromhex(keys.items[KEY_ENTRY])


def test_the_fingerprint_is_the_hmac_of_the_hash_with_the_local_key_and_does_not_change(linked, connections, keys):
    key = bytes.fromhex(keys.items[KEY_ENTRY])
    expected = [hmac.new(key, h.encode(), hashlib.sha256).hexdigest() for h in (FAKE_HASH, FAKE_HASH_2)]
    with linked.read() as u:
        first = [u.broker.external_of(i).fingerprint for i in ids(linked)]
    assert first == expected and first == [fingerprint(key, FAKE_HASH), fingerprint(key, FAKE_HASH_2)]
    connections.mark_added("schwab")                     # again: nothing new, nothing changes
    with linked.read() as u:
        assert [u.broker.external_of(i).fingerprint for i in ids(linked)] == first
        assert len(u.broker.list_all_accounts(schwab_broker(u).id)) == 2 and len(u.broker.list_brokers()) == 2
    assert keys.items[KEY_ENTRY] == keys.items[KEY_ENTRY] and load_key() == key


def test_removing_schwab_archives_the_accounts_turns_orders_off_and_deletes_nothing(linked, connections):
    before = ids(linked)
    service.set_orders_enabled(schwab_id(linked), True, db=linked)
    connections.remove("schwab")
    with linked.read() as u:
        broker = schwab_broker(u)
        assert u.broker.list_accounts(broker.id) == []
        assert [(a.id, a.status) for a in u.broker.list_all_accounts(broker.id)] == [(i, "archived") for i in before]
        assert broker.config["orders_enabled"] is False
        assert all(u.broker.external_of(i) is not None for i in before)


def test_reconnecting_brings_the_same_accounts_back_by_fingerprint(linked, connections):
    before = ids(linked)
    connections.remove("schwab")
    connections.mark_added("schwab")
    assert ids(linked) == before
    with linked.read() as u:
        assert len(u.broker.list_all_accounts(schwab_broker(u).id)) == 2


def test_an_account_schwab_no_longer_lists_is_archived_at_the_next_sync(linked, fake, connections):
    fake.on("GET", f"{TR}/accounts/accountNumbers", [load("account_numbers.json")[0]])
    accounts.reset()
    connections.mark_added("schwab")
    assert ids(linked) == [ids(linked)[0]] and len(ids(linked)) == 1


def test_a_hand_made_broker_called_schwab_is_not_touched(desks_db, fake, connections):
    service.create_broker("Schwab", db=desks_db)
    connections.mark_added("schwab")
    with desks_db.read() as u:
        assert sorted((r.kind, r.name) for r in u.broker.list_brokers()) == [("schwab", "Schwab 2"), ("sim", "Hokma"), ("sim", "Schwab")]


def test_without_a_credential_store_it_is_store_unavailable(desks_db, fake, env):
    svc = ConnectionService(get_catalog(), store=MemoryKeys(usable=False), settings=ConnectionSettings(env / "c.json"))
    runtime.install(svc, None)
    try:
        with pytest.raises(ServiceError) as raised:
            svc.mark_added("schwab")
        assert raised.value.code == "connections.store_unavailable" and raised.value.status == 503
        with desks_db.read() as u:
            assert [r.kind for r in u.broker.list_brokers()] == ["sim"]
    finally:
        runtime.reset()


def test_a_schwab_that_cannot_be_read_leaves_no_half_made_broker(desks_db, fake, connections):
    clear_fake(fake)                                     # every route answers 404
    connections.mark_added("schwab")                     # logged, not raised: the next connect does it again
    with desks_db.read() as u:
        assert [r.kind for r in u.broker.list_brokers()] == ["sim"]


def schwab_id(db):
    with db.read() as u:
        return schwab_broker(u).id


# ---------------------------------------------------------------------------------------------- real balance


def test_the_balance_of_a_margin_account_and_of_a_cash_account_comes_from_schwab(linked):
    with linked.read() as u:
        broker = SchwabBroker(u.broker, schwab_broker(u))
        margin, cash = (broker.balance(i) for i in ids(linked))
    assert (margin.cash, margin.equity, margin.buying_power, margin.currency) == (216.25, 217.75, 216.75, "USD")
    assert (cash.cash, cash.equity) == (231.0, 232.0 + 231.75)            # cash available; total cash plus securities
    assert margin.as_of and cash.as_of


def test_balance_values_by_type_and_never_a_made_up_zero():
    assert balance_values({"type": "MARGIN", "currentBalances": {"equity": 10, "availableFunds": 4, "buyingPower": 8}}) == (4, 10, 8)
    assert balance_values({"type": "CASH", "currentBalances": {"totalCash": 5, "cashAvailableForTrading": 3}}) == (3, 5, 3)
    assert balance_values({"type": "MARGIN", "currentBalances": {}}) == (None, None, None)
    assert balance_values({}) == (None, None, None)


def test_the_balance_is_none_while_schwab_cannot_be_read(linked, fake):
    fake.on("GET", f"{TR}/accounts/{FAKE_HASH}", (503, {"errors": [{"status": "503"}]}))
    with linked.read() as u:
        broker = SchwabBroker(u.broker, schwab_broker(u))
        first = broker.balance(ids(linked)[0])
    assert (first.cash, first.equity) == (None, None)


def test_a_desk_sees_the_real_balance_of_its_schwab_account(linked):
    desk = make_desk(linked)
    first = ids(linked)[0]
    desk_accounts.add_account(desk["id"] if isinstance(desk, dict) else desk.id, first, db=linked)
    with linked.read() as u:
        card = desk_accounts.of_desk(u, desk["id"] if isinstance(desk, dict) else desk.id)["accounts"][0]
    assert card["balance"] == {"cash": 216.25, "equity": 217.75, "currency": "USD"} and card["initial_capital"] == 218
    other = make_desk(linked, name="Otra")["id"]
    desk_accounts.add_account(other, first, db=linked)                          # an account may serve many desks (SPEC-027)
    with linked.read() as u:
        assert u.broker.desks_of_account(first) == sorted([desk["id"] if isinstance(desk, dict) else desk.id, other])


def test_positions_and_orders_are_typed_and_carry_only_account_ref_and_mask(linked, fake):
    fake.on("GET", f"{TR}/accounts/{FAKE_HASH}/orders/1000000001", load("order.json"))
    with linked.read() as u:
        broker = SchwabBroker(u.broker, schwab_broker(u))
        first = ids(linked)[0]
        positions, orders = broker.positions(first), broker.orders(first)
        one = broker.get_order(first, 1000000001)
    stock, option = positions[0], positions[1]
    assert (stock.symbol, stock.qty, stock.market_value, stock.asset_type) == ("FAKE", 179.75 - 178.75, 180.75, "EQUITY")
    assert (stock.account_ref, stock.mask, stock.open_pl) == ("A1", MASK_1, 182.25 + 182.5)
    assert (option.multiplier, option.put_call, option.underlying) == (100, "CALL", "FAKE")
    assert orders and orders[0].account_ref == "A1" and orders[0].mask == MASK_1 and orders[0].symbol == "FAKE"
    assert orders[0].order_id == "1000000001" and orders[0].status == "WORKING" and orders[0].cancelable is True
    assert orders[0].legs[0]["instruction"] == "BUY" and one.account_ref == "A1" and one.order_id is not None
    blob = json.dumps([p.data for p in positions] + [o.data for o in orders])
    assert not any(secret in blob for secret in SECRETS)


# ------------------------------------------------------------------------------ contract and capabilities


def test_capabilities_and_contract(linked):
    with linked.read() as u:
        schwab, sim = SchwabBroker(u.broker, schwab_broker(u)), SimBroker(u.broker, u.broker.find_broker("Hokma"))
    assert isinstance(schwab, Broker) and isinstance(sim, Broker) and schwab.kind == "schwab"
    caps = schwab.capabilities
    assert (caps.accounts, caps.reset, caps.orders, caps.positions) == (True, False, True, True)
    assert (sim.capabilities.orders, sim.capabilities.positions) == (True, True)       # SPEC-018
    with linked.read() as u:
        plain = BaseBroker(u.broker, u.broker.find_broker("Hokma"))
    for call in (plain.place_order, plain.replace_order, plain.cancel_order, plain.preview_order, plain.orders, plain.get_order, plain.positions):
        with pytest.raises(OrdersNotSupported):
            call()
    with pytest.raises(NotSupported):
        schwab.reset(ids(linked)[0])
    with pytest.raises(NotSupported):
        schwab.create_account("x", 1000)


def test_the_four_writes_only_delegate_with_the_account_ref(linked, monkeypatch):
    calls = []
    fake_orders = types.ModuleType("hokma.schwab.orders")
    fake_orders.place_order = lambda ref, order: calls.append(("place", ref, order)) or {"order_id": "7", "correl_id": None}
    fake_orders.replace_order = lambda ref, oid, order: calls.append(("replace", ref, oid, order)) or {"order_id": "8", "correl_id": None}
    fake_orders.cancel_order = lambda ref, oid: calls.append(("cancel", ref, oid)) or {"correl_id": "c"}
    fake_orders.preview_order = lambda ref, order: calls.append(("preview", ref, order)) or "preview"
    import hokma.schwab as package
    monkeypatch.setitem(sys.modules, "hokma.schwab.orders", fake_orders)
    monkeypatch.setattr(package, "orders", fake_orders, raising=False)
    with linked.read() as u:
        broker = SchwabBroker(u.broker, schwab_broker(u))
        second = ids(linked)[1]
        order = {"orderType": "MARKET"}
        assert broker.place_order(second, order)["order_id"] == "7"
        assert broker.replace_order(second, "5", order)["order_id"] == "8"
        assert broker.cancel_order(second, "5") == {"correl_id": "c"}
        assert broker.preview_order(second, order) == "preview"
    assert calls == [("place", "A2", order), ("replace", "A2", "5", order), ("cancel", "A2", "5"), ("preview", "A2", order)]
    assert not any(secret in json.dumps(calls) for secret in SECRETS)


# ----------------------------------------------------------------------------------------------------- the API


@pytest.fixture
def client(linked):
    app = FastAPI()
    app.include_router(desks_router)
    app.include_router(broker_router)
    with TestClient(app) as c:
        c.headers.update({"X-Hokma-Token": SESSION_TOKEN})
        yield c


def listed(client):
    return {b["kind"]: b for b in client.get("/api/brokers").json()["brokers"]}


def test_brokers_list_capabilities_the_switch_and_per_account_can_reset_and_mask(client):
    brokers = listed(client)
    assert brokers["schwab"]["orders_enabled"] is False and brokers["sim"]["orders_enabled"] is False
    assert brokers["schwab"]["capabilities"] == {"accounts": True, "reset": False, "orders": True, "positions": True}
    assert brokers["sim"]["capabilities"]["orders"] is True
    accounts_ = client.get(f"/api/brokers/{brokers['schwab']['id']}/accounts").json()["accounts"]
    assert [(a["mask"], a["can_reset"]) for a in accounts_] == [(MASK_1, False), (MASK_2, False)]
    assert (accounts_[0]["cash"], accounts_[0]["equity"]) == (216.25, 217.75)
    made = client.post(f"/api/brokers/{brokers['sim']['id']}/accounts", json={"name": "Swing", "capital": 5000}).json()
    assert made["can_reset"] is True and made["mask"] is None


def test_the_orders_switch_goes_on_and_off_only_for_a_broker_with_orders(client):
    brokers = listed(client)
    url = f"/api/brokers/{brokers['schwab']['id']}/orders-enabled"
    on = client.put(url, json={"enabled": True})
    assert on.status_code == 200 and on.json()["orders_enabled"] is True and listed(client)["schwab"]["orders_enabled"] is True
    assert service.schwab_orders_enabled() is True
    assert client.put(url, json={"enabled": False}).json()["orders_enabled"] is False
    for body in ({"enabled": "yes"}, {"enabled": 1}, {}):
        assert client.put(url, json=body).status_code == 422
    accepted = client.put(f"/api/brokers/{brokers['sim']['id']}/orders-enabled", json={"enabled": True})      # SPEC-018: the simulated broker has the switch too
    assert accepted.status_code == 200 and accepted.json()["orders_enabled"] is True
    client.put(f"/api/brokers/{brokers['sim']['id']}/orders-enabled", json={"enabled": False})
    assert client.put("/api/brokers/9999/orders-enabled", json={"enabled": True}).status_code == 404
    assert client.put(url, json={"enabled": True}, headers={"X-Hokma-Token": "no"}).status_code == 401


def test_a_schwab_account_is_not_reset_in_the_service_nor_in_the_route(client, linked):
    first = ids(linked)[0]
    res = client.post(f"/api/accounts/{first}/reset", json={"confirmed": True})
    assert res.status_code == 409 and res.json()["detail"]["code"] == "not_supported"
    with pytest.raises(service.BrokerError) as raised:
        service.reset_account(first, True, db=linked)
    assert raised.value.code == "not_supported"
    broker_id = listed(client)["schwab"]["id"]
    assert client.post(f"/api/brokers/{broker_id}/accounts", json={"name": "Nueva", "capital": 1000}).status_code == 409
    assert client.put(f"/api/brokers/{broker_id}/costs", json={"commission": None}).status_code == 409
    with linked.read() as u:
        assert len(u.broker.ledger(first)) == 1                                 # nothing was written


def test_the_connections_panel_carries_the_orders_switch(client, fake):
    from hokma.connections.api import router as connections_router

    app = FastAPI()
    app.include_router(connections_router)
    schwab = listed(client)["schwab"]["id"]
    fake.on("GET", f"{TR}/orders", load("orders.json"))
    with TestClient(app) as c:
        url, auth = "/api/connections/schwab/account", {"X-Hokma-Token": SESSION_TOKEN}
        first = c.get(url, headers=auth)
        assert first.status_code == 200, first.text
        assert first.json()["orders_enabled"] is False
        client.put(f"/api/brokers/{schwab}/orders-enabled", json={"enabled": True})
        body = c.get(url, headers=auth).json()
        assert body["orders_enabled"] is True and body["accounts"]


# ------------------------------------------------------------------------------ no identifier anywhere


def test_no_number_and_no_hash_in_any_table_file_log_or_answer(linked, client, caplog, keys, env):
    caplog.set_level("DEBUG")
    desk = make_desk(linked)
    desk_accounts.add_account(desk["id"] if isinstance(desk, dict) else desk.id, ids(linked)[0], db=linked)
    answers = [client.get("/api/brokers").text, client.get(f"/api/accounts/{ids(linked)[0]}").text,
               client.get(f"/api/brokers/{listed(client)['schwab']['id']}/accounts").text]
    with linked.read() as u:
        broker = SchwabBroker(u.broker, schwab_broker(u))
        answers.append(json.dumps([broker.positions(ids(linked)[0]), broker.orders(ids(linked)[0])], default=lambda o: o.__dict__ if hasattr(o, "__dict__") else str(o)))
        answers.append(str(u.broker.list_all_accounts(schwab_broker(u).id)))
    con = sqlite3.connect(str(db_path()))
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
    dump = "\n".join(repr(con.execute(f"SELECT * FROM {name}").fetchall()) for name in tables if not name.startswith("doc_chunk"))
    con.close()
    files = "\n".join(p.read_bytes().decode("latin-1") for p in env.rglob("*") if p.is_file())
    stored = "\n".join(keys.items.values())
    for where, text in (("answers", "\n".join(answers)), ("tables", dump), ("files", files), ("store", stored),
                        ("log", caplog.text)):
        for secret in SECRETS:
            assert secret not in text, f"{secret[:4]}... found in {where}"

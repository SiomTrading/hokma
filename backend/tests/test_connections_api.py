"""Connections over HTTP: the cards, keys, switches, priority and "Test connection" (C2, C3, C4, C7) against fake
providers. The system credential store is an in-memory backend, or the "no store" one for C4."""
import json
import time

import keyring
import pytest
from keyring.backends import fail

from hokma.core.config import hokma_data_dir
from hokma.market import hub as hub_module
from hokma.market.adapters import ADAPTERS
from live_fixtures import AUTH, WS_URL, Collector, live  # noqa: F401  (fixture)
from live_support import (FakeDatabento, FakeWs, alpaca_script, coinbase_script, finnhub_script, trade_coinbase,
                          wait_until)
from test_live_market import FINNHUB_KEY, KEY_ID, SECRET, iso, save_alpaca, save_finnhub, subscribe
from market_support import RECORDED_AT

PROVIDERS = ["coinbase", "alpaca", "finnhub", "siomtrading_futures", "databento", "massive"]


@pytest.fixture(autouse=True)
def quick_ack(monkeypatch):
    monkeypatch.setattr(hub_module, "ACK_GRACE", 0.2)


def cards(client) -> dict[str, dict]:
    body = client.get("/api/connections", headers=AUTH).json()
    return {c["id"]: c for c in body["connections"]}


def test_the_six_cards_are_listed_with_their_fields(live):
    with live.app() as client:
        body = client.get("/api/connections", headers=AUTH).json()
        assert [c["id"] for c in body["connections"]] == PROVIDERS
        by_id = {c["id"]: c for c in body["connections"]}
        for card in body["connections"]:
            for field in ("id", "name", "asset_classes", "needs_key", "key_fields", "key_state", "enabled", "state",
                          "data_state", "error_code", "symbols_in_use", "max_symbols", "priority"):
                assert field in card, field
        assert by_id["coinbase"]["needs_key"] is False and by_id["coinbase"]["key_state"] == "not_required"
        assert by_id["coinbase"]["enabled"] is True and by_id["coinbase"]["asset_classes"] == ["crypto"]
        assert by_id["alpaca"]["key_fields"] == ["key_id", "secret"] and by_id["alpaca"]["max_symbols"] == 30
        assert by_id["finnhub"]["max_symbols"] == 50 and by_id["finnhub"]["data_state"] == "live"
        assert by_id["siomtrading_futures"]["state"] == "not_connected"
        assert by_id["siomtrading_futures"]["needs_key"] is True and by_id["siomtrading_futures"]["available"] is True
        assert by_id["databento"]["premium"] and by_id["massive"]["premium"]
        assert all(c["key_state"] == "missing" for c in body["connections"] if c["needs_key"])
        assert body["groups"] == {"equity": ["alpaca", "finnhub", "databento"], "crypto": ["coinbase", "finnhub"],
                                  "forex": ["finnhub"], "future": ["siomtrading_futures", "databento", "massive"]}
        assert body["store_available"] is True
        assert by_id["finnhub"]["priority"] == {"equity": 1, "forex": 0, "crypto": 1}


def test_every_route_needs_the_session_token(live):
    with live.app() as client:
        for method, path in (("get", "/api/connections"), ("put", "/api/connections/alpaca/key"),
                             ("delete", "/api/connections/alpaca/key"), ("put", "/api/connections/alpaca/enabled"),
                             ("put", "/api/connections/priority"), ("post", "/api/connections/alpaca/test")):
            assert getattr(client, method)(path).status_code == 401, path
            assert getattr(client, method)(path, headers={"X-Hokma-Token": "nope"}).status_code == 401, path
        assert client.get("/api/connections", headers={"host": "evil.example"}).status_code == 403
        assert client.get(f"/api/connections?token={AUTH['X-Hokma-Token']}").status_code == 200


def test_an_unknown_provider_is_a_catalog_404(live):
    with live.app() as client:
        for response in (client.put("/api/connections/nope/key", json={"key": "abcd1234"}, headers=AUTH),
                         client.delete("/api/connections/nope/key", headers=AUTH),
                         client.put("/api/connections/nope/enabled", json={"enabled": True}, headers=AUTH),
                         client.post("/api/connections/nope/test", headers=AUTH)):
            assert response.status_code == 404 and response.json()["detail"]["code"] == "connections.unknown_provider"


# ------------------------------------------------------------------ keys (C3, C4)


def data_files() -> list:
    root = hokma_data_dir()
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


def test_c3_a_saved_key_is_in_the_system_store_and_nowhere_else(live):
    with live.app() as client:
        response = client.put("/api/connections/alpaca/key", json={"key_id": KEY_ID, "secret": SECRET}, headers=AUTH)
        assert response.status_code == 200 and response.json() == {"id": "alpaca", "key_state": "saved"}
        for secret in (KEY_ID, SECRET):
            assert secret not in response.text
        assert cards(client)["alpaca"]["key_state"] == "saved"
        assert secret_not_in(client.get("/api/connections", headers=AUTH).text)
        stored = live.keyring.items[("hokma-market", "alpaca")]
        assert json.loads(stored) == {"key_id": KEY_ID, "secret": SECRET}
        for path in data_files():
            body = path.read_bytes()
            assert KEY_ID.encode() not in body and SECRET.encode() not in body, path
        state = json.loads((hokma_data_dir() / "connections.json").read_text()) if (
            hokma_data_dir() / "connections.json").exists() else {}
        assert set(state) <= {"version", "enabled", "priority"}
        client.put("/api/connections/finnhub/key", json={"key": FINNHUB_KEY}, headers=AUTH)
        assert live.keyring.items[("hokma-market", "finnhub")] == FINNHUB_KEY
        removed = client.delete("/api/connections/alpaca/key", headers=AUTH)
        assert removed.status_code == 200 and removed.json()["key_state"] == "missing"
        assert ("hokma-market", "alpaca") not in live.keyring.items
        assert cards(client)["alpaca"]["key_state"] == "missing"


def secret_not_in(text: str) -> bool:
    return not any(s in text for s in (KEY_ID, SECRET, FINNHUB_KEY))


def test_a_malformed_key_is_refused_without_being_repeated(live):
    with live.app() as client:
        for body in ({"key": "has spaces in it"}, {"key": "x"}, {"key": ""}, {"nokey": "abcdefgh"},
                     {"key": "a" * 300}, {"key": 12345678}):
            response = client.put("/api/connections/finnhub/key", json=body, headers=AUTH)
            assert response.status_code == 422 and response.json()["detail"]["code"] == "connections.bad_key"
            for value in body.values():
                if isinstance(value, str) and len(value) > 3:
                    assert value not in response.text
        half = client.put("/api/connections/alpaca/key", json={"key_id": KEY_ID}, headers=AUTH)
        assert half.status_code == 422
        assert client.put("/api/connections/finnhub/key", content=b"not json", headers=AUTH).status_code == 400
        assert client.put("/api/connections/finnhub/key", content=b"x" * 5000, headers=AUTH).status_code == 413
        assert client.put("/api/connections/coinbase/key", json={"key": "abcdefgh"}, headers=AUTH).status_code == 400
        assert ("hokma-market", "finnhub") not in live.keyring.items


def test_c4_without_a_system_credential_store_nothing_is_saved(live):
    keyring.set_keyring(fail.Keyring())
    with live.app() as client:
        assert client.get("/api/connections", headers=AUTH).json()["store_available"] is False
        response = client.put("/api/connections/alpaca/key", json={"key_id": KEY_ID, "secret": SECRET}, headers=AUTH)
        detail = response.json()["detail"]
        assert response.status_code == 503 and detail["code"] == "connections.store_unavailable"
        assert detail["message"] == "System credential store not available"
        assert cards(client)["alpaca"]["key_state"] == "missing"
        for path in data_files():
            body = path.read_bytes()
            assert KEY_ID.encode() not in body and SECRET.encode() not in body, path
        assert not any(p.name.startswith(("key", "secret")) for p in data_files())


def test_c4_a_store_that_discards_or_keeps_plain_text_is_not_a_store(live):
    from keyring.backends import null
    from hokma.connections.store import KeyStore

    keyring.set_keyring(null.Keyring())
    assert KeyStore().available() is False
    keyring.set_keyring(live.keyring)
    assert KeyStore().available() is True


def test_c4_the_spanish_text_exists_too(live, monkeypatch):
    monkeypatch.setenv("HOKMA_LANG", "es")
    keyring.set_keyring(fail.Keyring())
    with live.app() as client:
        response = client.put("/api/connections/finnhub/key", json={"key": FINNHUB_KEY}, headers=AUTH)
        from hokma.i18n import catalog

        message = response.json()["detail"]["message"]
        assert message == catalog("es")["connections.error.store_unavailable"]
        assert message != catalog("en")["connections.error.store_unavailable"]


# ------------------------------------------------------------------ switches and priority


def test_the_switch_and_the_priority_are_saved_without_any_key(live):
    with live.app() as client:
        card = client.put("/api/connections/coinbase/enabled", json={"enabled": False}, headers=AUTH).json()
        assert card["enabled"] is False and card["id"] == "coinbase"
        assert client.put("/api/connections/coinbase/enabled", json={"enabled": "no"}, headers=AUTH).status_code == 422
        ok = client.put("/api/connections/priority", headers=AUTH,
                        json={"asset_class": "future", "order": ["massive", "databento", "siomtrading_futures"]})
        assert ok.status_code == 200 and ok.json()["groups"]["future"][0] == "massive"
        for bad in ({"asset_class": "future", "order": ["massive"]},
                    {"asset_class": "future", "order": ["massive", "massive", "databento"]},
                    {"asset_class": "future", "order": ["alpaca", "databento", "massive"]},
                    {"asset_class": "nope", "order": []}, {"order": []}):
            response = client.put("/api/connections/priority", headers=AUTH, json=bad)
            assert response.status_code == 422 and response.json()["detail"]["code"] == "connections.bad_priority"
        saved = json.loads((hokma_data_dir() / "connections.json").read_text())
        assert saved["enabled"]["coinbase"] is False and saved["priority"]["future"][0] == "massive"
    with live.app() as again:                                           # it survives a new engine run
        assert cards(again)["coinbase"]["enabled"] is False
        assert again.get("/api/connections", headers=AUTH).json()["groups"]["future"][0] == "massive"


# ------------------------------------------------------------------ test connection (C2)


def test_c2_a_healthy_provider_answers_connected_live_with_its_latency(live):
    coinbase = FakeWs(coinbase_script).start()
    try:
        live.use({"coinbase": coinbase.url})
        with live.app() as client:
            result = client.post("/api/connections/coinbase/test", headers=AUTH).json()
            assert result["ok"] is True and result["state"] == "connected" and result["data_state"] == "live"
            assert isinstance(result["latency_ms"], int) and result["latency_ms"] >= 0
            assert result["first_data"] is True and result["symbol"] == "BTC-USD" and result["error_code"] is None
            assert cards(client)["coinbase"]["state"] == "connected"
    finally:
        coinbase.stop()


def test_c2_alpaca_ok_with_a_silent_market_and_a_wrong_key_is_a_catalog_error(live):
    alpaca = FakeWs(alpaca_script(KEY_ID, SECRET)).start()
    try:
        live.use({"alpaca": alpaca.url})
        with live.app() as client:
            save_alpaca(client)
            ok = client.post("/api/connections/alpaca/test", headers=AUTH).json()
            assert ok["ok"] is True and ok["first_data"] is False and ok["data_state"] == "live"
            assert ok["symbol"] == "SPY"
            client.put("/api/connections/alpaca/key", json={"key_id": KEY_ID, "secret": "the-wrong-secret-x"},
                       headers=AUTH)
            bad = client.post("/api/connections/alpaca/test", headers=AUTH).json()
            assert bad["ok"] is False and bad["error_code"] == "connections.auth_failed" and bad["message"]
            assert bad["state"] == "error" and cards(client)["alpaca"]["error_code"] == "connections.auth_failed"
            assert "the-wrong-secret-x" not in json.dumps(bad)
            client.put("/api/connections/alpaca/key", json={"key_id": KEY_ID, "secret": SECRET}, headers=AUTH)
            assert cards(client)["alpaca"]["state"] != "error"            # a new key clears the error
            assert client.post("/api/connections/alpaca/test", headers=AUTH).json()["ok"] is True
    finally:
        alpaca.stop()


def test_c2_finnhub_refusing_the_handshake_is_a_key_error(live):
    finnhub = FakeWs(finnhub_script, reject_token="the-key-it-accepts").start()
    try:
        live.use({"finnhub": finnhub.url})
        with live.app() as client:
            save_finnhub(client)
            bad = client.post("/api/connections/finnhub/test", headers=AUTH).json()
            assert bad["ok"] is False and bad["error_code"] == "connections.auth_failed"
            assert FINNHUB_KEY not in json.dumps(bad)
    finally:
        finnhub.stop()


def test_c2_finnhub_ok_when_a_trade_arrives_and_the_state_is_the_one_in_the_providers_file(live):
    async def script(ws, fake):
        async for raw in ws:
            message = json.loads(raw)
            fake.received.append((ws.request.path, message))
            if message.get("type") == "subscribe":
                await ws.send(json.dumps({"type": "trade", "data": [
                    {"p": 80500.5, "s": message["symbol"], "t": int(RECORDED_AT * 1000), "v": 0.1}]}))
    finnhub = FakeWs(script, reject_token=FINNHUB_KEY).start()
    try:
        live.use({"finnhub": finnhub.url})
        with live.app() as client:
            save_finnhub(client)
            result = client.post("/api/connections/finnhub/test", headers=AUTH).json()
            assert result["ok"] is True and result["first_data"] is True and result["data_state"] == "live"
            assert result["symbol"] == "BTC-USD"
            assert any(m.get("symbol") == "BINANCE:BTCUSDT" for m in finnhub.messages())
    finally:
        finnhub.stop()


def test_c2_massive_and_databento_with_fake_servers(live):
    async def massive(ws, fake):
        await ws.send(json.dumps([{"ev": "status", "status": "connected"}]))
        async for raw in ws:
            message = json.loads(raw)
            fake.received.append((ws.request.path, message))
            if message.get("action") == "auth":
                good = message["params"] == "massive-key-24680"
                await ws.send(json.dumps([{"ev": "status", "status": "auth_success" if good else "auth_failed"}]))
            elif message.get("action") == "subscribe":
                sym = message["params"].split(",")[0].split(".", 1)[1]
                await ws.send(json.dumps([{"ev": "T", "sym": sym, "p": 28200.25, "s": 2, "t": int(RECORDED_AT * 1000)}]))
    server = FakeWs(massive).start()
    gateway = FakeDatabento("db-" + "k" * 29).start()
    try:
        live.use({"massive": server.url, "databento": f"tcp://127.0.0.1:{gateway.port}"})
        with live.app() as client:
            client.put("/api/connections/massive/key", json={"key": "massive-key-24680"}, headers=AUTH)
            ok = client.post("/api/connections/massive/test", headers=AUTH).json()
            assert ok["ok"] is True and ok["symbol"] == "NQ=F" and ok["data_state"] == "live"
            client.put("/api/connections/massive/key", json={"key": "another-key-13579"}, headers=AUTH)
            assert client.post("/api/connections/massive/test", headers=AUTH).json()["error_code"] == \
                "connections.auth_failed"
            client.put("/api/connections/databento/key", json={"key": "db-" + "k" * 29}, headers=AUTH)
            ok = client.post("/api/connections/databento/test", headers=AUTH).json()
            assert ok["ok"] is True or ok["first_data"] is False
            gateway.push_trade(28200.5)
            client.put("/api/connections/databento/key", json={"key": "db-" + "z" * 29}, headers=AUTH)
            bad = client.post("/api/connections/databento/test", headers=AUTH).json()
            assert bad["ok"] is False and bad["error_code"] == "connections.auth_failed"
    finally:
        server.stop()
        gateway.stop()


def test_c2_without_a_network_the_answer_is_an_error_within_ten_seconds(live):
    with live.app() as client:                                           # the default hosts are all refused
        started = time.monotonic()
        result = client.post("/api/connections/coinbase/test", headers=AUTH).json()
        assert time.monotonic() - started <= 10
        assert result["ok"] is False and result["error_code"] == "connections.unreachable" and result["message"]


def test_c2_a_provider_that_never_answers_times_out_in_ten_seconds_at_most(live, monkeypatch):
    async def silent(ws, fake):
        async for raw in ws:
            fake.received.append((ws.request.path, json.loads(raw)))
    server = FakeWs(silent).start()
    monkeypatch.setattr(hub_module, "TEST_TIMEOUT", 1.0)
    try:
        live.use({"coinbase": server.url})
        with live.app() as client:
            started = time.monotonic()
            result = client.post("/api/connections/coinbase/test", headers=AUTH).json()
            assert 0.9 <= time.monotonic() - started <= 3
            assert result["ok"] is False and result["error_code"] == "connections.timeout"
    finally:
        server.stop()


def test_c2_a_card_without_a_key_or_without_an_adapter_says_why(live, monkeypatch):
    monkeypatch.delitem(ADAPTERS, "siomtrading_futures")
    with live.app() as client:
        assert client.post("/api/connections/alpaca/test", headers=AUTH).json()["error_code"] == "connections.no_key"
        client.put("/api/connections/siomtrading_futures/key", json={"key": "token-value-1234"}, headers=AUTH)
        card = cards(client)["siomtrading_futures"]
        assert card["key_state"] == "saved" and card["state"] == "not_connected" and card["available"] is False
        result = client.post("/api/connections/siomtrading_futures/test", headers=AUTH).json()
        assert result["ok"] is False and result["error_code"] == "connections.unavailable"


# ------------------------------------------------------------------ live state (C7)


def test_c7_the_card_follows_the_provider_down_and_back_without_reloading(live):
    coinbase = FakeWs(coinbase_script).start()
    try:
        live.use({"coinbase": coinbase.url})
        with live.app() as client, live.connect(client) as ws:
            c = Collector(ws)
            client.post("/api/connections/coinbase/test", headers=AUTH)
            c.find(lambda m: m["type"] == "connection_status" and m["provider"] == "coinbase"
                   and m["state"] == "connected")
            mark = len(c.all)
            coinbase.stop()
            down = c.find(lambda m: m["type"] == "connection_status" and m["provider"] == "coinbase"
                          and m["state"] == "reconnecting", after=mark)
            assert down["enabled"] is True and down["key_state"] == "not_required"
            assert cards(client)["coinbase"]["state"] == "reconnecting"
            mark = len(c.all)
            coinbase.restart()
            c.find(lambda m: m["type"] == "connection_status" and m["provider"] == "coinbase"
                   and m["state"] == "connected", after=mark)
            assert wait_until(lambda: cards(client)["coinbase"]["state"] == "connected")
    finally:
        coinbase.stop()


def test_switching_a_provider_off_closes_its_connection_and_the_card_says_not_connected(live):
    coinbase = FakeWs(coinbase_script).start()
    try:
        live.use({"coinbase": coinbase.url})
        with live.app() as client, live.connect(client) as ws:
            c = Collector(ws)
            client.post("/api/connections/coinbase/test", headers=AUTH)
            client.put("/api/connections/coinbase/enabled", json={"enabled": False}, headers=AUTH)
            status = c.find(lambda m: m["type"] == "connection_status" and m["provider"] == "coinbase"
                            and m["enabled"] is False)
            assert wait_until(lambda: cards(client)["coinbase"]["state"] == "not_connected")
            assert wait_until(lambda: not coinbase.clients)
            assert status["state"] in ("not_connected", "connected")
    finally:
        coinbase.stop()


def test_symbols_in_use_counts_the_open_subscriptions_of_a_provider(live):
    coinbase = FakeWs(coinbase_script).start()
    try:
        live.use({"coinbase": coinbase.url})
        with live.app() as client, live.connect(client) as ws:
            c = Collector(ws)
            subscribe(c, "a", "BTC-USD", "1h")
            assert cards(client)["coinbase"]["symbols_in_use"] == 1
            coinbase.send_all(trade_coinbase("BTC-USD", 80400.0, iso(int(RECORDED_AT * 1000) + 3000)))
            c.send({"op": "unsubscribe", "sub": "a"})
            assert wait_until(lambda: cards(client)["coinbase"]["symbols_in_use"] == 0)
    finally:
        coinbase.stop()

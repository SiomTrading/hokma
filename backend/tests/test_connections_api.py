"""Connections over HTTP: the cards, keys, switches, priority and "Test connection" (C2, C3, C4, C7) against fake
providers. The system credential store is an in-memory backend, or the "no store" one for C4."""
import json
import threading
import time

import keyring
import pytest
from keyring.backends import fail

from hokma.core.config import hokma_data_dir
from hokma.market import hub as hub_module
from hokma.market.adapters import ADAPTERS
from live_fixtures import AUTH, WS_URL, Collector, live  # noqa: F401  (fixture)
from live_support import (FakeWs, alpaca_script, coinbase_script, finnhub_script, trade_coinbase,
                          wait_until)
from test_live_market import FINNHUB_KEY, KEY_ID, SECRET, iso, save_alpaca, save_finnhub, subscribe
from market_support import RECORDED_AT

PROVIDERS = ["coinbase", "alpaca", "finnhub", "siomtrading_futures", "schwab"]


@pytest.fixture(autouse=True)
def quick_ack(monkeypatch):
    monkeypatch.setattr(hub_module, "ACK_GRACE", 0.2)


@pytest.fixture(autouse=True)
def fresh_limits(monkeypatch):
    """The route limits are per process: every test starts with a full allowance."""
    from hokma.connections import api as connections_api
    from hokma.market.cache import RateLimiter

    monkeypatch.setattr(connections_api, "_tests", RateLimiter(limit=20, window=60.0))
    monkeypatch.setattr(connections_api, "_finishes", RateLimiter(limit=10, window=60.0))


def cards(client) -> dict[str, dict]:
    body = client.get("/api/connections", headers=AUTH).json()
    return {c["id"]: c for c in body["connections"]}


def test_the_cards_are_listed_with_their_fields(live):
    with live.app() as client:
        body = client.get("/api/connections", headers=AUTH).json()
        assert [c["id"] for c in body["connections"]] == PROVIDERS
        by_id = {c["id"]: c for c in body["connections"]}
        for card in body["connections"]:
            for field in ("id", "name", "asset_classes", "needs_key", "key_fields", "key_state", "enabled", "state",
                          "data_state", "error_code", "symbols_in_use", "max_symbols", "priority", "added", "auth",
                          "uses"):
                assert field in card, field
        assert by_id["coinbase"]["needs_key"] is False and by_id["coinbase"]["key_state"] == "not_required"
        assert by_id["coinbase"]["enabled"] is True and by_id["coinbase"]["asset_classes"] == ["crypto"]
        assert by_id["alpaca"]["key_fields"] == ["key_id", "secret"] and by_id["alpaca"]["max_symbols"] == 30
        assert by_id["finnhub"]["max_symbols"] == 50 and by_id["finnhub"]["data_state"] == "live"
        assert by_id["siomtrading_futures"]["state"] == "not_connected"
        assert by_id["siomtrading_futures"]["needs_key"] is True and by_id["siomtrading_futures"]["available"] is True
        assert all("premium" not in c for c in body["connections"])
        assert all(c["key_state"] == "missing" for c in body["connections"] if c["needs_key"])
        assert body["groups"] == {"equity": ["schwab", "alpaca", "finnhub"], "crypto": ["coinbase", "finnhub"],
                                  "forex": ["finnhub"], "future": ["siomtrading_futures"]}
        assert body["uses"] == {"equity": ["schwab", "alpaca", "finnhub"], "bars": ["schwab", "alpaca", "finnhub"],
                                "options": ["schwab", "alpaca"], "future": ["siomtrading_futures"],
                                "crypto": ["coinbase", "finnhub"], "forex": ["finnhub"], "news": ["finnhub", "alpaca"]}
        assert body["store_available"] is True
        assert by_id["finnhub"]["priority"] == {"equity": 2, "forex": 0, "crypto": 1}
        assert by_id["schwab"]["auth"] == "oauth" and by_id["schwab"]["key_fields"] == ["client_id", "client_secret"]
        assert by_id["schwab"]["uses"] == ["equity", "bars", "options"] and by_id["schwab"]["max_symbols"] == 100
        assert by_id["schwab"]["session"]["state"] == "signed_out" and by_id["schwab"]["added"] is False
        assert all(c["auth"] == "key" for c in body["connections"] if c["id"] != "schwab")
        assert all("session" not in c for c in body["connections"] if c["id"] != "schwab")
        assert by_id["coinbase"]["added"] is True and by_id["alpaca"]["added"] is False
        assert by_id["finnhub"]["uses"] == ["equity", "bars", "crypto", "forex", "news"]


def test_every_route_needs_the_session_token(live):
    with live.app() as client:
        for method, path in (("get", "/api/connections"), ("put", "/api/connections/alpaca/key"),
                             ("delete", "/api/connections/alpaca/key"), ("put", "/api/connections/alpaca/enabled"),
                             ("put", "/api/connections/priority"), ("post", "/api/connections/alpaca/test"),
                             ("post", "/api/connections/alpaca/add"), ("post", "/api/connections/alpaca/remove"),
                             ("post", "/api/connections/schwab/login/start"),
                             ("post", "/api/connections/schwab/login/open"),
                             ("post", "/api/connections/schwab/login/finish"),
                             ("get", "/api/connections/schwab/account")):
            assert getattr(client, method)(path).status_code == 401, path
            assert getattr(client, method)(path, headers={"X-Hokma-Token": "nope"}).status_code == 401, path
        assert client.get("/api/connections", headers={"host": "evil.example"}).status_code == 403
        assert client.get(f"/api/connections?token={AUTH['X-Hokma-Token']}").status_code == 200


def test_an_unknown_provider_is_a_catalog_404(live):
    with live.app() as client:
        for response in (client.put("/api/connections/nope/key", json={"key": "abcd1234"}, headers=AUTH),
                         client.delete("/api/connections/nope/key", headers=AUTH),
                         client.put("/api/connections/nope/enabled", json={"enabled": True}, headers=AUTH),
                         client.post("/api/connections/nope/test", headers=AUTH),
                         client.post("/api/connections/nope/add", json={}, headers=AUTH),
                         client.post("/api/connections/nope/remove", json={}, headers=AUTH)):
            assert response.status_code == 404 and response.json()["detail"]["code"] == "connections.unknown_provider"


# ------------------------------------------------------------------ keys (C3, C4)


def data_files() -> list:
    root = hokma_data_dir()
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


def read_if_present(path) -> bytes:
    """A SQLite -shm or -wal file can disappear between the listing and the read (the database closes); that is not a leak."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""


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
            body = read_if_present(path)
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
            body = read_if_present(path)
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
                        json={"asset_class": "equity", "order": ["finnhub", "alpaca", "schwab"]})
        assert ok.status_code == 200 and ok.json()["groups"]["equity"][0] == "finnhub"
        for bad in ({"asset_class": "equity", "order": ["finnhub"]},
                    {"asset_class": "equity", "order": ["finnhub", "alpaca"]},
                    {"asset_class": "equity", "order": ["finnhub", "finnhub", "alpaca", "schwab"]},
                    {"asset_class": "equity", "order": ["alpaca", "finnhub", "siomtrading_futures"]},
                    {"asset_class": "nope", "order": []}, {"order": []}):
            response = client.put("/api/connections/priority", headers=AUTH, json=bad)
            assert response.status_code == 422 and response.json()["detail"]["code"] == "connections.bad_priority"
        saved = json.loads((hokma_data_dir() / "connections.json").read_text())
        assert saved["enabled"]["coinbase"] is False and saved["priority"]["equity"][0] == "finnhub"
    with live.app() as again:                                           # it survives a new engine run
        assert cards(again)["coinbase"]["enabled"] is False
        assert again.get("/api/connections", headers=AUTH).json()["groups"]["equity"][0] == "finnhub"


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
    # Saving the key already opens the stream in the background. The server holds its refusal until the test call is
    # running (an event, not a delay), so the order is the same on a slow machine and on a fast one: the call starts,
    # then the provider refuses the handshake.
    refuse = threading.Event()
    finnhub = FakeWs(finnhub_script, reject_token="the-key-it-accepts", hold=refuse).start()
    try:
        live.use({"finnhub": finnhub.url})
        with live.app() as client:
            save_finnhub(client)
            probes = connections_runtime.get_hub()._runtimes["finnhub"].probes
            releaser = threading.Thread(target=lambda: (wait_until(lambda: probes, timeout=30), refuse.set()), daemon=True)
            releaser.start()
            bad = client.post("/api/connections/finnhub/test", headers=AUTH).json()
            assert bad["ok"] is False and bad["error_code"] == "connections.auth_failed"
            assert FINNHUB_KEY not in json.dumps(bad)
    finally:
        refuse.set()                                                     # never leave a handshake waiting on a failed run
        finnhub.stop()


def test_c2_a_key_the_provider_already_refused_still_tests_as_a_key_error_not_a_timeout(live):
    # The refusal reaches the hub BEFORE the user presses Test (saving the key opens the stream in the background). The
    # dead source must not stay: the test opens a new one and reports auth_failed, never connections.timeout.
    finnhub = FakeWs(finnhub_script, reject_token="the-key-it-accepts").start()
    try:
        live.use({"finnhub": finnhub.url})
        with live.app() as client:
            save_finnhub(client)
            runtime = connections_runtime.get_hub()._runtimes["finnhub"]
            wait_until(lambda: runtime.state == "error", timeout=30)
            assert runtime.source is not None                          # the situation of the bug: refused and still there
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


# ------------------------------------------------------------------ SPEC-015: add, remove, uses, Schwab sign-in (5.12)

import sys
import types

from dbsupport import db, env  # noqa: F401  (fixtures)
from desks_support import desks_db, make_desk  # noqa: F401  (fixture and helper)
from hokma.connections import runtime as connections_runtime
from hokma.desks import service as desks_service
from hokma.schwab.http import SchwabError


class FakeAuth(types.ModuleType):
    """Stands in for `hokma.schwab.auth` (written by another module): the calls the Connections routes make."""

    def __init__(self) -> None:
        super().__init__("hokma.schwab.auth")
        self.connected = False
        self.calls: list[tuple] = []
        self.test_result = {"ok": True, "state": "connected", "data_state": "live", "latency_ms": 12,
                            "symbol": "SPY", "first_data": True, "error_code": None, "message": ""}
        self.raises: SchwabError | None = None
        self.accounts = [{"account_ref": "A1", "mask": "\u2022\u2022\u2022\u20221234"}]

    def _maybe_raise(self) -> None:
        if self.raises is not None:
            raise self.raises

    def is_connected(self) -> bool:
        return self.connected

    def session_card(self) -> dict:
        return {"state": "signed_in" if self.connected else "signed_out", "session_expires_at": None,
                "accounts": self.accounts if self.connected else []}

    def run_test(self) -> dict:
        self.calls.append(("run_test",))
        return self.test_result

    def start_login(self) -> dict:
        self.calls.append(("start_login",))
        self._maybe_raise()
        return {"auth_url": "https://auth.example.invalid/authorize?client_id=fake-client-id"}

    def open_login_in_browser(self, url: str) -> bool:
        self.calls.append(("open", url))
        return True

    def finish_login(self, redirect_url: str) -> dict:
        self.calls.append(("finish", redirect_url))
        self._maybe_raise()
        self.connected = True
        return {"test": self.test_result}

    def sign_out(self) -> None:
        self.calls.append(("sign_out",))
        self.connected = False

    def maintain(self) -> None:
        self.calls.append(("maintain",))


@pytest.fixture
def fake_auth(monkeypatch):
    import hokma.schwab as schwab_package

    module = FakeAuth()
    monkeypatch.setitem(sys.modules, "hokma.schwab.auth", module)
    monkeypatch.setattr(schwab_package, "auth", module, raising=False)
    return module


def post(client, path, body=None):
    return client.post(f"/api/connections/{path}", json=body or {}, headers=AUTH)


def saved_file() -> dict:
    return json.loads((hokma_data_dir() / "connections.json").read_text())


GOOD_ALPACA = {"key_id": KEY_ID, "secret": SECRET}
SCHWAB_KEYS = {"client_id": "fake-client-id-0001", "client_secret": "fake-client-secret-0001"}


def test_add_with_a_good_key_saves_it_switches_on_and_tests(live):
    alpaca = FakeWs(alpaca_script(KEY_ID, SECRET)).start()
    try:
        live.use({"alpaca": alpaca.url})
        with live.app() as client:
            response = post(client, "alpaca/add", GOOD_ALPACA)
            body = response.json()
            assert response.status_code == 200 and body["added"] is True
            assert body["test"]["ok"] is True and body["test"]["symbol"] == "SPY"
            assert body["card"]["id"] == "alpaca" and body["card"]["added"] is True
            assert body["card"]["key_state"] == "saved" and body["card"]["enabled"] is True
            assert json.loads(live.keyring.items[("hokma-market", "alpaca")]) == GOOD_ALPACA
            assert saved_file()["enabled"]["alpaca"] is True
            assert KEY_ID not in response.text and SECRET not in response.text
            assert saved_file()["priority"] == {}                       # a key provider keeps its place in every order
    finally:
        alpaca.stop()


def test_add_with_a_failing_test_drops_the_new_key_and_leaves_the_switch_as_it_was(live):
    alpaca = FakeWs(alpaca_script(KEY_ID, SECRET)).start()
    try:
        live.use({"alpaca": alpaca.url})
        with live.app() as client:
            response = post(client, "alpaca/add", {"key_id": KEY_ID, "secret": "the-wrong-secret-x"})
            body = response.json()
            assert response.status_code == 200 and body["added"] is False
            assert body["test"]["ok"] is False and body["test"]["error_code"] == "connections.auth_failed"
            assert ("hokma-market", "alpaca") not in live.keyring.items
            assert body["card"]["key_state"] == "missing" and body["card"]["added"] is False
            assert "alpaca" not in saved_file()["enabled"]              # it was never mentioned: it still is not
            assert "the-wrong-secret-x" not in response.text
            client.put("/api/connections/alpaca/enabled", json={"enabled": False}, headers=AUTH)
            again = post(client, "alpaca/add", {"key_id": KEY_ID, "secret": "the-wrong-secret-x"}).json()
            assert again["added"] is False and again["card"]["enabled"] is False   # a switch that was off stays off
    finally:
        alpaca.stop()


def test_a_replacement_key_that_fails_keeps_the_previous_one(live):
    alpaca = FakeWs(alpaca_script(KEY_ID, SECRET)).start()
    try:
        live.use({"alpaca": alpaca.url})
        with live.app() as client:
            assert post(client, "alpaca/add", GOOD_ALPACA).json()["added"] is True
            bad = post(client, "alpaca/add", {"key_id": KEY_ID, "secret": "the-wrong-secret-x"}).json()
            assert bad["added"] is False and bad["test"]["error_code"] == "connections.auth_failed"
            assert json.loads(live.keyring.items[("hokma-market", "alpaca")]) == GOOD_ALPACA
            card = cards(client)["alpaca"]
            assert card["key_state"] == "saved" and card["added"] is True and card["enabled"] is True
            assert client.post("/api/connections/alpaca/test", headers=AUTH).json()["ok"] is True   # old key works
    finally:
        alpaca.stop()


def test_add_refuses_a_malformed_key_without_changing_anything(live):
    with live.app() as client:
        for body in ({"key_id": KEY_ID}, {"key_id": "has space", "secret": SECRET}, {}):
            response = post(client, "alpaca/add", body)
            assert response.status_code == 422 and response.json()["detail"]["code"] == "connections.bad_key"
        assert ("hokma-market", "alpaca") not in live.keyring.items
        assert not (hokma_data_dir() / "connections.json").exists() or "alpaca" not in saved_file()["enabled"]


def test_add_coinbase_only_switches_on_and_tests_and_a_failure_restores_the_switch(live):
    coinbase = FakeWs(coinbase_script).start()
    try:
        live.use({"coinbase": coinbase.url})
        with live.app() as client:
            client.put("/api/connections/coinbase/enabled", json={"enabled": False}, headers=AUTH)
            assert cards(client)["coinbase"]["added"] is False
            body = post(client, "coinbase/add").json()
            assert body["added"] is True and body["card"]["enabled"] is True and body["card"]["added"] is True
            assert saved_file()["enabled"]["coinbase"] is True
            assert not any(k[1] == "coinbase" for k in live.keyring.items)
    finally:
        coinbase.stop()
    live.use({})                                                         # every host refused
    with live.app() as client:
        client.put("/api/connections/coinbase/enabled", json={"enabled": False}, headers=AUTH)
        body = post(client, "coinbase/add").json()
        assert body["added"] is False and body["test"]["ok"] is False
        assert body["card"]["enabled"] is False and saved_file()["enabled"]["coinbase"] is False


def test_remove_by_type(live, fake_auth):
    alpaca = FakeWs(alpaca_script(KEY_ID, SECRET)).start()
    try:
        live.use({"alpaca": alpaca.url})
        with live.app() as client:
            assert post(client, "alpaca/add", GOOD_ALPACA).json()["added"] is True
            card = post(client, "alpaca/remove").json()                  # a key provider: key gone, switched off
            assert card["id"] == "alpaca" and card["added"] is False and card["key_state"] == "missing"
            assert card["enabled"] is False and ("hokma-market", "alpaca") not in live.keyring.items
            card = post(client, "coinbase/remove").json()                # no key: only switched off
            assert card["added"] is False and card["enabled"] is False and card["key_state"] == "not_required"
            live.keyring.items[("hokma-market", "schwab")] = json.dumps(SCHWAB_KEYS)      # Schwab: keys and session
            live.keyring.items[("hokma-market", "schwab.session")] = "{}"
            live.keyring.items[("hokma-market", "finnhub")] = FINNHUB_KEY
            client.get("/api/connections", headers=AUTH)
            card = post(client, "schwab/remove").json()
            assert card["id"] == "schwab" and card["key_state"] == "missing" and card["added"] is False
            assert ("hokma-market", "schwab") not in live.keyring.items
            assert ("hokma-market", "schwab.session") not in live.keyring.items
            assert ("sign_out",) in fake_auth.calls
            assert live.keyring.items[("hokma-market", "finnhub")] == FINNHUB_KEY      # nobody else is touched
            assert post(client, "nope/remove").status_code == 404
    finally:
        alpaca.stop()


def test_remove_schwab_works_while_the_sign_in_module_is_missing(live, monkeypatch):
    monkeypatch.setitem(sys.modules, "hokma.schwab.auth", None)         # an import of it raises ImportError
    live.keyring.items[("hokma-market", "schwab")] = json.dumps(SCHWAB_KEYS)
    live.keyring.items[("hokma-market", "schwab.session")] = "{}"
    with live.app() as client:
        client.get("/api/connections", headers=AUTH)
        assert post(client, "schwab/remove").status_code == 200
        assert not any(k[1].startswith("schwab") for k in live.keyring.items)


def test_priority_accepts_bars_options_and_news_and_refuses_an_unknown_use(live):
    with live.app() as client:
        for use, order in (("bars", ["finnhub", "schwab", "alpaca"]), ("options", ["alpaca", "schwab"]),
                           ("news", ["alpaca", "finnhub"]), ("future", ["siomtrading_futures"])):
            response = client.put("/api/connections/priority", json={"asset_class": use, "order": order}, headers=AUTH)
            assert response.status_code == 200, use
            assert response.json()["uses"][use] == order
            assert response.json()["groups"].get(use) in (None, order)
        assert "bars" not in client.get("/api/connections", headers=AUTH).json()["groups"]
        for bad in ({"asset_class": "streaming", "order": ["alpaca"]},
                    {"asset_class": "options", "order": ["alpaca"]},               # must list every provider of the use
                    {"asset_class": "news", "order": ["finnhub", "alpaca", "schwab"]},
                    {"asset_class": "bars", "order": ["schwab", "schwab", "alpaca", "finnhub"]}):
            response = client.put("/api/connections/priority", json=bad, headers=AUTH)
            assert response.status_code == 422 and response.json()["detail"]["code"] == "connections.bad_priority"
        saved = saved_file()["priority"]
        assert saved["bars"] == ["finnhub", "schwab", "alpaca"] and saved["news"] == ["alpaca", "finnhub"]
    with live.app() as again:
        uses = again.get("/api/connections", headers=AUTH).json()["uses"]
        assert uses["bars"] == ["finnhub", "schwab", "alpaca"] and uses["options"] == ["alpaca", "schwab"]


def test_schwab_goes_first_in_equity_bars_and_options_only_when_it_is_added(live, fake_auth):
    with live.app() as client:
        for use, order in (("equity", ["finnhub", "alpaca", "schwab"]), ("bars", ["alpaca", "finnhub", "schwab"]),
                           ("options", ["alpaca", "schwab"]), ("news", ["alpaca", "finnhub"])):
            client.put("/api/connections/priority", json={"asset_class": use, "order": order}, headers=AUTH)
        client.put("/api/connections/schwab/key", json=SCHWAB_KEYS, headers=AUTH)
        assert client.put("/api/connections/priority", json={"asset_class": "crypto", "order": ["finnhub", "coinbase"]},
                          headers=AUTH).status_code == 200
        body = post(client, "schwab/add").json()                        # keys saved earlier, the test passes
        assert body["added"] is True
        uses = client.get("/api/connections", headers=AUTH).json()["uses"]
        assert uses["equity"] == ["schwab", "finnhub", "alpaca"] and uses["bars"] == ["schwab", "alpaca", "finnhub"]
        assert uses["options"] == ["schwab", "alpaca"]
        assert uses["news"] == ["alpaca", "finnhub"] and uses["crypto"] == ["finnhub", "coinbase"]   # untouched
        assert saved_file()["enabled"]["schwab"] is True
        client.put("/api/connections/priority", json={"asset_class": "equity", "order": ["finnhub", "alpaca", "schwab"]},
                   headers=AUTH)
        assert post(client, "schwab/add").json()["added"] is True       # a second add moves nothing
        assert client.get("/api/connections", headers=AUTH).json()["uses"]["equity"] == ["finnhub", "alpaca", "schwab"]


def test_schwab_add_without_a_sign_in_keeps_the_keys_and_says_so(live, fake_auth):
    fake_auth.test_result = {"ok": False, "state": "not_connected", "data_state": "live", "latency_ms": None,
                             "symbol": "SPY", "first_data": False, "error_code": "connections.no_login",
                             "message": "x"}
    with live.app() as client:
        body = post(client, "schwab/add", SCHWAB_KEYS).json()
        assert body["added"] is False and body["test"]["error_code"] == "connections.no_login"
        assert body["card"]["key_state"] == "saved"                     # the sign-in is the next step
        assert json.loads(live.keyring.items[("hokma-market", "schwab")]) == SCHWAB_KEYS
        assert "schwab" not in saved_file()["enabled"]
        assert SCHWAB_KEYS["client_secret"] not in json.dumps(body)


def test_schwab_card_carries_the_session_and_the_masked_accounts(live, fake_auth):
    with live.app() as client:
        assert cards(client)["schwab"]["session"] == {"state": "signed_out", "session_expires_at": None, "accounts": []}
        fake_auth.connected = True
        card = cards(client)["schwab"]
        assert card["added"] is True and card["session"]["state"] == "signed_in"
        assert card["session"]["accounts"] == fake_auth.accounts


def test_the_schwab_test_is_the_sign_in_module_test(live, fake_auth):
    with live.app() as client:
        assert client.post("/api/connections/schwab/test", headers=AUTH).json() == fake_auth.test_result
        fake_auth.test_result = {**fake_auth.test_result, "ok": False, "error_code": "connections.schwab_expired"}
        assert client.post("/api/connections/schwab/test", headers=AUTH).json()["error_code"] == \
            "connections.schwab_expired"


def test_schwab_login_routes(live, fake_auth):
    with live.app() as client:
        refused = post(client, "schwab/login/start")                      # no Client ID yet
        assert refused.status_code == 409 and refused.json()["detail"]["code"] == "connections.no_key"
        client.put("/api/connections/schwab/key", json=SCHWAB_KEYS, headers=AUTH)
        started = post(client, "schwab/login/start")
        assert started.status_code == 200 and started.json()["auth_url"].startswith("https://auth.example.invalid/")
        assert post(client, "schwab/login/open", {"url": "https://auth.example.invalid/x"}).json() == {"opened": True}
        for bad in ({}, {"url": 5}, {"url": ""}, {"url": "h" * 2000}):
            response = post(client, "schwab/login/open", bad)
            assert response.status_code in (413, 422) and response.json()["detail"]["code"] in (
                "connections.schwab_url", "connections.bad_request")
        for body in ({}, {"redirect_url": ""}, {"redirect_url": 7}):
            response = post(client, "schwab/login/finish", body)
            assert response.status_code == 422 and response.json()["detail"]["code"] == "connections.schwab_url"
        done = post(client, "schwab/login/finish", {"redirect_url": "https://127.0.0.1:8182/?code=abc&state=xyz"})
        assert done.status_code == 200 and done.json()["test"] == fake_auth.test_result
        assert done.json()["card"]["id"] == "schwab" and done.json()["card"]["session"]["state"] == "signed_in"
        assert saved_file()["enabled"]["schwab"] is True                  # switched on by a good sign-in


def test_schwab_errors_travel_with_their_catalog_code_and_status(live, fake_auth):
    from hokma.schwab import http

    cases = (("connections.schwab_url", 422), ("connections.schwab_state", 409), ("connections.schwab_code", 400),
             ("connections.schwab_expired", 401), ("connections.schwab_denied", 403),
             ("connections.no_login", 409), (http.NOT_CONNECTED, 409))
    with live.app() as client:
        client.put("/api/connections/schwab/key", json=SCHWAB_KEYS, headers=AUTH)
        for code, status in cases:
            fake_auth.raises = SchwabError(code, status)
            response = post(client, "schwab/login/finish", {"redirect_url": "https://127.0.0.1:8182/?code=a&state=b"})
            detail = response.json()["detail"]
            assert response.status_code == status, code
            assert detail["code"] == ("connections.no_login" if code == http.NOT_CONNECTED else code)
            assert detail["message"]
        fake_auth.raises = SchwabError("connections.schwab_state", 409)
        assert post(client, "schwab/login/start").json()["detail"]["code"] == "connections.schwab_state"


def test_schwab_login_finish_is_limited_to_ten_a_minute(live, fake_auth):
    fake_auth.raises = SchwabError("connections.schwab_code", 400)
    with live.app() as client:
        codes = [post(client, "schwab/login/finish", {"redirect_url": "https://127.0.0.1:8182/?code=a"}).status_code
                 for _ in range(11)]
        assert codes[:10] == [400] * 10 and codes[10] == 429


def test_schwab_account_route_returns_the_panel_data(live, monkeypatch):
    import hokma.schwab as schwab_package

    panel = types.ModuleType("hokma.schwab.accounts")
    panel.panel_data = lambda: {"accounts": [{"account_ref": "A1", "mask": "\u2022\u2022\u2022\u20221234"}]}
    monkeypatch.setitem(sys.modules, "hokma.schwab.accounts", panel)
    monkeypatch.setattr(schwab_package, "accounts", panel, raising=False)
    with live.app() as client:
        assert client.get("/api/connections/schwab/account", headers=AUTH).json() == {**panel.panel_data(), "orders_enabled": False}
        def refuse():
            raise SchwabError("connections.schwab_expired", 401)
        panel.panel_data = refuse
        response = client.get("/api/connections/schwab/account", headers=AUTH)
        assert response.status_code == 401 and response.json()["detail"]["code"] == "connections.schwab_expired"


def test_adding_a_provider_turns_it_on_in_every_desk_without_a_row_and_is_idempotent(desks_db, live):
    alpaca = FakeWs(alpaca_script(KEY_ID, SECRET)).start()
    try:
        live.use({"alpaca": alpaca.url})
        first, second = make_desk(desks_db, name="Futuros NQ"), make_desk(desks_db, name="Acciones")
        with live.app() as client:
            client.get("/api/connections", headers=AUTH)
            desks_service.set_connection(second["id"], "alpaca", False, desks_db)     # the owner chose: not here
            assert post(client, "alpaca/add", GOOD_ALPACA).json()["added"] is True
            chosen = {d: {c["provider_id"]: c["enabled"] for c in desks_service.list_connections(d, desks_db)}
                      for d in (first["id"], second["id"])}
            assert chosen[first["id"]]["alpaca"] is True and chosen[second["id"]]["alpaca"] is False
            with desks_db.read() as u:
                rows = u.session.scalar("SELECT count(*) FROM desk_connections WHERE provider_id = 'alpaca'")
            assert post(client, "alpaca/add", GOOD_ALPACA).json()["added"] is True
            assert connections_runtime.get_service().mark_added("alpaca") == []
            with desks_db.read() as u:
                assert u.session.scalar("SELECT count(*) FROM desk_connections WHERE provider_id = 'alpaca'") == rows == 2
            third = make_desk(desks_db, name="Opciones")                              # a desk made later has no row
            assert not any(c["enabled"] for c in desks_service.list_connections(third["id"], desks_db))
    finally:
        alpaca.stop()


def test_the_hub_maintains_the_schwab_session_every_minute_only_with_a_session(live, fake_auth, monkeypatch):
    monkeypatch.setattr(hub_module, "MAINTAIN_SECONDS", 0.05)
    with live.app() as client:
        client.get("/api/connections", headers=AUTH)
        time.sleep(0.4)
        assert ("maintain",) not in fake_auth.calls                      # no session: nothing to renew
        fake_auth.connected = True
        assert wait_until(lambda: ("maintain",) in fake_auth.calls)
    calls = len(fake_auth.calls)
    time.sleep(0.3)
    assert len(fake_auth.calls) == calls                                   # the task ended with the hub


def test_a_failing_maintenance_round_does_not_stop_the_next_one(live, fake_auth, monkeypatch):
    monkeypatch.setattr(hub_module, "MAINTAIN_SECONDS", 0.05)
    fake_auth.connected = True
    rounds = []

    def flaky():
        rounds.append(1)
        if len(rounds) == 1:
            raise RuntimeError("boom")
    fake_auth.maintain = flaky
    with live.app() as client:
        client.get("/api/connections", headers=AUTH)
        assert wait_until(lambda: len(rounds) >= 2)

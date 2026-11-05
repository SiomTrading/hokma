"""B1/B2/C: Host and Origin validation on HTTP and every WebSocket, no token in the access log, non-ASCII tokens."""
import socket
import threading
import time
import urllib.request

import pytest
import uvicorn
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from dbsupport import db, env  # noqa: F401  (fixtures)
from hokma.core.app import create_app
from hokma.core.origin import hosts_for_port, token_matches
from hokma.rpc.ws import SESSION_TOKEN

EVIL = {"origin": "http://evil.example"}
WS_URLS = [f"/ws/terminal?token={SESSION_TOKEN}", f"/ws/chat?token={SESSION_TOKEN}", f"/rpc?token={SESSION_TOKEN}"]


@pytest.fixture
def app(db):  # noqa: F811
    return create_app()


def test_foreign_origin_is_403_on_http(app):
    c = TestClient(app)
    assert c.get("/health").status_code == 200
    assert c.get("/health", headers=EVIL).status_code == 403
    assert c.get("/api/status", headers=EVIL).status_code == 403
    assert c.get("/", headers=EVIL).status_code == 403


def test_same_origin_and_absent_origin_work(app):
    c = TestClient(app)
    assert c.get("/health", headers={"origin": "http://testserver"}).status_code == 200
    assert c.get("/health").status_code == 200


def test_foreign_host_is_403(app):
    c = TestClient(app)
    assert c.get("/health", headers={"host": "evil.example"}).status_code == 403
    assert c.get("/health", headers={"host": "evil.example:8765"}).status_code == 403


def test_production_hosts_are_loopback_with_the_real_port(db):  # noqa: F811
    c = TestClient(create_app(8765), base_url="http://127.0.0.1:8765")
    assert c.get("/health").status_code == 200
    assert c.get("/health", headers={"origin": "http://127.0.0.1:8765"}).status_code == 200
    assert c.get("/health", headers={"host": "localhost:8765", "origin": "http://localhost:8765"}).status_code == 200
    assert c.get("/health", headers={"host": "127.0.0.1:9999"}).status_code == 403
    assert c.get("/health", headers={"origin": "http://127.0.0.1:9999"}).status_code == 403
    assert c.get("/health", headers={"origin": "https://127.0.0.1:8765"}).status_code == 403
    assert TestClient(create_app(8765)).get("/health").status_code == 403    # testserver host is not accepted


@pytest.mark.parametrize("url", WS_URLS)
def test_websocket_foreign_origin_is_closed_before_accept(app, url):
    c = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect(url, headers=EVIL):
            pass
    assert exc.value.code == 1008


@pytest.mark.parametrize("url", WS_URLS)
def test_websocket_foreign_host_is_closed(app, url):
    with pytest.raises(WebSocketDisconnect) as exc:
        with TestClient(app).websocket_connect(url, headers={"host": "evil.example"}):
            pass
    assert exc.value.code == 1008


def test_websocket_same_origin_and_absent_origin_work(app):
    c = TestClient(app)
    for headers in ({}, {"origin": "http://testserver"}):
        with c.websocket_connect(f"/rpc?token={SESSION_TOKEN}", headers=headers) as ws:
            ws.send_text('{"id": 1}')
            assert ws.receive_json()["id"] == 1


def test_allowed_hosts_are_adjustable_by_tests(app):
    app.state.allowed_hosts = hosts_for_port(1234)
    assert TestClient(app).get("/health").status_code == 403
    assert TestClient(app, base_url="http://localhost:1234").get("/health").status_code == 200


@pytest.mark.parametrize("path", ["/api/status", "/api/database", "/api/projects", "/api/agents"])
def test_non_ascii_token_is_401_not_500(app, path):
    r = TestClient(app).get(path, params={"token": "x" + chr(233) + chr(8364)})
    assert r.status_code == 401
    raw = TestClient(app).get(path, headers=[(b"x-hokma-token", "x\u00e9".encode("latin-1"))])
    assert raw.status_code == 401


@pytest.mark.parametrize("path", ["/rpc", "/ws/chat", "/ws/terminal"])
def test_non_ascii_token_closes_the_websocket(app, path):
    with pytest.raises(WebSocketDisconnect) as exc:
        with TestClient(app).websocket_connect(f"{path}?token=x%C3%A9%E2%82%AC"):
            pass
    assert exc.value.code == 4401


def test_token_matches_unit():
    assert token_matches(SESSION_TOKEN, SESSION_TOKEN)
    assert not token_matches("caf" + chr(233), SESSION_TOKEN)
    assert not token_matches(None, SESSION_TOKEN)
    assert not token_matches("", SESSION_TOKEN)


def test_access_log_does_not_record_the_token(app, capfd):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    from hokma.core import server
    t = threading.Thread(target=server.run, args=("127.0.0.1", port), daemon=True)
    started = {}
    original = uvicorn.Server.run

    def keep(self, *a, **k):
        started["s"] = self
        return original(self, *a, **k)
    uvicorn.Server.run = keep
    try:
        t.start()
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health?token=SECRETVALUE", timeout=1).read()
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("server did not start")
    finally:
        uvicorn.Server.run = original
        started["s"].should_exit = True
        t.join(10)
    out = capfd.readouterr()
    assert "SECRETVALUE" not in out.out + out.err

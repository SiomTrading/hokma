import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hokma.core.app import create_app
from hokma.rpc.ws import SESSION_TOKEN


def test_health():
    r = TestClient(create_app()).get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_rpc_echo():
    with TestClient(create_app()).websocket_connect(f"/rpc?token={SESSION_TOKEN}") as ws:
        ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert ws.receive_json()["result"]["echo"]["method"] == "ping"


def test_rpc_rejects_bad_token():
    with pytest.raises(WebSocketDisconnect):
        with TestClient(create_app()).websocket_connect("/rpc?token=bad"):
            pass

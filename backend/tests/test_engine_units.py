"""Pure pieces of the engine and the wiring into the real app."""
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
import pytest

from hokma.core.app import create_app
from hokma.engine import stream
from hokma.engine.engine import ChatEngine, parse_command
from hokma.rpc.ws import SESSION_TOKEN


def test_parse_command_accepts_both_languages_and_ignores_the_rest():
    assert parse_command("/modelo opus") == ("model", "opus")
    assert parse_command("/model") == ("model", None)
    assert parse_command("  /agente  analyst  ") == ("agent", "analyst")
    assert parse_command("/proyecto Mi Proyecto") == ("project", "Mi Proyecto")
    assert parse_command("/compact") is None and parse_command("hello /modelo") is None


def test_summarize_tool_kinds():
    assert stream.summarize_tool("Write", {"file_path": "/a/b.txt", "content": "x"}) == \
        {"kind": "write_file", "target": "/a/b.txt"}
    assert stream.summarize_tool("Bash", {"command": "ls " + "x" * 600})["target"] == "ls " + "x" * 497
    assert stream.summarize_tool("Mystery", None) == {"kind": "other", "target": ""}


def test_usage_and_permission_reply_shapes():
    result = {"total_cost_usd": 0.5, "usage": {"input_tokens": 2, "cache_creation_input_tokens": 10,
                                                "cache_read_input_tokens": 7, "output_tokens": 4}}
    assert stream.usage_of(result) == {"tokens_in": 12, "tokens_out": 4, "cache_read": 7, "cost_usd": 0.5}
    allow = stream.permission_reply("r1", True, {"a": 1}, "")["response"]
    assert allow == {"subtype": "success", "request_id": "r1", "response": {"behavior": "allow", "updatedInput": {"a": 1}}}
    deny = stream.permission_reply("r1", False, {}, "no")["response"]["response"]
    assert deny == {"behavior": "deny", "message": "no"}
    assert stream.parse_line("garbage") is None and stream.parse_line('{"type": "x"}') == {"type": "x"}


def test_title_is_the_first_words():
    assert ChatEngine.make_title("hello world") == "hello world"
    assert ChatEngine.make_title("a b c d e f g h i j") == "a b c d e f g h..."


def test_real_app_serves_the_chat_socket_with_the_session_token():
    client = TestClient(create_app())
    with client.websocket_connect(f"/ws/chat?token={SESSION_TOKEN}") as ws:
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/chat"):
            pass
    assert client.post("/api/attachments", content=b"x").status_code == 401

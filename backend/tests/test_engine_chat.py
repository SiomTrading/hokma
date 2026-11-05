"""Engine over the real WebSocket route, against the fake `claude` (stream-json recorded from a real run)."""
import os

import pytest
from starlette.websockets import WebSocketDisconnect

from engine_support import connect, env, hard_timeout, make_client, posix_only, text_of, turn, until  # noqa: F401
from hokma.rpc.ws import SESSION_TOKEN

pytestmark = posix_only


def flag_value(argv, name):
    return argv[argv.index(name) + 1]


def test_turn_streams_in_order_and_is_saved(env):
    engine = env.engine()
    with make_client(engine) as client, connect(client) as ws:
        events = turn(ws, "Reply with exactly: pong please and thank you very much indeed")
    kinds = [e["type"] for e in events]
    assert kinds[:2] == ["conversation", "turn_start"] and kinds[-1] == "done"
    assert "delta" in kinds and kinds.index("delta") < kinds.index("done")
    assert text_of(events) == "pong"
    done = events[-1]
    assert done["stopped"] is False and done["usage"]["tokens_out"] > 0 and done["usage"]["tokens_in"] > 0
    conv_id = events[0]["conversation_id"]
    rows = env.store.list_messages(conv_id)
    assert [(m.role, m.text, m.status) for m in rows] == [
        ("user", "Reply with exactly: pong please and thank you very much indeed", "complete"),
        ("assistant", "pong", "complete")]
    assert rows[1].tokens_out == done["usage"]["tokens_out"] and rows[1].tokens_in == done["usage"]["tokens_in"]
    assert env.store.get_conversation(conv_id).title == "Reply with exactly: pong please and thank you..."
    assert env.store.get_conversation(conv_id).engine_session == done["engine_session"]


def test_launch_flags_and_working_folder(env):
    with make_client(env.engine()) as client, connect(client) as ws:
        turn(ws, "hello")
    start = env.entries("start")[0]
    argv = start["argv"]
    for flag in ("-p", "--include-partial-messages", "--verbose"):
        assert flag in argv
    assert flag_value(argv, "--output-format") == "stream-json" and flag_value(argv, "--input-format") == "stream-json"
    assert flag_value(argv, "--model") == "sonnet" and flag_value(argv, "--permission-prompt-tool") == "stdio"
    assert "--session-id" in argv and "--resume" not in argv
    assert os.path.realpath(start["cwd"]) == os.path.realpath(env.home)
    assert start["env"]["CLAUDE_CONFIG_DIR"] == str(env.data / "cfg")


def test_second_turn_reuses_process_and_a_new_engine_resumes(env):
    engine = env.engine()
    with make_client(engine) as client, connect(client) as ws:
        first = turn(ws, "one")
        conv_id = first[0]["conversation_id"]
        turn(ws, "two", conversation_id=conv_id)
    assert len(env.entries("start")) == 1 and len(env.entries("user")) == 2
    session = first[-1]["engine_session"]
    # A fresh engine (Hokma restarted) resumes the stored CLI session.
    with make_client(env.engine()) as client, connect(client) as ws:
        turn(ws, "three", conversation_id=conv_id)
    last = env.entries("start")[-1]["argv"]
    assert flag_value(last, "--resume") == session and "--session-id" not in last


def test_stop_interrupts_and_keeps_partial_text(env):
    with make_client(env.engine()) as client, connect(client) as ws:
        ws.send_json({"type": "send", "text": "[slow] count"})
        events = until(ws, "delta")
        conv_id = events[0]["conversation_id"]
        ws.send_json({"type": "stop", "conversation_id": conv_id})
        rest = until(ws, "done", "error")
        done = rest[-1]
        assert done["type"] == "done" and done["stopped"] is True and done["message_id"]
        rows = env.store.list_messages(conv_id)
        assert rows[-1].status == "stopped" and rows[-1].text.startswith("1")
        assert env.entries("interrupt")
        # the conversation keeps working: the next turn resumes the session in a new process
        again = turn(ws, "after", conversation_id=conv_id)
        assert again[-1]["type"] == "done" and again[-1]["stopped"] is False
    assert len(env.entries("start")) == 2 and "--resume" in env.entries("start")[1]["argv"]


def test_unresponsive_process_is_killed_after_the_grace_period(env):
    with make_client(env.engine(stop_grace=0.3)) as client, connect(client) as ws:
        ws.send_json({"type": "send", "text": "[hang] go"})
        conv_id = until(ws, "delta")[0]["conversation_id"]
        ws.send_json({"type": "stop", "conversation_id": conv_id})
        done = until(ws, "done", "error")[-1]
    assert done["type"] == "done" and done["stopped"] is True
    pid = env.entries("start")[0]["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_busy_while_a_turn_runs(env):
    with make_client(env.engine()) as client, connect(client) as ws:
        ws.send_json({"type": "send", "text": "[slow] go"})
        conv_id = until(ws, "delta")[0]["conversation_id"]
        ws.send_json({"type": "send", "text": "again", "conversation_id": conv_id})
        assert until(ws, "error")[-1]["code"] == "busy"
        ws.send_json({"type": "stop", "conversation_id": conv_id})
        until(ws, "done")


def test_permission_allow_reaches_the_cli_and_is_recorded_as_human(env):
    with make_client(env.engine()) as client, connect(client) as ws:
        ws.send_json({"type": "send", "text": "[perm] write a file"})
        events = until(ws, "permission_request")
        request = events[-1]
        assert request["tool"] == "Write" and request["summary"]["kind"] == "write_file"
        assert request["summary"]["target"].endswith("out.txt") and request["expires_at"] > 0
        ws.send_json({"type": "permission_response", "conversation_id": request["conversation_id"],
                      "request_id": request["request_id"], "decision": "allow"})
        rest = until(ws, "done", "error")
    kinds = [e["type"] for e in rest]
    assert kinds[0] == "permission_resolved" and rest[0]["decision"] == "allow" and rest[0]["by"] == "human"
    assert "tool_result" in kinds and kinds[-1] == "done"
    sent = env.entries("control_response")[0]["response"]["response"]
    assert sent["behavior"] == "allow" and sent["updatedInput"]["content"] == "x"
    assert env.store.permissions[0]["decision"] == "allowed_by_human"
    assert (env.store.permissions[0]["decided_by_kind"], env.store.permissions[0]["channel"]) == ("human", "dashboard")


def test_permission_deny_reaches_the_cli(env):
    with make_client(env.engine()) as client, connect(client) as ws:
        ws.send_json({"type": "send", "text": "[perm] write a file"})
        request = until(ws, "permission_request")[-1]
        ws.send_json({"type": "permission_response", "conversation_id": request["conversation_id"],
                      "request_id": request["request_id"], "decision": "deny"})
        rest = until(ws, "done", "error")
        assert rest[-1]["type"] == "done"
        ws.send_json({"type": "permission_response", "conversation_id": request["conversation_id"],
                      "request_id": request["request_id"], "decision": "allow"})
        assert until(ws, "error")[-1]["code"] == "unknown_request"      # one answer per request
    assert env.entries("control_response")[0]["response"]["response"]["behavior"] == "deny"
    assert env.store.permissions[0]["decision"] == "denied_by_human"


def test_permission_without_answer_is_denied_by_timeout(env):
    with make_client(env.engine(permission_timeout=0.4)) as client, connect(client) as ws:
        ws.send_json({"type": "send", "text": "[perm] write a file"})
        until(ws, "permission_request")
        rest = until(ws, "done", "error")
    assert rest[0]["type"] == "permission_resolved" and rest[0]["by"] == "timeout" and rest[0]["decision"] == "deny"
    assert env.entries("control_response")[0]["response"]["response"]["behavior"] == "deny"
    assert env.store.permissions[0]["decision"] == "denied_by_timeout"
    assert env.store.permissions[0]["decided_by_kind"] == "system"


def test_stop_while_a_permission_is_pending_denies_it(env):
    with make_client(env.engine()) as client, connect(client) as ws:
        ws.send_json({"type": "send", "text": "[perm] write a file"})
        request = until(ws, "permission_request")[-1]
        ws.send_json({"type": "stop", "conversation_id": request["conversation_id"]})
        rest = until(ws, "done", "error")
    assert any(e["type"] == "permission_resolved" and e["by"] == "stop" for e in rest)
    assert env.store.permissions[0]["decision"] == "denied_by_stop"


def test_reconnect_gets_history_and_pending_permission(env):
    engine = env.engine()
    with make_client(engine) as client:
        with connect(client) as ws:
            ws.send_json({"type": "send", "text": "[perm] write a file"})
            request = until(ws, "permission_request")[-1]
        with connect(client) as ws2:                                   # the first tab closed; the turn lives on
            ws2.send_json({"type": "open", "conversation_id": request["conversation_id"]})
            got = until(ws2, "state")
            history = next(e for e in got if e["type"] == "history")
            assert [m["role"] for m in history["messages"]] == ["user"]
            state = got[-1]
            assert state["state"] == "running" and state["pending_permissions"][0]["request_id"] == request["request_id"]
            ws2.send_json({"type": "permission_response", "conversation_id": request["conversation_id"],
                           "request_id": request["request_id"], "decision": "allow"})
            assert until(ws2, "done")[-1]["type"] == "done"


def test_session_token_never_reaches_the_cli(env, monkeypatch):
    monkeypatch.setenv("HOKMA_SESSION_TOKEN", SESSION_TOKEN)
    monkeypatch.setenv("SOME_COPY", SESSION_TOKEN)
    with make_client(env.engine()) as client, connect(client) as ws:
        turn(ws, "hello")
    start = env.entries("start")[0]
    assert SESSION_TOKEN not in str(start["argv"]) and SESSION_TOKEN not in str(start["env"])


def test_wrong_token_is_refused(env):
    with make_client(env.engine()) as client:
        with pytest.raises(WebSocketDisconnect) as info:
            with connect(client, "bad"):
                pass
        assert info.value.code == 4401


def test_crash_and_engine_error_report_and_recover(env):
    with make_client(env.engine()) as client, connect(client) as ws:
        events = turn(ws, "[crash] boom")
        conv_id = events[0]["conversation_id"]
        assert events[-1]["type"] == "error" and events[-1]["code"] == "engine_exited"
        assert "simulated crash" in events[-1]["message"]
        failed = turn(ws, "[error] boom", conversation_id=conv_id)
        assert failed[-1]["type"] == "error" and failed[-1]["code"] == "engine_error"
        assert env.store.list_messages(conv_id)[-1].status == "error"
        assert turn(ws, "fine", conversation_id=conv_id)[-1]["type"] == "done"


def test_bad_messages_do_not_close_the_socket(env):
    with make_client(env.engine()) as client, connect(client) as ws:
        ws.send_text("not json")
        assert ws.receive_json()["code"] == "bad_json"
        ws.send_json({"type": "nope"})
        assert ws.receive_json()["code"] == "bad_message"
        ws.send_json({"type": "send", "text": "x", "conversation_id": "missing"})
        assert ws.receive_json()["code"] == "unknown_conversation"
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"


def test_missing_claude_is_reported(env, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    with make_client(env.engine()) as client, connect(client) as ws:
        assert turn(ws, "hello")[-1]["code"] == "claude_not_found"


def test_default_launch_passes_the_isolated_config_and_hokma_settings(env):
    from hokma.engine.engine import ChatEngine
    from hokma.engine.memory import WorkdirProjects
    engine = ChatEngine(store=env.store, projects=WorkdirProjects(env.home), session_token=SESSION_TOKEN)
    with make_client(engine) as client, connect(client) as ws:
        assert turn(ws, "hello")[-1]["type"] == "done"
    start = env.entries("start")[0]
    assert start["env"]["CLAUDE_CONFIG_DIR"] == str(env.data / "claude-config")
    assert "--settings" in start["argv"] and "--mcp-config" in start["argv"]
    assert flag_value(start["argv"], "--setting-sources") == "user"

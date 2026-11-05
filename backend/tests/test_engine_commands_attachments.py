"""Slash commands, model/agent/project changes and attachments, through the real routes."""
import base64
import os

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine_support import connect, env, hard_timeout, make_client, png_bytes, posix_only, turn, until  # noqa: F401
from hokma.engine.api import create_router
from hokma.rpc.ws import SESSION_TOKEN

pytestmark = posix_only
HEADERS = {"X-Hokma-Token": SESSION_TOKEN}


def flag_value(argv, name):
    return argv[argv.index(name) + 1]


def command(ws, text, **fields):
    ws.send_json({"type": "send", "text": text, **fields})
    return until(ws, "command_result", "error")


def test_model_command_menu_and_change_applies_next_turn(env):
    with make_client(env.engine()) as client, connect(client) as ws:
        menu = command(ws, "/modelo")[-1]
        assert menu["options"] == ["haiku", "sonnet", "opus"]
        first = turn(ws, "hello", model="haiku")
        conv_id = first[0]["conversation_id"]
        assert first[0]["model"] == "haiku"
        result = command(ws, "/modelo opus", conversation_id=conv_id)
        assert result[-1]["value"] == "opus" and env.store.get_conversation(conv_id).model == "opus"
        assert command(ws, "/model gpt", conversation_id=conv_id)[-1]["code"] == "unknown_model"
        assert turn(ws, "again", conversation_id=conv_id)[-1]["type"] == "done"
    starts = env.entries("start")
    assert flag_value(starts[0]["argv"], "--model") == "haiku"
    assert flag_value(starts[1]["argv"], "--model") == "opus" and "--resume" in starts[1]["argv"]


def test_command_message_type_matches_the_slash_form(env):
    with make_client(env.engine()) as client, connect(client) as ws:
        ws.send_json({"type": "command", "name": "model", "arg": "opus"})
        events = until(ws, "command_result", "error")
    assert events[0]["type"] == "conversation" and events[0]["created"] is True and events[-1]["value"] == "opus"
    assert env.store.get_conversation(events[0]["conversation_id"]).model == "opus"


def test_agent_command_validates_and_sets_the_flag(env):
    agents = env.home / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "analyst.md").write_text("---\nname: analyst\n---\n")
    with make_client(env.engine()) as client, connect(client) as ws:
        conv_id = turn(ws, "hello")[0]["conversation_id"]
        assert command(ws, "/agente ghost", conversation_id=conv_id)[-1]["code"] == "unknown_agent"
        ok = command(ws, "/agente analyst", conversation_id=conv_id)
        assert ok[-1]["value"] == "analyst" and ok[0]["agent"] == "analyst"
        assert turn(ws, "as analyst", conversation_id=conv_id)[-1]["type"] == "done"
        assert command(ws, "/agente none", conversation_id=conv_id)[-1]["value"] is None
    starts = env.entries("start")
    assert "--agent" not in starts[0]["argv"] and flag_value(starts[1]["argv"], "--agent") == "analyst"


def test_project_command_moves_the_cwd_and_resumes(env):
    (env.home / "alpha").mkdir()
    with make_client(env.engine()) as client, connect(client) as ws:
        conv_id = turn(ws, "hello")[0]["conversation_id"]
        assert command(ws, "/proyecto nope", conversation_id=conv_id)[-1]["code"] == "unknown_project"
        assert command(ws, "/proyecto ../x", conversation_id=conv_id)[-1]["code"] == "unknown_project"
        result = command(ws, "/proyecto alpha", conversation_id=conv_id)
        assert result[-1]["value"]["name"] == "alpha"
        assert turn(ws, "in alpha", conversation_id=conv_id)[-1]["type"] == "done"
    first, second = env.entries("start")
    assert os.path.realpath(second["cwd"]) == os.path.realpath(env.home / "alpha")
    assert flag_value(second["argv"], "--resume") == flag_value(first["argv"], "--session-id")


def test_project_cannot_change_while_a_turn_runs(env):
    (env.home / "alpha").mkdir()
    with make_client(env.engine()) as client, connect(client) as ws:
        ws.send_json({"type": "send", "text": "[slow] go"})
        conv_id = until(ws, "delta")[0]["conversation_id"]
        assert command(ws, "/proyecto alpha", conversation_id=conv_id)[-1]["code"] == "busy"
        ws.send_json({"type": "stop", "conversation_id": conv_id})
        until(ws, "done")


def upload(client, name, data, content_type="application/octet-stream", **kw):
    return client.post("/api/attachments", headers=HEADERS, files={"file": (name, data, content_type)}, **kw)


def test_image_goes_to_the_cli_as_an_image_block(env):
    png = png_bytes()
    with make_client(env.engine()) as client:
        reply = upload(client, "shot.png", png, "image/png")
        assert reply.status_code == 201
        meta = reply.json()
        assert meta["kind"] == "image" and meta["mime"] == "image/png" and meta["size"] == len(png)
        with connect(client) as ws:
            assert turn(ws, "what is this", attachments=[meta["id"]])[-1]["type"] == "done"
    content = env.entries("user")[0]["message"]["content"]
    image = next(b for b in content if b["type"] == "image")
    assert image["source"] == {"type": "base64", "media_type": "image/png", "data": base64.b64encode(png).decode()}
    stored = env.data / "attachments" / env.store.get_attachment(meta["id"]).sha256
    assert stored.read_bytes() == png


def test_file_is_copied_into_the_project_and_the_message_names_it(env):
    with make_client(env.engine()) as client:
        meta = upload(client, "../../plan.pdf", b"%PDF-1.4 fake", "application/pdf").json()
        assert meta["kind"] == "file" and meta["name"] == "plan.pdf" and meta["mime"] == "application/pdf"
        with connect(client) as ws:
            turn(ws, "read it", attachments=[meta["id"]])
    assert (env.home / "attachments" / "plan.pdf").read_bytes() == b"%PDF-1.4 fake"
    text = env.entries("user")[0]["message"]["content"][-1]["text"]
    assert "read it" in text and "- attachments/plan.pdf" in text


def test_declared_image_type_is_not_trusted(env):
    with make_client(env.engine()) as client:
        meta = upload(client, "evil.png", b"<html>not an image</html>", "image/png").json()
    assert meta["kind"] == "file" and meta["mime"] != "image/png"


def test_attachment_limits_and_auth(env):
    with make_client(env.engine()) as client:
        assert upload(client, "big.bin", b"\0" * (25 * 1024 * 1024 + 1)).status_code == 413
        assert upload(client, "empty.bin", b"").status_code == 400
        assert client.post("/api/attachments", headers={"X-Hokma-Token": "bad"}, content=b"x").status_code == 401
        raw = client.post("/api/attachments", headers={**HEADERS, "X-Filename": "n.txt", "Content-Type": "text/plain"},
                          content=b"hello")
        assert raw.status_code == 201 and raw.json()["name"] == "n.txt"
        with connect(client) as ws:
            ws.send_json({"type": "send", "text": "x", "attachments": ["nope"]})
            assert until(ws, "error")[-1]["code"] == "attachment_not_found"

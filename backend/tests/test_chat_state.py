"""Chat state kept in the database: active project, archived conversations, attachments and tool lines in the
history, the partial answer of a live turn after a reconnect, onboarding marks and the image thumbnail route."""
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine_support import connect, env, hard_timeout, make_client, posix_only, png_bytes, turn, until  # noqa: F401
from hokma.db import migrate, open_database
from hokma.db.transfer import table_plan
from hokma.db.migrate import discover, render
from hokma.db.backend import SqliteBackend
from hokma.engine.api import create_router
from hokma.engine.conversations_api import create_router as create_conversations_router
from hokma.engine.dbstore import DbAgents, DbChatStore, DbProjects
from hokma.engine.engine import ChatEngine
from hokma.engine.protocols import DEFAULT_MODEL
from hokma.rpc.ws import SESSION_TOKEN

pytestmark = posix_only
AUTH = {"X-Hokma-Token": SESSION_TOKEN}


def build(env):
    migrate(env.data / "hokma.db")
    db = open_database(env.data / "hokma.db")
    engine = ChatEngine(store=DbChatStore(db), projects=DbProjects(db), agents=DbAgents(db),
                        launch=lambda: env.launch_spec, session_token=SESSION_TOKEN, stop_grace=0.5)
    app = FastAPI()
    app.include_router(create_router(lambda: engine))
    app.include_router(create_conversations_router(lambda: db, lambda: engine))
    return db, engine, TestClient(app)


def open_conv(ws, conversation_id):
    ws.send_json({"type": "open", "conversation_id": conversation_id})
    return until(ws, "state")


def test_migration_0002_adds_the_columns(env):
    migrate(env.data / "hokma.db")
    con = sqlite3.connect(str(env.data / "hokma.db"))
    columns = lambda table: {r[1] for r in con.execute(f"PRAGMA table_info({table})")}  # noqa: E731
    assert "active_project_id" in columns("profile")
    assert "archived" in columns("conversations")
    assert "meta" in columns("messages")


def test_transfer_copies_projects_before_the_profile_that_points_at_one():
    script = "\n".join(render(path.read_text(encoding="utf-8"), SqliteBackend(env_path()))
                       for _v, _n, path in discover())
    order = [t.name for t in table_plan(script)]
    assert order.index("projects") < order.index("profile")


def env_path():
    from pathlib import Path
    return Path("unused.db")


def test_active_project_lives_in_the_profile_and_the_old_file_is_imported_once(env):
    from hokma.projects import service
    migrate(env.data / "hokma.db")
    db = open_database(env.data / "hokma.db")
    with db.write() as u:
        u.profile.save(owner_name="Ana")
        first = u.projects.create("Fund A", str(env.home / "a"))
        second = u.projects.create("Fund B", str(env.home / "b"))
    (env.data / "active-project").write_text(str(second.id), encoding="ascii")
    assert service.active_id(db) == second.id
    assert not (env.data / "active-project").exists()                 # read once, then removed
    with db.read() as u:
        assert u.profile.get().active_project_id == second.id
    service.activate(first.id, db)
    assert service.active_id(db) == first.id
    assert not (env.data / "active-project").exists()                 # nothing writes the file any more
    with db.read() as u:
        assert u.profile.get().active_project_id == first.id


def test_a_project_that_is_gone_falls_back_to_the_oldest(env):
    from hokma.projects import service
    migrate(env.data / "hokma.db")
    db = open_database(env.data / "hokma.db")
    with db.write() as u:
        u.profile.save(owner_name="Ana")
        first = u.projects.create("Fund A", str(env.home / "a"))
        second = u.projects.create("Fund B", str(env.home / "b"))
    service.activate(second.id, db)
    with db.write() as u:
        u.projects.delete(second.id)
    assert service.active_id(db) == first.id


def test_archive_and_unarchive_with_the_list(env):
    db, engine, client = build(env)
    with client, connect(client) as ws:
        a = turn(ws, "first")[0]["conversation_id"]
        b = turn(ws, "second", conversation_id=None)[0]["conversation_id"]
    ids = lambda **q: [c["id"] for c in client.get("/api/conversations", headers=AUTH, params=q).json()["conversations"]]  # noqa: E731
    assert set(ids()) == {a, b}
    done = client.patch(f"/api/conversations/{a}", headers=AUTH, json={"archived": True}).json()
    assert done["archived"] is True
    assert ids() == [b]                                              # archived ones are left out by default
    assert ids(archived="true") == [a]
    assert client.patch(f"/api/conversations/{a}", headers=AUTH, json={"archived": False}).json()["archived"] is False
    assert set(ids()) == {a, b} and ids(archived="true") == []


def test_history_restores_attachments_and_tool_lines(env):
    db, engine, client = build(env)
    png = png_bytes()
    with client:
        up = client.post("/api/attachments", headers=AUTH, files={"file": ("chart.png", png, "image/png")}).json()
        pdf = client.post("/api/attachments", headers=AUTH, files={"file": ("plan.pdf", b"%PDF-1.4 x", "application/pdf")}).json()
        with connect(client) as ws:
            ws.send_json({"type": "send", "text": "[perm] look", "attachments": [up["id"], pdf["id"]]})
            request = until(ws, "permission_request")[-1]
            ws.send_json({"type": "permission_response", "conversation_id": request["conversation_id"],
                          "request_id": request["request_id"], "decision": "allow"})
            cid = until(ws, "done")[-1]["conversation_id"]
        with connect(client) as ws:                                    # a fresh socket = a reload
            history = open_conv(ws, cid)[-2]
        assert history["type"] == "history"
        user, assistant = history["messages"]
        assert [(a["name"], a["kind"]) for a in user["attachments"]] == [("chart.png", "image"), ("plan.pdf", "file")]
        assert user["attachments"][0]["mime"] == "image/png"
        assert len(assistant["tools"]) == 1
        line = assistant["tools"][0]
        assert line["name"] == "Write" and line["kind"] == "write_file" and line["is_error"] is False
        assert "created" in line["result"] and line["at"] >= 0
        # the thumbnail route: images only, with the token
        shown = client.get(f"/api/attachments/{up['id']}", headers=AUTH)
        assert shown.status_code == 200 and shown.headers["content-type"] == "image/png" and shown.content == png
        assert client.get(f"/api/attachments/{pdf['id']}", headers=AUTH).status_code == 404
        assert client.get(f"/api/attachments/{up['id']}", headers={"X-Hokma-Token": "bad"}).status_code == 401
    with db.read() as u:
        assert u.onboarding.done().keys() >= {"first_conversation", "first_image"}


def test_onboarding_image_step_only_for_images(env):
    db, engine, client = build(env)
    with client:
        pdf = client.post("/api/attachments", headers=AUTH, files={"file": ("plan.pdf", b"%PDF-1.4 x", "application/pdf")}).json()
        with connect(client) as ws:
            turn(ws, "read it", attachments=[pdf["id"]])
    with db.read() as u:
        done = u.onboarding.done()
    assert "first_conversation" in done and "first_image" not in done


def test_a_live_turn_keeps_its_partial_text_when_the_socket_reconnects(env):
    db, engine, client = build(env)
    with client:
        with connect(client) as ws:
            ws.send_json({"type": "send", "text": "[slow] go"})
            cid = until(ws, "turn_start")[0]["conversation_id"]
            seen = ""
            while len(seen) < 5:
                event = ws.receive_json()
                if event["type"] == "delta":
                    seen += event["text"]
        # the first socket is closed in the middle of the turn
        with connect(client) as ws2:
            events = open_conv(ws2, cid)
            history, state = events[-2], events[-1]
            assert state["state"] == "running"
            assert state["partial"]["text"].startswith(seen)             # nothing the first socket saw is lost
            assert [m["role"] for m in history["messages"]] == ["user"]  # the answer is not saved yet
            ws2.send_json({"type": "stop", "conversation_id": cid})
            until(ws2, "done")
        with connect(client) as ws3:
            state = open_conv(ws3, cid)[-1]
            assert state["state"] == "idle" and "partial" not in state


def test_new_conversations_do_not_default_to_opus(env):
    db, engine, client = build(env)
    assert DEFAULT_MODEL == "sonnet"
    with client, connect(client) as ws:
        cid = turn(ws, "hello")[0]["conversation_id"]
    with db.read() as u:
        assert u.conversations.get(int(cid)).model == "sonnet"

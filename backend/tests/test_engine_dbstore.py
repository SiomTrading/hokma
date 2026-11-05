"""The engine on the real SQLite repositories (hokma.db), with the fake claude."""
import os

from engine_support import connect, env, hard_timeout, make_client, posix_only, png_bytes, turn, until  # noqa: F401
from hokma.db import migrate, open_database
from hokma.engine.dbstore import DbAgents, DbChatStore, DbProjects
from hokma.engine.engine import ChatEngine
from hokma.rpc.ws import SESSION_TOKEN

pytestmark = posix_only


def db_engine(env, **kwargs):
    migrate(env.data / "hokma.db")
    db = open_database(env.data / "hokma.db")
    store = DbChatStore(db)
    engine = ChatEngine(store=store, projects=DbProjects(db), agents=DbAgents(db), launch=lambda: env.launch_spec,
                        session_token=SESSION_TOKEN, stop_grace=0.5, **kwargs)
    return engine, db, store


def test_turn_is_persisted_in_sqlite_and_resumed_after_restart(env):
    engine, db, store = db_engine(env)
    with make_client(engine) as client, connect(client) as ws:
        events = turn(ws, "Hello from the database")
    conv_id = events[0]["conversation_id"]
    with db.read() as u:
        conv = u.conversations.get(int(conv_id))
        rows = u.messages.list(int(conv_id))
    assert conv.engine == "claude_code" and conv.title == "Hello from the database" and conv.model == "sonnet"
    assert conv.engine_session == events[-1]["engine_session"]
    assert [(m.role, m.content) for m in rows] == [("user", "Hello from the database"), ("assistant", "pong")]
    assert rows[1].tokens_out > 0 and rows[1].model == "sonnet"
    # a new engine on the same database resumes the CLI session
    engine2, _, _ = db_engine(env)
    with make_client(engine2) as client, connect(client) as ws:
        turn(ws, "again", conversation_id=conv_id)
    assert "--resume" in env.entries("start")[-1]["argv"]


def test_permission_decisions_land_in_guard_events(env):
    engine, db, _ = db_engine(env)
    with make_client(engine) as client, connect(client) as ws:
        ws.send_json({"type": "send", "text": "[perm] write"})
        request = until(ws, "permission_request")[-1]
        ws.send_json({"type": "permission_response", "conversation_id": request["conversation_id"],
                      "request_id": request["request_id"], "decision": "deny"})
        until(ws, "done")
    with db.read() as u:
        events = u.guard_events.list(guard="permission")
    assert [(e.decision, e.tool) for e in events] == [("denied_by_human", "Write")]


def test_attachments_and_repeated_uploads_with_the_database(env):
    engine, db, store = db_engine(env)
    png = png_bytes()
    with make_client(engine) as client:
        headers = {"X-Hokma-Token": SESSION_TOKEN}
        first = client.post("/api/attachments", headers=headers, files={"file": ("a.png", png, "image/png")}).json()
        second = client.post("/api/attachments", headers=headers, files={"file": ("b.png", png, "image/png")}).json()
        assert first["id"] != second["id"]                                  # same bytes, two rows, one blob
        doc = client.post("/api/attachments", headers=headers, files={"file": ("n.txt", b"hello", "text/plain")}).json()
        with connect(client) as ws:
            assert turn(ws, "see both", attachments=[first["id"], doc["id"]])[-1]["type"] == "done"
    kinds = [b["type"] for b in env.entries("user")[0]["message"]["content"]]
    assert kinds == ["image", "text"] and (env.home / "attachments" / "n.txt").read_bytes() == b"hello"
    assert len(list((env.data / "attachments").iterdir())) == 2


def test_projects_and_agents_come_from_the_database(env):
    engine, db, _ = db_engine(env)
    folder = env.home / "fund-a"
    folder.mkdir()
    with db.write() as u:
        project = u.projects.create("Fund A", str(folder))
        u.agents.create_custom("analyst", "Analyst")
        u.agents.set_status("analyst", "active")
    with make_client(engine) as client, connect(client) as ws:
        conv_id = turn(ws, "hello")[0]["conversation_id"]
        ws.send_json({"type": "send", "text": "/proyecto fund a", "conversation_id": conv_id})
        assert until(ws, "command_result", "error")[-1]["value"]["name"] == "Fund A"
        ws.send_json({"type": "send", "text": "/agente analyst", "conversation_id": conv_id})
        assert until(ws, "command_result", "error")[-1]["value"] == "analyst"
        ws.send_json({"type": "send", "text": "/agente ghost", "conversation_id": conv_id})
        assert until(ws, "command_result", "error")[-1]["code"] == "unknown_agent"
        assert turn(ws, "in the project", conversation_id=conv_id)[-1]["type"] == "done"
    last = env.entries("start")[-1]
    assert os.path.realpath(last["cwd"]) == os.path.realpath(folder) and "analyst" in last["argv"]
    with db.read() as u:
        assert u.conversations.get(int(conv_id)).project_id == project.id

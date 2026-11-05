"""Conversation list, search, pin, rename, delete, command options and "save to the project" (REST of the chat panel)."""
import shutil
import subprocess
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine_support import connect, env, hard_timeout, make_client, posix_only, png_bytes, turn, until  # noqa: F401
from hokma.db import migrate, open_database
from hokma.engine.api import create_router
from hokma.engine.conversations_api import create_router as create_conversations_router
from hokma.engine.dbstore import DbAgents, DbChatStore, DbProjects
from hokma.engine.engine import ChatEngine
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


def test_every_route_needs_the_session_token(env, monkeypatch):
    monkeypatch.setattr("hokma.engine.api.database_ready", lambda: True)
    _, _, client = build(env)
    with client:
        for method, path in (("get", "/api/conversations"), ("patch", "/api/conversations/1"),
                             ("delete", "/api/conversations/1"), ("get", "/api/chat/options"),
                             ("post", "/api/conversations/1/save")):
            kwargs = {"json": {"pinned": True}} if method in ("patch", "post") else {}
            assert getattr(client, method)(path, headers={"X-Hokma-Token": "wrong"}, **kwargs).status_code == 401, path


def test_list_search_pin_rename_delete(env, monkeypatch):
    monkeypatch.setattr("hokma.engine.api.database_ready", lambda: True)
    db, engine, client = build(env)
    with client, connect(client) as ws:
        first = turn(ws, "Opening range breakout plan")[0]["conversation_id"]
        second = turn(ws, "Weekly review notes")[0]["conversation_id"]
        rows = client.get("/api/conversations", headers=AUTH).json()["conversations"]
        assert {r["id"] for r in rows} == {first, second} and rows[0]["message_count"] == 2

        by_content = client.get("/api/conversations", params={"q": "pong"}, headers=AUTH).json()["conversations"]
        assert len(by_content) == 2
        by_title = client.get("/api/conversations", params={"q": "breakout"}, headers=AUTH).json()["conversations"]
        assert [r["id"] for r in by_title] == [first]
        assert client.get("/api/conversations", params={"q": "100%_nothing"}, headers=AUTH).json()["conversations"] == []

        pinned = client.patch(f"/api/conversations/{first}", json={"pinned": True}, headers=AUTH).json()
        assert pinned["pinned"] is True
        listing = client.get("/api/conversations", headers=AUTH).json()["conversations"]
        assert listing[0]["id"] == first                              # pinned goes first
        renamed = client.patch(f"/api/conversations/{first}", json={"title": "  My plan "}, headers=AUTH).json()
        assert renamed["title"] == "My plan"
        assert client.patch(f"/api/conversations/{first}", json={}, headers=AUTH).status_code == 400
        assert client.patch(f"/api/conversations/{first}", json={"title": ""}, headers=AUTH).status_code == 422
        assert client.patch("/api/conversations/9999", json={"pinned": True}, headers=AUTH).status_code == 404

        assert client.delete(f"/api/conversations/{second}", headers=AUTH).status_code == 204
        assert client.delete(f"/api/conversations/{second}", headers=AUTH).status_code == 404
    with db.read() as u:
        assert [c.id for c in u.conversations.list()] == [int(first)]


def test_empty_conversations_are_not_listed(env, monkeypatch):
    monkeypatch.setattr("hokma.engine.api.database_ready", lambda: True)
    db, engine, client = build(env)
    with client, connect(client) as ws:
        ws.send_json({"type": "command", "conversation_id": None, "name": "model", "arg": "opus"})
        until(ws, "command_result")
        assert client.get("/api/conversations", headers=AUTH).json()["conversations"] == []


def test_options_list_agents_projects_and_the_default_project(env, monkeypatch):
    monkeypatch.setattr("hokma.engine.api.database_ready", lambda: True)
    db, _, client = build(env)
    with db.write() as u:
        u.agents.create_custom("risk-desk", "Risk desk", description="Watches the limits")
        u.agents.set_status("risk-desk", "active")
        u.agents.create_custom("parked", "Parked")                   # stays off: not offered
        u.projects.create("Strategy", str(env.home / "strategy"))
    with client:
        data = client.get("/api/chat/options", headers=AUTH).json()
    assert any(a["slug"] == "risk-desk" and a["description"] == "Watches the limits" for a in data["agents"])
    assert "parked" not in [a["slug"] for a in data["agents"]]
    assert [p["name"] for p in data["projects"]] == ["Strategy"]
    assert data["default_project"] == {"id": data["projects"][0]["id"], "name": "Strategy"}   # the active project


def test_save_an_answer_and_an_attachment_into_the_project_with_a_git_commit(env, monkeypatch):
    monkeypatch.setattr("hokma.engine.api.database_ready", lambda: True)
    db, engine, client = build(env)
    with db.write() as u:
        u.profile.set_owner("Owner", "Fund")
    with client, connect(client) as ws:
        up = client.post("/api/attachments", content=png_bytes(), headers={**AUTH, "X-Filename": "chart.png"}).json()
        events = turn(ws, "Save this chart", attachments=[up["id"]])
        conv = events[0]["conversation_id"]
        answer_id = [e for e in events if e["type"] == "done"][0]["message_id"]

        saved = client.post(f"/api/conversations/{conv}/save", json={"message_id": answer_id}, headers=AUTH)
        assert saved.status_code == 201, saved.text
        body = saved.json()
        project_dir = env.home / "projects" / "principal"
        md = project_dir / body["path"]
        assert md.read_text(encoding="utf-8") == "pong" and md.suffix == ".md"
        assert body["git"]["ok"] is True and body["project"]["name"] == "Fund"
        log = subprocess.run(["git", "-C", str(project_dir), "log", "--format=%s", "-3"], capture_output=True, text=True).stdout
        assert log.strip()
        again = client.post(f"/api/conversations/{conv}/save", json={"message_id": answer_id}, headers=AUTH).json()
        assert again["path"] == body["path"]                           # same content is stored once
        assert len(list((project_dir / "documentos").glob("*.md"))) == 1

        image = client.post(f"/api/conversations/{conv}/save", json={"attachment_id": up["id"]}, headers=AUTH).json()
        assert image["path"].startswith("imagenes/") and (project_dir / image["path"]).read_bytes() == png_bytes()

        assert client.post(f"/api/conversations/{conv}/save", json={}, headers=AUTH).status_code == 400
        both = {"message_id": answer_id, "attachment_id": up["id"]}
        assert client.post(f"/api/conversations/{conv}/save", json=both, headers=AUTH).status_code == 400
        assert client.post(f"/api/conversations/{conv}/save", json={"message_id": "99999"}, headers=AUTH).status_code == 404
        assert client.post("/api/conversations/99999/save", json={"message_id": answer_id}, headers=AUTH).status_code == 404


def test_saving_never_follows_a_symlink_out_of_the_project(env, monkeypatch):
    monkeypatch.setattr("hokma.engine.api.database_ready", lambda: True)
    db, engine, client = build(env)
    outside = env.data / "outside"
    outside.mkdir()
    with db.write() as u:
        u.profile.set_owner("Owner", "Fund")
    from hokma.projects import service
    created = service.create_project("Linked", "other", db=db)
    folder = Path(created["path"]) if "path" in created else env.home / "projects" / "linked"
    shutil.rmtree(folder / "documentos", ignore_errors=True)
    (folder / "documentos").symlink_to(outside, target_is_directory=True)
    with client, connect(client) as ws:
        events = turn(ws, "hello", project_id=str(created["id"]))
        conv = events[0]["conversation_id"]
        answer_id = [e for e in events if e["type"] == "done"][0]["message_id"]
        res = client.post(f"/api/conversations/{conv}/save", json={"message_id": answer_id}, headers=AUTH)
    assert res.status_code >= 400
    assert list(outside.iterdir()) == []

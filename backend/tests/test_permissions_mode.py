"""Permission mode (tests P1, P2, P9), the guard_block event and the read-only list of protections."""
import asyncio
import shutil
import sqlite3
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine_support import Env, connect, posix_only, turn, until
from hokma.db import Actor, IntegrityViolation, RuleViolation, current_version, get_database, migrate, open_database
from hokma.db.migrate import MIGRATIONS_DIR
from hokma.engine.api import create_router
from hokma.engine.conversations_api import create_router as create_conversations_router
from hokma.engine.dbstore import DbAgents, DbChatStore, DbProjects
from hokma.engine.engine import ChatEngine
from hokma.permissions.api import create_router as create_permissions_router
from hokma.rpc.ws import SESSION_TOKEN
from permissions_support import HEADERS, owner  # noqa: F401  (fixture)

AUTH = HEADERS


def v2_database(path, tmp_path):
    """A database as the 0.0.5 version left it: only migrations 1 and 2."""
    old = tmp_path / "old-migrations"
    old.mkdir()
    for name in ("0001_base.sql", "0002_chat_state.sql"):
        shutil.copy(MIGRATIONS_DIR / name, old / name)
    migrate(path, migrations_dir=old, seed=False)


# ---------------------------------------------------------------------------------------- the migration

def test_migrations_0003_and_0004_keep_the_data_and_the_migrated_owner_stays_in_ask(tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    path = tmp_path / "data" / "hokma.db"
    v2_database(path, tmp_path)
    db = open_database(path)
    with db.write() as u:
        u.profile.save(owner_name="Ana")
        u.profile.complete_setup()
        u.guard_events.record("permission", "blocked", "an old block", tool="Write")
    result = migrate(path)
    assert result.applied == [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14] and result.version == 14 and result.backup is not None and result.backup.exists()
    with db.read() as u:
        profile = u.profile.get()
        events = u.guard_events.list()
    assert (profile.permission_mode, profile.permission_mode_intro_seen, profile.update_checked_at) == ("ask", False, None)
    assert [e.reason for e in events] == ["an old block"]                       # the rebuilt table kept its rows
    con = sqlite3.connect(path)
    columns = {r[1] for r in con.execute("PRAGMA table_info(profile)")}
    assert {"permission_mode", "permission_mode_intro_seen", "update_checked_at"} <= columns
    assert "autostart" not in columns
    assert {r[1] for r in con.execute("PRAGMA table_info(trash_items)")} == {
        "id", "original_path", "stored_path", "source_root", "size_bytes", "project_id", "conversation_id", "reason", "moved_at",
        "restored_at", "purged_at"}
    assert con.execute("PRAGMA foreign_key_check").fetchall() == [] and con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    again = migrate(path)                                                       # idempotent
    assert again.applied == [] and again.backup is None


def test_guard_events_accept_the_new_rules_only_and_stay_append_only(owner):
    with owner.db.write() as u:
        for rule in ("delete", "hokma_internal", "outside_workdir", "secrets", "real_money"):
            u.guard_events.record_block(rule, "Bash", f"target for {rule}")
        first = u.guard_events.list()[-1]
    with pytest.raises(IntegrityViolation):
        with owner.db.write() as u:
            u.guard_events.record("not_a_rule", "blocked", "x")
    with pytest.raises(Exception):
        with owner.db.write() as u:
            u.guard_events.record_block("not_a_rule", "Bash", "x")
    with pytest.raises(RuleViolation):
        with owner.db.write() as u:
            u.session.execute("UPDATE guard_events SET reason = 'changed' WHERE id = :id", {"id": first.id})
    with pytest.raises(RuleViolation):
        with owner.db.write() as u:
            u.session.execute("DELETE FROM guard_events")


def test_the_profile_rejects_other_modes(owner):
    with pytest.raises(Exception):
        with owner.db.write() as u:
            u.profile.set_permission_mode("yolo")


# ----------------------------------------------------------------------------------------- P1 and P2

def test_p1_a_new_install_ends_the_first_run_in_auto_with_the_notice_already_seen(owner):
    assert owner.profile().permission_mode == "ask"
    state = owner.client.post("/api/setup", headers=HEADERS).json()
    assert state["profile"]["permission_mode"] == "auto" and state["profile"]["permission_mode_intro_seen"] is True
    assert owner.client.get("/api/setup", headers=HEADERS).json()["profile"]["permission_mode"] == "auto"


def test_p1_the_owner_who_chose_ask_on_the_ready_card_keeps_it(owner):
    assert owner.client.put("/api/profile/permission-mode", json={"mode": "ask"}, headers=HEADERS).status_code == 200
    state = owner.client.post("/api/setup", headers=HEADERS).json()
    assert state["profile"]["permission_mode"] == "ask"


def test_p1_one_migrated_from_005_stays_in_ask_with_the_notice_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOKMA_HOME", str(tmp_path / "work"))
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOKMA_LANG", "en")
    (tmp_path / "work").mkdir()
    v2_database(tmp_path / "data" / "hokma.db", tmp_path)
    with open_database(tmp_path / "data" / "hokma.db").write() as u:
        u.profile.save(owner_name="Ana", workdir=str(tmp_path / "work"))
        u.profile.complete_setup()
    migrate()
    from hokma.core.app import create_app

    client = TestClient(create_app())
    profile = client.get("/api/setup", headers=HEADERS).json()["profile"]
    assert profile["permission_mode"] == "ask" and profile["permission_mode_intro_seen"] is False
    assert client.post("/api/setup", headers=HEADERS).json()["profile"]["permission_mode"] == "ask"   # still "ask"
    done = client.put("/api/profile/permission-mode", json={"intro_seen": True}, headers=HEADERS).json()
    assert done["mode"] == "ask" and done["intro_seen"] is True                  # dismissing the notice changes no mode
    on = client.put("/api/profile/permission-mode", json={"mode": "auto"}, headers=HEADERS).json()
    assert on["mode"] == "auto" and on["changed"] is True


def test_p2_the_endpoint_needs_the_session_token_and_a_valid_mode(owner):
    c = owner.client
    for headers in ({}, {"X-Hokma-Token": "wrong"}):
        assert c.put("/api/profile/permission-mode", json={"mode": "auto"}, headers=headers).status_code == 401
        assert c.get("/api/profile/permission-mode", headers=headers).status_code == 401
        assert c.get("/api/protections", headers=headers).status_code == 401
    assert owner.profile().permission_mode == "ask"
    for body in ({"mode": "yolo"}, {"mode": ""}, {}, {"mode": "auto; DROP"}):
        res = c.put("/api/profile/permission-mode", json=body, headers=HEADERS)
        assert res.status_code == 400 and res.json()["detail"]["code"] == "bad_mode", body
    assert owner.profile().permission_mode == "ask"


def test_p2_a_change_is_saved_survives_a_restart_and_is_written_in_the_journal_as_the_owner(owner):
    res = owner.client.put("/api/profile/permission-mode", json={"mode": "auto"}, headers=HEADERS).json()
    assert res == {"mode": "auto", "intro_seen": True, "changed": True, "turns_running": 0}
    again = owner.client.put("/api/profile/permission-mode", json={"mode": "auto"}, headers=HEADERS).json()
    assert again["changed"] is False
    from hokma.core.app import create_app

    restarted = TestClient(create_app())                                         # a fresh app on the same database
    assert restarted.get("/api/profile/permission-mode", headers=HEADERS).json() == {"mode": "auto", "intro_seen": True}
    with owner.db.read() as u:
        entries = [e for e in u.journal.list() if "Permission mode" in e.title]
    assert [(e.actor_kind, e.title) for e in entries] == [("human", "Permission mode set to auto")]


def test_p2_no_agent_route_reaches_the_mode(owner):
    """Every way to write it needs the per-boot token that never reaches an agent; the agent has one tool."""
    from hokma.core.app import create_app

    routes = {(m, r.path) for r in create_app().routes for m in getattr(r, "methods", ()) if "permission" in r.path}
    assert routes == {("GET", "/api/profile/permission-mode"), ("PUT", "/api/profile/permission-mode")}


def test_the_protections_list_is_read_only_there_is_no_switch(owner):
    res = owner.client.get("/api/protections", headers=HEADERS).json()
    assert res["always_on"] is True
    assert [r["id"] for r in res["rules"]] == ["delete", "hokma_internal", "outside_workdir", "secrets",
                                               "real_money"]
    assert all(r["title"] for r in res["rules"])
    for method in ("put", "post", "delete", "patch"):
        assert getattr(owner.client, method)("/api/protections", headers=HEADERS).status_code == 405


# ----------------------------------------------------------------------- the engine and the fake claude

pytestmark_engine = pytest.mark.skipif(sys.platform == "win32", reason="the fake claude is a POSIX script")


def build(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    migrate(env.data / "hokma.db")
    db = open_database(env.data / "hokma.db")
    with db.write() as u:
        u.profile.save(owner_name="Ana")
    engine = ChatEngine(store=DbChatStore(db), projects=DbProjects(db), agents=DbAgents(db),
                        launch=lambda: env.launch_spec, session_token=SESSION_TOKEN, stop_grace=0.5)
    app = FastAPI()
    app.include_router(create_router(lambda: engine))
    app.include_router(create_conversations_router(lambda: db, lambda: engine))
    app.include_router(create_permissions_router(lambda: engine))
    return env, db, engine, TestClient(app)


@posix_only
def test_p2_the_launch_follows_the_mode_and_a_change_restarts_with_resume(tmp_path, monkeypatch):
    env, db, engine, client = build(tmp_path, monkeypatch)
    with client:
        with connect(client) as ws:
            events = turn(ws, "first, in ask")
            cid = events[-1]["conversation_id"]
            assert client.put("/api/profile/permission-mode", json={"mode": "auto"}, headers=AUTH).json()["mode"] == "auto"
            turn(ws, "second, in auto", conversation_id=cid)
            client.put("/api/profile/permission-mode", json={"mode": "ask"}, headers=AUTH)
            turn(ws, "third, back in ask", conversation_id=cid)
    starts = env.entries("start")
    modes_used = [s["argv"][s["argv"].index("--permission-mode") + 1] for s in starts]
    assert modes_used == ["default", "bypassPermissions", "default"]
    assert "--session-id" in starts[0]["argv"] and "--resume" in starts[1]["argv"] and "--resume" in starts[2]["argv"]
    assert "HOKMA_SESSION_TOKEN" not in starts[0]["env"] and SESSION_TOKEN not in starts[0]["env"].values()
    assert starts[0]["env"]["HOKMA_WORK_ROOT"] == str(env.home) and starts[0]["env"]["HOKMA_CONVERSATION_ID"] == cid


@posix_only
def test_p2_a_change_while_a_turn_runs_applies_when_it_ends(tmp_path, monkeypatch):
    env, db, engine, client = build(tmp_path, monkeypatch)
    with client:
        with connect(client) as ws:
            ws.send_json({"type": "send", "text": "[slow] keep going"})
            cid = until(ws, "turn_start")[-1]["conversation_id"]
            until(ws, "delta")
            res = client.put("/api/profile/permission-mode", json={"mode": "auto"}, headers=AUTH).json()
            assert res["turns_running"] == 1                     # the pill tells the owner it applies at the end
            ws.send_json({"type": "stop", "conversation_id": cid})
            until(ws, "done")
            turn(ws, "next turn", conversation_id=cid)
    starts = env.entries("start")
    assert [s["argv"][s["argv"].index("--permission-mode") + 1] for s in starts] == ["default", "bypassPermissions"]


@posix_only
def test_a_block_by_the_hook_becomes_a_row_a_guard_block_event_and_a_card_in_the_history(tmp_path, monkeypatch):
    env, db, engine, client = build(tmp_path, monkeypatch)
    with client:
        with connect(client) as ws:
            events = turn(ws, "[block] please delete the old report")
            cid = events[-1]["conversation_id"]
            card = next(e for e in events if e["type"] == "guard_block")
            assert card["rule"] == "delete" and card["target"] == "rm informes/viejo.md" and card["tool"] == "Bash"
            assert card["conversation_id"] == cid and card["id"] > 0 and card["at"]
            assert events.index(card) < len(events) - 1           # before the turn's `done`
        with connect(client) as ws:                               # a reload still shows the card
            ws.send_json({"type": "open", "conversation_id": cid})
            history = until(ws, "state")[-2]
        assistant = history["messages"][-1]
        assert [(b["rule"], b["target"]) for b in assistant["blocks"]] == [("delete", "rm informes/viejo.md")]
        assert client.get("/api/guard-events", headers=AUTH).json()["events"][0]["rule"] == "delete"
    with db.read() as u:
        rows = u.guard_events.list(guard="delete")
    assert [(r.decision, r.tool, r.reason) for r in rows] == [("blocked", "Bash", "rm informes/viejo.md")]   # stored once
    with connect(client) as ws:
        turn(ws, "another turn")                                  # the same line is not stored again
    with db.read() as u:
        assert len(u.guard_events.list(guard="delete")) == 1


@posix_only
def test_p9_routines_use_the_same_mode_and_a_blocked_attempt_is_recorded(tmp_path, monkeypatch):
    from agents_support import Work
    from hokma.kit.sync import sync_kit
    from agents_support import KIT_TEMPLATE
    from hokma.agents.service import AgentService
    from hokma.scheduler import runner

    work = Work(tmp_path, monkeypatch)
    sync_kit(KIT_TEMPLATE, version="test", workdir=work.home, values={"owner_name": "Ana", "fund_name": "F"})
    migrate()
    db = get_database()
    with db.write() as u:
        u.profile.save(owner_name="Ana", fund_name="F", timezone="UTC", workdir=str(work.home))
    AgentService(db).reconcile()

    def run(prompt, name):
        with db.write() as u:
            job = u.scheduled_jobs.create(name, "hokma", prompt, "once", "2026-11-05T00:00", "UTC")
        return asyncio.run(runner.JobRunner(db, launch=lambda: work.launch_spec).run(job))

    first = run("write the weekly note", "ask run")
    with db.write() as u:
        u.profile.set_permission_mode("auto", intro_seen=True)
    second = run("[block] write the weekly note and try to remove the old one", "auto run")
    assert first.status == second.status == "done"
    starts = work.entries("start")
    flag = lambda s: s["argv"][s["argv"].index("--permission-mode") + 1]      # noqa: E731
    assert [flag(s) for s in starts] == ["default", "bypassPermissions"]
    assert starts[1]["env"]["HOKMA_TASK_ID"] == str(second.task_id)
    with db.read() as u:
        blocked = u.guard_events.list(guard="delete")
    assert [(e.decision, e.task_id) for e in blocked] == [("blocked", second.task_id)]

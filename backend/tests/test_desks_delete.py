"""Delete a desk (task 3286): the folder goes to the Trash and can be restored; the desk, its team and its project row
go; conversations and the student's agents stay; one desk never deletes another."""
import os
from pathlib import Path

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hokma.agents.api import create_router as agents_router
from hokma.desks import service
from hokma.desks.api import router as desks_router
from hokma.permissions import trash
from hokma.rpc.ws import SESSION_TOKEN

SLUGS = ("desk-chief", "hugo", "risk")


@pytest.fixture
def desks_db(db):
    with db.write() as u:
        u.profile.save(owner_name="Ana", fund_name="Fondo Prueba")
        for slug in SLUGS:
            u.agents.upsert_kit(slug, slug.title(), f"{slug} agent", "balanced", f"agents/{slug}.md")
            u.agents.set_status(slug, "active")
    return db


@pytest.fixture
def client(desks_db):
    app = FastAPI()
    app.include_router(desks_router)
    app.include_router(agents_router())
    with TestClient(app) as c:
        c.headers.update({"X-Hokma-Token": SESSION_TOKEN})
        yield c


def make(db, name):
    return service.create_desk(name, 10000, template="minimal", db=db)


def folder_of(db, desk):
    with db.read() as u:
        return Path(u.projects.get(desk["project_id"]).path)


def test_delete_moves_folder_to_trash_and_removes_rows(client, desks_db):
    desk = make(desks_db, "Futuros NQ")
    folder = folder_of(desks_db, desk)
    assert folder.is_dir()
    res = client.delete(f"/api/desks/{desk['id']}")
    assert res.status_code == 200, res.text
    assert not folder.exists()
    with desks_db.read() as u:
        item = u.trash.get(res.json()["trash_id"])
        assert item.original_path == str(folder) and os.path.isdir(item.stored_path)
        assert u.desks.find(desk["id"]) is None
        assert u.projects.find(desk["project_id"]) is None
        assert u.desks.list_agents(desk["id"]) == [] and u.desks.list_assets(desk["id"]) == []
    assert client.get(f"/api/desks/{desk['id']}").status_code == 404


def test_delete_keeps_agents_and_conversations(client, desks_db):
    desk = make(desks_db, "Futuros NQ")
    with desks_db.write() as u:
        u.conversations.create(engine="claude", project_id=desk["project_id"], title="chat")
    agents_before = {a.slug for a in _agents(desks_db)}
    count_before = _count(desks_db, "conversations")
    assert client.delete(f"/api/desks/{desk['id']}").status_code == 200
    assert {a.slug for a in _agents(desks_db)} == agents_before
    assert _count(desks_db, "conversations") == count_before


def _agents(db):
    with db.read() as u:
        return u.agents.list()


def _count(db, table):
    with db.read() as u:
        return len(u.conversations.list())


def test_deleted_folder_can_be_restored(client, desks_db):
    desk = make(desks_db, "Futuros NQ")
    folder = folder_of(desks_db, desk)
    res = client.delete(f"/api/desks/{desk['id']}").json()
    from hokma.kit.settings import protected_paths
    out = trash.restore(desks_db, res["trash_id"], protect=[str(p) for p in protected_paths()])
    assert Path(out["restored_to"]) == folder and (folder / "CLAUDE.md").is_file()


def test_unknown_desk_is_404(client):
    res = client.delete("/api/desks/999")
    assert res.status_code == 404 and res.json()["detail"]["code"] == "not_found"


def test_one_desk_does_not_delete_another(client, desks_db):
    a, b = make(desks_db, "Mesa A"), make(desks_db, "Mesa B")
    assert client.delete(f"/api/desks/{a['id']}").status_code == 200
    assert folder_of(desks_db, b).is_dir()
    assert client.get(f"/api/desks/{b['id']}").status_code == 200
    assert len(client.get(f"/api/desks/{b['id']}/agents").json()["agents"]) == b["agents_total"]


def test_missing_folder_still_deletes(client, desks_db):
    import shutil
    desk = make(desks_db, "Futuros NQ")
    shutil.rmtree(folder_of(desks_db, desk))
    res = client.delete(f"/api/desks/{desk['id']}")
    assert res.status_code == 200 and res.json()["trash_id"] is None


def test_a_desk_that_already_worked_is_deleted_and_its_history_stays_archived(client, desks_db):
    """A real, used desk: a chat with the chief, a task, a budget warning, a decision and its approval."""
    from hokma.db.models import Actor
    from hokma.desks.budget import SYSTEM

    desk = make(desks_db, "Futuros NQ")
    folder = folder_of(desks_db, desk)
    pid = desk["project_id"]
    with desks_db.write() as u:
        conv = u.conversations.create(engine="claude", project_id=pid, title="chat with the chief")
        task = u.tasks.create("review the NQ", actor=SYSTEM, project_id=pid, assigned_agent="hugo")
        u.journal.add("alert", "Budget at 80%", actor=SYSTEM, outcome="info", project_id=pid)
        u.journal.add("action", "Hugo closed the task", actor=Actor.agent("hugo"), outcome="success", project_id=pid,
                      task_id=task.id)
        decision = u.decisions.propose("Raise the size", "x2", proposed_by="hugo", project_id=pid)
        u.approvals.record("decision", decision.id, "approved", decided_by="Ana")
        u.approvals.record("task", task.id, "approved", decided_by="Ana")
    res = client.delete(f"/api/desks/{desk['id']}")
    assert res.status_code == 200, res.text
    assert not folder.exists()
    with desks_db.read() as u:
        assert u.desks.find(desk["id"]) is None and u.projects.find(pid) is None
        project = u.projects.find(pid, include_archived=True)
        assert project.name == "Futuros NQ" and project.archived_at
        assert [e.title for e in u.journal.list(project_id=pid)] == ["Hugo closed the task", "Budget at 80%"]
        assert u.decisions.get(decision.id).project_id == pid
        assert len(u.approvals.list_for("decision", decision.id)) == 1 and len(u.approvals.list_for("task", task.id)) == 1
        assert u.conversations.get(conv.id).project_id is None and u.tasks.get(task.id).project_id is None
        assert u.journal.count(task_id=task.id) == 1
    assert client.get(f"/api/desks/{desk['id']}").status_code == 404
    # the student can open a new desk with the same name afterwards
    assert make(desks_db, "Futuros NQ")["id"] != desk["id"]


def test_the_message_never_names_the_journal(client, desks_db):
    desk = make(desks_db, "Futuros NQ")
    from hokma.desks.budget import SYSTEM
    with desks_db.write() as u:
        u.journal.add("alert", "Budget at 100%", actor=SYSTEM, project_id=desk["project_id"])
    res = client.delete(f"/api/desks/{desk['id']}")
    assert res.status_code == 200
    assert "diario" not in res.text.lower() and "journal" not in res.text.lower()


# ---- bug 1208: the cause is written to engine.log and the message says what to do ----------------

def _fail_move(monkeypatch, exc):
    def boom(*a, **k):
        raise trash.TrashError("move_failed", "private path C:\\Users\\x") from exc
    monkeypatch.setattr(trash, "move", boom)


def test_folder_in_use_says_to_close_chats_and_terminals(desks_db, monkeypatch):
    from hokma.core import config
    from hokma.i18n import t
    desk = make(desks_db, "Mesa Uso")
    _fail_move(monkeypatch, PermissionError(13, "in use"))
    with pytest.raises(service.DeskError) as err:
        service.delete_desk(desk["id"], desks_db)
    assert err.value.code == "delete_in_use" and err.value.message == t("desks.err.delete_in_use")
    log = config.engine_log_path().read_text(encoding="utf-8")
    assert "desks:" in log and "move_failed" in log and "PermissionError" in log and "private path" not in log


def test_other_move_failure_and_db_failure_have_their_own_message(desks_db, monkeypatch):
    from hokma.db import DbError
    desk = make(desks_db, "Mesa Otra")
    _fail_move(monkeypatch, OSError(5, "io"))
    with pytest.raises(service.DeskError) as err:
        service.delete_desk(desk["id"], desks_db)
    assert err.value.code == "delete_move_failed"
    assert service._log_delete_failure(DbError("x"), desk["id"]) == "delete_db_failed"


def test_terminals_inside_the_desk_folder_are_closed_before_the_move(desks_db):
    from hokma.terminal.session import registry
    desk = make(desks_db, "Mesa Term")
    folder = Path(folder_of(desks_db, desk))

    class Pty:
        killed = False
        def kill_now(self): self.killed = True

    class Sess:
        def __init__(self, cwd): self.cwd, self.pty, self.closed = str(cwd), Pty(), False

    inside, outside = Sess(folder / "sub"), Sess(folder.parent)
    registry.sessions.update({inside, outside})
    try:
        service.delete_desk(desk["id"], desks_db)
        assert inside.pty.killed and inside not in registry.sessions
        assert not outside.pty.killed and outside in registry.sessions
    finally:
        registry.sessions.difference_update({inside, outside})

"""Delete a project (task 3288): the folder goes to the Trash and can be restored; the conversations stay; General and
the folder of a desk are refused; the active project going sends the app back to General; another project is intact."""
import json
import os
from pathlib import Path

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from fastapi.testclient import TestClient

from hokma.core.app import create_app
from hokma.desks import service as desks
from hokma.desks.budget import SYSTEM
from hokma.i18n import t
from hokma.permissions import trash
from hokma.projects import service
from hokma.rpc.ws import SESSION_TOKEN

H = {"X-Hokma-Token": SESSION_TOKEN}


@pytest.fixture
def client(db):
    with db.write() as u:
        u.profile.save(owner_name="Ana", fund_name="Fondo Prueba")
    return TestClient(create_app())


def make(client, name):
    r = client.post("/api/projects", headers=H, json={"name": name, "kind": "strategy"})
    assert r.status_code == 201, r.text
    return r.json()


def test_delete_moves_the_folder_to_the_trash_and_removes_the_row(client, db):
    p = make(client, "Alpha")
    folder = Path(p["path"])
    assert folder.is_dir()
    res = client.delete(f"/api/projects/{p['id']}", headers=H)
    assert res.status_code == 200, res.text
    assert not folder.exists()
    with db.read() as u:
        item = u.trash.get(res.json()["trash_id"])
        assert item.original_path == str(folder) and os.path.isdir(item.stored_path)
        assert item.reason == t("projects.trash_reason", name="Alpha")
        assert u.projects.find(p["id"]) is None
    assert p["id"] not in [x["id"] for x in client.get("/api/projects", headers=H).json()["projects"]]
    assert client.delete(f"/api/projects/{p['id']}", headers=H).status_code == 404


def test_deleted_project_folder_can_be_restored(client, db):
    p = make(client, "Alpha")
    folder = Path(p["path"])
    (folder / "contexto" / "nota.md").write_text("hola")
    res = client.delete(f"/api/projects/{p['id']}", headers=H).json()
    from hokma.kit.settings import protected_paths
    out = trash.restore(db, res["trash_id"], protect=[str(x) for x in protected_paths()])
    assert Path(out["restored_to"]) == folder
    assert (folder / "contexto" / "nota.md").read_text() == "hola"


def test_delete_keeps_the_conversations(client, db):
    p = make(client, "Alpha")
    with db.write() as u:
        conv = u.conversations.create(engine="claude", project_id=p["id"], title="chat de alpha")
    before = _conversations(db)
    assert client.delete(f"/api/projects/{p['id']}", headers=H).status_code == 200
    after = _conversations(db)
    assert before == after and conv.id in after


def _conversations(db):
    with db.read() as u:
        return {c.id for c in u.conversations.list()}


def test_general_cannot_be_deleted(client, db):
    # General is the working folder: it has no row. A row pointing at it is refused all the same.
    work = service.workdir(db)
    work.mkdir(parents=True, exist_ok=True)
    with db.write() as u:
        row = u.projects.create("General fake", str(work), slug="general-fake")
    res = client.delete(f"/api/projects/{row.id}", headers=H)
    assert res.status_code == 400 and res.json()["detail"]["code"] == "delete_general"
    assert work.is_dir()
    with db.read() as u:
        assert u.projects.find(row.id) is not None


def test_the_folder_of_a_desk_cannot_be_deleted_from_projects(client, db):
    with db.write() as u:
        for slug in ("desk-chief", "hugo", "risk"):
            u.agents.upsert_kit(slug, slug.title(), f"{slug} agent", "balanced", f"agents/{slug}.md")
            u.agents.set_status(slug, "active")
    desk = desks.create_desk("Futuros NQ", 10000, template="minimal", db=db)
    with db.read() as u:
        folder = Path(u.projects.get(desk["project_id"]).path)
    res = client.delete(f"/api/projects/{desk['project_id']}", headers=H)
    assert res.status_code == 409 and res.json()["detail"]["code"] == "delete_desk"
    assert res.json()["detail"]["message"] == t("projects.err.delete_desk")
    assert folder.is_dir()
    with db.read() as u:
        assert u.desks.find(desk["id"]) is not None and u.projects.find(desk["project_id"]) is not None


def test_deleting_the_active_project_goes_back_to_general(client, db):
    a, b = make(client, "Alpha"), make(client, "Beta")
    client.post(f"/api/projects/{b['id']}/activate", headers=H)
    assert client.get("/api/projects", headers=H).json()["active_id"] == b["id"]
    res = client.delete(f"/api/projects/{b['id']}", headers=H)
    assert res.status_code == 200 and res.json()["was_active"] is True
    listing = client.get("/api/projects", headers=H).json()
    assert listing["active_id"] is None
    assert [x["id"] for x in listing["projects"] if x["id"] == a["id"]] == [a["id"]]


def test_deleting_another_project_keeps_the_active_one(client, db):
    a, b = make(client, "Alpha"), make(client, "Beta")
    client.post(f"/api/projects/{a['id']}/activate", headers=H)
    res = client.delete(f"/api/projects/{b['id']}", headers=H)
    assert res.json()["was_active"] is False
    assert client.get("/api/projects", headers=H).json()["active_id"] == a["id"]


def test_another_project_is_intact(client, db):
    a, b = make(client, "Alpha"), make(client, "Beta")
    (Path(a["path"]) / "contexto" / "x.md").write_text("x")
    assert client.delete(f"/api/projects/{b['id']}", headers=H).status_code == 200
    assert (Path(a["path"]) / "contexto" / "x.md").read_text() == "x"
    with db.read() as u:
        assert u.projects.find(a["id"]) is not None
    assert client.get(f"/api/projects/{a['id']}/tree", headers=H).status_code == 200


def test_a_missing_folder_still_deletes_the_row(client, db):
    import shutil
    p = make(client, "Alpha")
    shutil.rmtree(p["path"])
    res = client.delete(f"/api/projects/{p['id']}", headers=H)
    assert res.status_code == 200 and res.json()["trash_id"] is None


def test_a_project_with_a_journal_is_deleted_and_the_journal_stays_archived(client, db):
    p = make(client, "Alpha")
    folder = Path(p["path"])
    with db.write() as u:
        conv = u.conversations.create(engine="claude", project_id=p["id"], title="chat de alpha")
        u.agents.upsert_kit("hugo", "Hugo", "hugo agent", "balanced", "agents/hugo.md")
        u.journal.add("note", "hi", actor=SYSTEM, outcome="info", project_id=p["id"])
        task = u.tasks.create("tarea de alpha", actor=SYSTEM, project_id=p["id"])
        decision = u.decisions.propose("Tema", "propuesta", proposed_by="hugo", project_id=p["id"])
        u.approvals.record("decision", decision.id, "approved", decided_by="Ana")
    res = client.delete(f"/api/projects/{p['id']}", headers=H)
    assert res.status_code == 200, res.text
    assert not folder.exists()
    assert p["id"] not in [x["id"] for x in client.get("/api/projects", headers=H).json()["projects"]]
    assert client.delete(f"/api/projects/{p['id']}", headers=H).status_code == 404
    with db.read() as u:
        assert u.projects.find(p["id"]) is None                          # hidden everywhere...
        row = u.projects.find(p["id"], include_archived=True)            # ...but kept, with its name and the date
        assert row.name == "Alpha" and row.archived_at
        entries = u.journal.list(project_id=p["id"])
        assert [e.title for e in entries] == ["hi"]                      # the journal is whole and still points at it
        assert u.decisions.get(decision.id).project_id == p["id"]
        assert u.approvals.list_for("decision", decision.id)
        assert u.tasks.get(task.id).project_id is None and u.conversations.get(conv.id).project_id is None
    again = make(client, "Alpha")                                        # the name and the folder can be used again
    assert again["id"] != p["id"] and again["slug"] == p["slug"]


def test_the_journal_tool_names_the_deleted_project(client, db):
    from hokma.kit import hokma_tools
    p = make(client, "Alpha")
    with db.write() as u:
        u.journal.add("note", "hi", actor=SYSTEM, outcome="info", project_id=p["id"])
    client.delete(f"/api/projects/{p['id']}", headers=H)
    with db.read() as u:
        out = json.loads(hokma_tools.journal_search(u, None, {"query": "hi"}))
    assert out[0]["project"] == "Alpha" and out[0]["project_deleted"] is True


def test_requires_token(client):
    p = make(client, "Alpha")
    assert client.delete(f"/api/projects/{p['id']}").status_code == 401

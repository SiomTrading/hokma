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

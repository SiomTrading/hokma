"""Project documents on every platform (SPEC-009 P9): paths with spaces and accents, and removing a file just read.

On Windows a file that something still holds open cannot be deleted. Each reader must close what it opens, so the
original can be removed right after it was indexed and searched.
"""
from pathlib import Path

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from docs_support import make_docx, make_pdf, make_xlsx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hokma.docs.api import router as docs_router
from hokma.projects.api import router as projects_router
from hokma.rpc.ws import SESSION_TOKEN

H = {"X-Hokma-Token": SESSION_TOKEN}
ODD_DIR = ("Mis datos", "an\u00e1lisis \u00f1and\u00fa", "folder with spaces")
TEXT = "momentum con rotacion sectorial"


@pytest.fixture
def spaced(env, monkeypatch):  # noqa: F811
    """The data folder moves to a path with spaces and accents before the database is created."""
    folder = env.joinpath(*ODD_DIR)
    folder.mkdir(parents=True)
    monkeypatch.setenv("HOKMA_DATA_DIR", str(folder))
    return folder


@pytest.fixture
def client(spaced, db, monkeypatch):  # noqa: F811
    monkeypatch.setenv("HOKMA_LANG", "es")
    with db.write() as u:
        u.profile.save(owner_name="Ana", fund_name="Fondo Prueba")
    app = FastAPI()
    app.include_router(projects_router)
    app.include_router(docs_router)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def project(client):
    return client.post("/api/projects", headers=H, json={"name": "Alpha", "kind": "strategy"}).json()["id"]


def upload(client, project_id, name, data):
    return client.post(f"/api/projects/{project_id}/documents", headers=H,
                       files={"file": (name, data, "application/octet-stream")})


CASES = [
    ("txt", "informe trimestral \u00f1and\u00fa.txt", lambda: TEXT.encode()),
    ("pdf", "an\u00e1lisis de riesgo.pdf", lambda: make_pdf([TEXT])),
    ("xlsx", "hoja de c\u00e1lculo.xlsx", lambda: make_xlsx({"Datos": [("momentum", "rotacion sectorial")]})),
    ("docx", "memoria t\u00e9cnica.docx", lambda: make_docx([(None, TEXT)])),
]


@pytest.mark.parametrize("kind,name,build", CASES, ids=[c[0] for c in CASES])
def test_a_file_just_read_can_be_removed_and_its_path_has_spaces_and_accents(client, project, db, spaced, kind, name,
                                                                            build):
    doc = upload(client, project, name, build()).json()
    assert doc["status"] == "ready", doc
    with db.read() as u:
        stored = Path(u.documents.get_document(doc["id"]).stored_path)
        library = u.documents.find_project_library(project)
        assert u.documents.search([library.id], "momentum")
    assert stored.is_file()
    assert " " in str(stored) and "\u00f1" in str(stored) and str(spaced) in str(stored)
    res = client.delete(f"/api/projects/{project}/documents/{doc['id']}", headers=H)
    assert res.status_code == 200 and res.json() == {"removed": True, "file_removed": True}
    assert not stored.exists()

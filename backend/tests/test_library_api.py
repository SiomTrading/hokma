"""Library document endpoints (SPEC-010 P3): list, upload, remove, move, discover and fetch on /api/library/documents,
plus the 0.0.26 per-project aliases answering exactly the same."""
from pathlib import Path

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hokma.docs import api as docs_api
from hokma.docs import ingest, web
from hokma.docs.api import router as docs_router
from hokma.i18n import t
from hokma.projects.api import router as projects_router
from hokma.rpc.ws import SESSION_TOKEN

H = {"X-Hokma-Token": SESSION_TOKEN}
BASE = "/api/library/documents"


@pytest.fixture
def client(db, monkeypatch):
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


@pytest.fixture
def folders(db):
    """papers (top) > 2026 (inside papers), and notes (top)."""
    with db.write() as u:
        lib = u.library.ensure_student_library()
        papers = u.library.create_folder(lib.id, "Papers")
        year = u.library.create_folder(lib.id, "2026", parent_id=papers.id)
        notes = u.library.create_folder(lib.id, "Notes")
    return {"papers": papers.id, "year": year.id, "notes": notes.id}


def upload(client, name, data, *, folder_id=None, headers=H):
    params = {"folder_id": folder_id} if folder_id is not None else None
    return client.post(BASE, headers=headers, params=params, files={"file": (name, data, "application/octet-stream")})


def names(client, **params):
    return [d["name"] for d in client.get(BASE, headers=H, params=params).json()["documents"]]


def test_the_empty_library_lists_nothing_and_exists_afterwards(client, db):
    assert client.get(BASE, headers=H).json() == {"documents": []}
    with db.read() as u:
        assert u.library.find_student_library() is not None


def test_upload_goes_to_the_student_library_at_the_top_level(client, db, env):
    res = upload(client, "a.txt", b"alpha text")
    assert res.status_code == 201
    doc = res.json()
    assert doc["status"] == "ready" and doc["library_folder_id"] is None and doc["library_path"] == ""
    assert doc["origin_project_id"] is None and doc["origin_project"] is None
    assert "stored_path" not in doc and "sha256" not in doc
    with db.read() as u:
        library = u.library.find_student_library()
        row = u.documents.get_document(doc["id"])
    assert row.library_id == library.id
    assert Path(row.stored_path).parent == env / "data" / "libraries" / str(library.id)


def test_upload_into_a_folder_reports_its_path_and_the_listing_filters_one_level(client, folders):
    top = upload(client, "top.txt", b"one").json()
    inside = upload(client, "inside.txt", b"two", folder_id=folders["papers"]).json()
    deep = upload(client, "deep.txt", b"three", folder_id=folders["year"]).json()
    assert (top["library_path"], inside["library_path"], deep["library_path"]) == ("", "Papers", "Papers/2026")
    assert deep["library_folder_id"] == folders["year"]
    assert names(client) == ["top.txt", "inside.txt", "deep.txt"]
    assert names(client, folder_id=folders["papers"]) == ["inside.txt"]
    assert names(client, folder_id=folders["year"]) == ["deep.txt"]
    assert names(client, folder_id=folders["notes"]) == []


def test_an_unknown_folder_is_a_404_for_list_and_upload_and_nothing_is_stored(client, db, env):
    assert client.get(BASE, headers=H, params={"folder_id": 999}).status_code == 404
    res = upload(client, "a.txt", b"x", folder_id=999)
    assert res.status_code == 404
    assert res.json()["detail"] == {"code": "lib_folder_not_found", "message": t("docs.err.folder_not_found")}
    assert names(client) == []
    assert not (env / "data" / "libraries").exists() or not any((env / "data" / "libraries").rglob("*.txt"))


def test_the_same_content_is_a_409_anywhere_in_the_library(client, folders):
    assert upload(client, "a.txt", b"same content").status_code == 201
    again = upload(client, "copy.txt", b"same content", folder_id=folders["notes"])
    assert again.status_code == 409
    assert again.json()["detail"] == {"code": "doc_duplicate", "message": t("docs.err.duplicate")}
    assert names(client) == ["a.txt"]


def test_unsupported_413_empty_and_no_file_keep_the_statuses_of_0_0_26(client, monkeypatch):
    assert upload(client, "tool.exe", b"MZ....").status_code == 415
    assert upload(client, "a.txt", b"").status_code == 400
    assert client.post(BASE, headers=H, data={"description": "only text"}).status_code == 400
    monkeypatch.setattr(ingest, "MAX_DOCUMENT_BYTES", 10)
    monkeypatch.setattr(docs_api, "MAX_UPLOAD_BODY", 10 + 256 * 1024)
    assert upload(client, "big.txt", b"x" * 11).status_code == 413
    assert names(client) == []


def test_remove_deletes_rows_and_the_stored_file(client, db):
    doc = upload(client, "a.txt", b"material de referencia").json()
    with db.read() as u:
        stored = Path(u.documents.get_document(doc["id"]).stored_path)
    assert stored.is_file()
    res = client.delete(f"{BASE}/{doc['id']}", headers=H)
    assert res.status_code == 200 and res.json() == {"removed": True, "file_removed": True}
    assert not stored.exists() and names(client) == []
    assert client.delete(f"{BASE}/{doc['id']}", headers=H).status_code == 404


def test_a_document_outside_the_student_library_is_a_404_for_remove_and_move(client, db, project):
    with db.write() as u:
        library = u.documents.create_project_library(project)
        old = u.documents.add_document(library.id, name="old.txt", stored_path="x/old.txt", mime_type="text/plain",
                                       format="txt", size_bytes=3, sha256="a" * 64)
    assert client.delete(f"{BASE}/{old.id}", headers=H).status_code == 404
    assert client.patch(f"{BASE}/{old.id}", headers=H, json={"library_folder_id": None}).status_code == 404
    with db.read() as u:
        assert u.documents.get_document(old.id).library_folder_id is None


def test_move_to_a_folder_back_to_the_top_and_into_a_subfolder(client, folders):
    doc = upload(client, "a.txt", b"movable").json()
    into = client.patch(f"{BASE}/{doc['id']}", headers=H, json={"library_folder_id": folders["year"]})
    assert into.status_code == 200
    assert into.json()["library_folder_id"] == folders["year"] and into.json()["library_path"] == "Papers/2026"
    assert names(client, folder_id=folders["year"]) == ["a.txt"]
    back = client.patch(f"{BASE}/{doc['id']}", headers=H, json={"library_folder_id": None})
    assert back.json()["library_folder_id"] is None and back.json()["library_path"] == ""
    assert names(client, folder_id=folders["year"]) == []


def test_moving_to_a_folder_that_does_not_exist_is_a_404_and_the_document_stays(client, folders):
    doc = upload(client, "a.txt", b"stays", folder_id=folders["notes"]).json()
    res = client.patch(f"{BASE}/{doc['id']}", headers=H, json={"library_folder_id": 999})
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "lib_folder_not_found"
    assert names(client, folder_id=folders["notes"]) == ["a.txt"]
    assert client.patch(f"{BASE}/999", headers=H, json={"library_folder_id": None}).status_code == 404


def test_moving_where_a_document_has_the_same_name_is_a_409_and_the_same_place_is_fine(client, folders):
    upload(client, "Report.txt", b"first", folder_id=folders["papers"])
    clash = upload(client, "report.TXT", b"second").json()
    res = client.patch(f"{BASE}/{clash['id']}", headers=H, json={"library_folder_id": folders["papers"]})
    assert res.status_code == 409
    assert res.json()["detail"] == {"code": "doc_name_taken", "message": t("docs.err.name_taken")}
    assert names(client, folder_id=folders["papers"]) == ["Report.txt"]
    same = client.patch(f"{BASE}/{clash['id']}", headers=H, json={"library_folder_id": None})
    assert same.status_code == 200


def test_the_move_body_needs_the_field(client):
    doc = upload(client, "a.txt", b"x").json()
    assert client.patch(f"{BASE}/{doc['id']}", headers=H, json={}).status_code == 422


def test_documents_of_a_removed_folder_leave_the_listing(client, db, folders):
    upload(client, "in.txt", b"inside", folder_id=folders["papers"])
    upload(client, "out.txt", b"outside")
    with db.write() as u:
        u.library.remove_folder(folders["papers"])
    assert names(client) == ["out.txt"]


def test_every_route_needs_the_token(client, folders):
    doc = upload(client, "a.txt", b"x").json()
    assert client.get(BASE).status_code == 401
    assert client.post(BASE, files={"file": ("a.txt", b"x")}).status_code == 401
    assert client.delete(f"{BASE}/{doc['id']}").status_code == 401
    assert client.patch(f"{BASE}/{doc['id']}", json={"library_folder_id": None}).status_code == 401
    assert client.post(f"{BASE}/discover", json={"url": "https://x.example/"}).status_code == 401
    assert client.post(f"{BASE}/fetch", json={"url": "https://x.example/"}).status_code == 401


# ------------------------------------------------------------------------------------------ discover and fetch
def test_discover_returns_what_the_service_finds(client, monkeypatch):
    seen = []
    monkeypatch.setattr(web, "discover", lambda link: seen.append(link) or {"kind": "page", "links": []})
    res = client.post(f"{BASE}/discover", headers=H, json={"url": "https://papers.example.org/"})
    assert res.status_code == 200 and res.json() == {"kind": "page", "links": []}
    assert seen == ["https://papers.example.org/"]
    assert client.post(f"{BASE}/discover", headers=H, json={}).status_code == 422


def test_fetch_stores_the_download_as_a_link_document_in_the_chosen_folder(client, folders, monkeypatch):
    monkeypatch.setattr(web, "download", lambda link, title=None, **kw: (
        f"{title or 'page'}.txt", b"downloaded text", "https://papers.example.org/final"))
    res = client.post(f"{BASE}/fetch", headers=H, json={"url": "https://papers.example.org/a", "title": "Paper",
                                                       "folder_id": folders["papers"]})
    assert res.status_code == 201
    doc = res.json()
    assert doc["name"] == "Paper.txt" and doc["origin"] == "link" and doc["source_url"] == "https://papers.example.org/final"
    assert doc["library_path"] == "Papers" and doc["origin_project_id"] is None
    again = client.post(f"{BASE}/fetch", headers=H, json={"url": "https://papers.example.org/a", "title": "Other"})
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "doc_duplicate"


def test_fetch_into_an_unknown_folder_is_a_404(client, monkeypatch):
    monkeypatch.setattr(web, "download", lambda link, title=None, **kw: ("a.txt", b"text", link))
    res = client.post(f"{BASE}/fetch", headers=H, json={"url": "https://papers.example.org/a", "folder_id": 999})
    assert res.status_code == 404 and res.json()["detail"]["code"] == "lib_folder_not_found"


# ------------------------------------------------------------------------------------------ 0.0.26 aliases
def alias(project_id, tail=""):
    return f"/api/projects/{project_id}/documents{tail}"


def test_aliases_answer_like_the_library_routes_and_record_the_origin(client, project, folders, monkeypatch):
    up = client.post(alias(project), headers=H, files={"file": ("a.txt", b"via alias", "text/plain")})
    assert up.status_code == 201
    doc = up.json()
    assert doc["origin_project_id"] == project and doc["origin_project"] == "Alpha" and doc["library_path"] == ""
    assert client.get(alias(project), headers=H).json() == client.get(BASE, headers=H).json()
    inside = client.post(alias(project), headers=H, params={"folder_id": folders["notes"]},
                         files={"file": ("b.txt", b"second", "text/plain")}).json()
    assert client.get(alias(project), headers=H, params={"folder_id": folders["notes"]}).json() == \
        client.get(BASE, headers=H, params={"folder_id": folders["notes"]}).json()
    assert inside["library_path"] == "Notes"

    monkeypatch.setattr(web, "discover", lambda link: {"kind": "page", "links": []})
    assert client.post(alias(project, "/discover"), headers=H, json={"url": "https://x.example/"}).json() == \
        client.post(f"{BASE}/discover", headers=H, json={"url": "https://x.example/"}).json()
    monkeypatch.setattr(web, "download", lambda link, title=None, **kw: ("c.txt", b"from link", link))
    fetched = client.post(alias(project, "/fetch"), headers=H, json={"url": "https://x.example/c"})
    assert fetched.status_code == 201 and fetched.json()["origin_project"] == "Alpha"

    assert client.delete(alias(project, f"/{doc['id']}"), headers=H).json() == {"removed": True, "file_removed": True}
    assert "a.txt" not in names(client)


def test_aliases_of_an_unknown_project_are_a_404_and_the_token_is_required(client, project):
    assert client.get(alias(9999), headers=H).status_code == 404
    assert client.post(alias(9999), headers=H, files={"file": ("a.txt", b"x")}).status_code == 404
    assert client.delete(alias(9999, "/1"), headers=H).status_code == 404
    assert client.post(alias(9999, "/discover"), headers=H, json={"url": "https://x.example/"}).status_code == 404
    assert client.post(alias(9999, "/fetch"), headers=H, json={"url": "https://x.example/"}).status_code == 404
    assert client.get(alias(project)).status_code == 401
    assert names(client) == []


def test_aliases_give_the_same_error_codes_as_the_library_routes(client, project):
    assert client.post(alias(project), headers=H, files={"file": ("a.txt", b"dup")}).status_code == 201
    assert client.post(alias(project), headers=H, files={"file": ("b.txt", b"dup")}).status_code == 409
    assert upload(client, "c.txt", b"dup").status_code == 409
    assert client.post(alias(project), headers=H, files={"file": ("x.exe", b"MZ")}).status_code == 415
    assert client.post(alias(project), headers=H, files={"file": ("e.txt", b"")}).status_code == 400
    assert client.delete(alias(project, "/999"), headers=H).status_code == 404

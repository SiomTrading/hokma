"""Library folder endpoints (SPEC-010 P4): level listing, create, rename and remove with confirmation."""
from pathlib import Path

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hokma.docs.api import router as docs_router
from hokma.i18n import t
from hokma.projects.api import router as projects_router
from hokma.rpc.ws import SESSION_TOKEN

H = {"X-Hokma-Token": SESSION_TOKEN}
FOLDERS = "/api/library/folders"
DOCS = "/api/library/documents"


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


def make(client, name, parent_id=None):
    return client.post(FOLDERS, headers=H, json={"name": name, "parent_id": parent_id})


def names(client, parent_id=None):
    params = {"parent_id": parent_id} if parent_id is not None else None
    return [f["name"] for f in client.get(FOLDERS, headers=H, params=params).json()["folders"]]


def upload(client, name, data, folder_id=None):
    params = {"folder_id": folder_id} if folder_id is not None else None
    return client.post(DOCS, headers=H, params=params, files={"file": (name, data, "text/plain")})


def test_the_token_is_required(client):
    assert client.get(FOLDERS).status_code == 401
    assert client.post(FOLDERS, json={"name": "A"}).status_code == 401


def test_the_empty_library_has_no_folders(client):
    assert client.get(FOLDERS, headers=H).json() == {"folders": [], "path": []}


def test_create_three_levels_and_list_each_level_with_its_path(client):
    papers = make(client, "Papers")
    assert papers.status_code == 201
    body = papers.json()
    assert (body["name"], body["parent_id"], body["path"], body["documents"], body["subfolders"]) == (
        "Papers", None, "Papers", 0, 0)
    year = make(client, "2026", papers.json()["id"]).json()
    month = make(client, "Marzo", year["id"]).json()
    assert month["path"] == "Papers/2026/Marzo"
    assert names(client) == ["Papers"]
    assert names(client, papers.json()["id"]) == ["2026"]
    level = client.get(FOLDERS, headers=H, params={"parent_id": year["id"]}).json()
    assert [f["name"] for f in level["folders"]] == ["Marzo"]
    assert level["path"] == [{"id": papers.json()["id"], "name": "Papers"}, {"id": year["id"], "name": "2026"}]
    assert client.get(FOLDERS, headers=H).json()["folders"][0]["subfolders"] == 2


def test_listing_is_sorted_by_name_without_regard_to_case(client):
    for name in ("beta", "Alfa", "gamma"):
        make(client, name)
    assert names(client) == ["Alfa", "beta", "gamma"]


def test_listing_a_missing_parent_is_a_404(client):
    response = client.get(FOLDERS, headers=H, params={"parent_id": 999})
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "lib_folder_not_found"


def test_create_under_a_missing_parent_is_a_404(client):
    response = make(client, "Orphan", 999)
    assert response.status_code == 404
    assert response.json()["detail"]["message"] == t("docs.err.folder_not_found")


def test_a_name_used_by_a_sibling_is_a_409_without_regard_to_case(client):
    make(client, "Papers")
    response = make(client, "PAPERS")
    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "duplicate_name", "message": t("docs.err.folder_taken")}
    assert names(client) == ["Papers"]


def test_the_same_name_is_fine_under_another_parent(client):
    a = make(client, "A").json()["id"]
    b = make(client, "B").json()["id"]
    assert make(client, "2026", a).status_code == 201
    assert make(client, "2026", b).status_code == 201


@pytest.mark.parametrize("bad", ["   ", "a/b", "a\\b", "x" * 81])
def test_a_bad_name_is_a_400(client, bad):
    response = make(client, bad)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "folder_bad_name"


def test_depth_over_eight_is_rejected_and_eight_is_accepted(client):
    parent = None
    for level in range(1, 9):
        response = make(client, f"L{level}", parent)
        assert response.status_code == 201, level
        parent = response.json()["id"]
    response = make(client, "L9", parent)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "folder_too_deep"
    assert names(client, parent) == []


def test_rename_keeps_the_place_and_updates_the_path(client):
    papers = make(client, "Papers").json()["id"]
    year = make(client, "2026", papers).json()["id"]
    renamed = client.patch(f"{FOLDERS}/{papers}", headers=H, json={"name": "Articulos"})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Articulos"
    child = client.get(FOLDERS, headers=H, params={"parent_id": papers}).json()["folders"][0]
    assert (child["id"], child["path"]) == (year, "Articulos/2026")


def test_rename_to_a_sibling_name_is_a_409_but_to_its_own_name_with_other_case_is_fine(client):
    make(client, "Alfa")
    beta = make(client, "Beta").json()["id"]
    clash = client.patch(f"{FOLDERS}/{beta}", headers=H, json={"name": "alfa"})
    assert clash.status_code == 409
    assert clash.json()["detail"]["code"] == "duplicate_name"
    assert client.patch(f"{FOLDERS}/{beta}", headers=H, json={"name": "BETA"}).status_code == 200
    assert "BETA" in names(client)


def test_rename_errors(client):
    assert client.patch(f"{FOLDERS}/999", headers=H, json={"name": "X"}).status_code == 404
    folder = make(client, "Alfa").json()["id"]
    assert client.patch(f"{FOLDERS}/{folder}", headers=H, json={"name": "a/b"}).status_code == 400


def test_remove_an_empty_folder_needs_no_confirmation(client):
    folder = make(client, "Empty").json()["id"]
    response = client.delete(f"{FOLDERS}/{folder}", headers=H)
    assert response.status_code == 200
    assert response.json()["removed"] is True
    assert names(client) == []


def test_remove_a_missing_folder_is_a_404(client):
    assert client.delete(f"{FOLDERS}/999", headers=H).status_code == 404


def test_remove_with_contents_without_confirm_is_a_409_with_counts_and_changes_nothing(client):
    papers = make(client, "Papers").json()["id"]
    year = make(client, "2026", papers).json()["id"]
    assert upload(client, "a.txt", b"alpha text", papers).status_code == 201
    assert upload(client, "b.txt", b"beta text", year).status_code == 201
    response = client.delete(f"{FOLDERS}/{papers}", headers=H)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert (detail["code"], detail["documents"], detail["subfolders"]) == ("folder_not_empty", 2, 1)
    assert detail["message"] == t("docs.err.folder_not_empty", documents=2, subfolders=1)
    assert "2 documentos y 1 subcarpetas" in detail["message"]
    assert names(client) == ["Papers"]
    assert len(client.get(DOCS, headers=H).json()["documents"]) == 2


def test_a_folder_with_only_subfolders_also_asks_for_confirmation(client):
    papers = make(client, "Papers").json()["id"]
    make(client, "2026", papers)
    detail = client.delete(f"{FOLDERS}/{papers}", headers=H).json()["detail"]
    assert (detail["documents"], detail["subfolders"]) == (0, 1)


def test_remove_with_confirm_deletes_the_tree_its_documents_and_their_files(client, db):
    papers = make(client, "Papers").json()["id"]
    year = make(client, "2026", papers).json()["id"]
    keep = make(client, "Keep").json()["id"]
    inside = upload(client, "a.txt", b"alpha text", year).json()
    outside = upload(client, "k.txt", b"keep text", keep).json()
    top = upload(client, "t.txt", b"top level text").json()
    with db.read() as u:
        stored_inside = Path(u.documents.get_document(inside["id"]).stored_path)
        stored_outside = Path(u.documents.get_document(outside["id"]).stored_path)
    assert stored_inside.exists()
    response = client.delete(f"{FOLDERS}/{papers}", headers=H, params={"confirm": "true"})
    assert response.status_code == 200
    assert response.json() == {"removed": True, "documents": 1, "subfolders": 1, "files_removed": 1}
    assert not stored_inside.exists()
    assert stored_outside.exists()
    assert names(client) == ["Keep"]
    remaining = {d["id"] for d in client.get(DOCS, headers=H).json()["documents"]}
    assert remaining == {outside["id"], top["id"]}
    with db.read() as u:
        assert u.library.descendant_ids(keep) == [keep]


def test_confirm_false_is_the_same_as_no_confirm(client):
    papers = make(client, "Papers").json()["id"]
    make(client, "2026", papers)
    assert client.delete(f"{FOLDERS}/{papers}", headers=H, params={"confirm": "false"}).status_code == 409


def test_a_folder_moved_documents_follow_the_listing_by_level(client):
    papers = make(client, "Papers").json()["id"]
    doc = upload(client, "a.txt", b"alpha text").json()
    moved = client.patch(f"{DOCS}/{doc['id']}", headers=H, json={"library_folder_id": papers})
    assert moved.status_code == 200
    folder = client.get(FOLDERS, headers=H).json()["folders"][0]
    assert folder["documents"] == 1

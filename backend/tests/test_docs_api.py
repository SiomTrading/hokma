"""The 0.0.26 per-project document routes, now aliases of the Library (SPEC-010 P3): upload, list and remove, with the
statuses 400, 401, 404, 409, 413 and 415. The Library routes themselves are in test_library_api.py."""
import os
from pathlib import Path

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from docs_support import make_docx, make_encrypted_pdf, make_pdf, make_png, make_xlsx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hokma.docs import api as docs_api
from hokma.docs import ingest
from hokma.docs.api import router as docs_router
from hokma.i18n import t
from hokma.projects.api import router as projects_router
from hokma.rpc.ws import SESSION_TOKEN

H = {"X-Hokma-Token": SESSION_TOKEN}
FIXTURES = Path(__file__).parent / "fixtures" / "docs"


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


def upload(client, project_id, name, data, description=None, mime="application/octet-stream"):
    fields = {"description": description} if description is not None else None
    return client.post(f"/api/projects/{project_id}/documents", headers=H, files={"file": (name, data, mime)},
                       data=fields)


def url(project_id, doc_id=None):
    base = f"/api/projects/{project_id}/documents"
    return base if doc_id is None else f"{base}/{doc_id}"


def chunk_count(db, doc_id):
    with db.read() as u:
        return u.session.scalar("SELECT count(*) FROM doc_chunks WHERE document_id = :d", {"d": doc_id})


def test_empty_project_lists_nothing(client, project):
    got = client.get(url(project), headers=H)
    assert got.status_code == 200 and got.json() == {"documents": []}


def test_upload_stores_the_original_in_the_library_path_and_indexes_it(client, project, db, env):
    data = (FIXTURES / "sample.md").read_bytes()
    res = upload(client, project, "caf\u00e9 report.md", data)
    assert res.status_code == 201
    doc = res.json()
    assert doc["name"] == "caf\u00e9 report.md" and doc["format"] == "md" and doc["status"] == "ready"
    assert doc["chunk_count"] > 0 and doc["origin"] == "file" and doc["size_bytes"] == len(data)
    assert "stored_path" not in doc and "sha256" not in doc

    with db.read() as u:
        row = u.documents.get_document(doc["id"])
        library = u.library.find_student_library()
    stored = Path(row.stored_path)
    assert stored.read_bytes() == data
    assert stored.parent == env / "data" / "libraries" / str(library.id)
    assert stored.name.startswith(f"{row.id}-")
    assert len(row.sha256) == 64 and row.indexed_at
    assert chunk_count(db, doc["id"]) == doc["chunk_count"]


def test_uploaded_text_can_be_found_by_the_search(client, project, db):
    res = upload(client, project, "notas.txt", "El filtro de volatilidad descarta las sesiones planas.".encode())
    assert res.status_code == 201
    with db.read() as u:
        library = u.library.find_student_library()
        hits = u.documents.search([library.id], "VOLATILIDAD sesiones")
    assert [h.document for h in hits] == ["notas.txt"]


def test_list_shows_every_document_with_its_status(client, project):
    upload(client, project, "a.txt", b"alpha text")
    upload(client, project, "b.csv", b"x,y\n1,2\n")
    docs = client.get(url(project), headers=H).json()["documents"]
    assert [d["name"] for d in docs] == ["a.txt", "b.csv"]
    assert {d["status"] for d in docs} == {"ready"} and all(d["chunk_count"] >= 1 for d in docs)


def test_the_same_content_is_a_409_even_with_another_name(client, project, db):
    first = upload(client, project, "a.txt", b"same content")
    assert first.status_code == 201
    again = upload(client, project, "copy.txt", b"same content")
    assert again.status_code == 409
    assert again.json()["detail"] == {"code": "doc_duplicate", "message": t("docs.err.duplicate")}
    assert len(client.get(url(project), headers=H).json()["documents"]) == 1


def test_the_same_content_from_two_projects_is_one_document_in_the_library(client, project):
    other = client.post("/api/projects", headers=H, json={"name": "Beta", "kind": "other"}).json()["id"]
    assert upload(client, project, "a.txt", b"shared").status_code == 201
    assert upload(client, other, "a.txt", b"shared").status_code == 409


def test_unsupported_format_is_a_415_and_nothing_is_stored(client, project, env):
    res = upload(client, project, "program.exe", b"MZ\x90\x00")
    assert res.status_code == 415
    assert res.json()["detail"] == {"code": "doc_unsupported", "message": t("docs.err.unsupported")}
    assert client.get(url(project), headers=H).json() == {"documents": []}
    assert not (env / "data" / "libraries").exists()


def test_a_file_over_50_mb_is_a_413(client, project, monkeypatch):
    monkeypatch.setattr(ingest, "MAX_DOCUMENT_BYTES", 1024)
    monkeypatch.setattr(docs_api, "MAX_UPLOAD_BODY", 64 * 1024)
    ok = upload(client, project, "small.txt", b"a" * 1024)
    assert ok.status_code == 201
    big = upload(client, project, "big.txt", b"b" * 1025)
    assert big.status_code == 413
    assert big.json()["detail"] == {"code": "doc_too_large", "message": t("docs.err.too_large")}
    huge = upload(client, project, "huge.txt", b"c" * (200 * 1024))
    assert huge.status_code == 413
    assert len(client.get(url(project), headers=H).json()["documents"]) == 1


def test_an_empty_file_and_a_request_without_file_are_400(client, project):
    assert upload(client, project, "empty.txt", b"").status_code == 400
    res = client.post(url(project), headers=H, data={"description": "nothing"}, files={"other": (None, "x")})
    assert res.status_code == 400 and res.json()["detail"]["code"] == "no_file"
    raw = client.post(url(project), headers={**H, "content-type": "text/plain"}, content=b"hello")
    assert raw.status_code == 400 and raw.json()["detail"]["code"] == "no_file"


def test_unknown_project_is_a_404_and_the_token_is_required(client, project):
    assert upload(client, 9999, "a.txt", b"x").status_code == 404
    assert client.get(url(9999), headers=H).status_code == 404
    assert client.delete(url(9999, 1), headers=H).status_code == 404
    assert client.get(url(project)).status_code == 401
    assert client.post(url(project), files={"file": ("a.txt", b"x")}).status_code == 401
    assert client.delete(url(project, 1)).status_code == 401


def test_image_description_is_saved_and_searchable(client, project, db):
    res = upload(client, project, "chart.png", make_png(8, 6), description="  weekly   chart of the NQ  ")
    assert res.status_code == 201
    doc = res.json()
    assert doc["format"] == "image" and doc["description"] == "weekly chart of the NQ"
    with db.read() as u:
        library = u.library.find_student_library()
        hits = u.documents.search([library.id], "weekly")
    assert [h.document for h in hits] == ["chart.png"]


def test_office_and_pdf_formats_are_indexed_with_their_sections(client, project, db):
    docx = make_docx([("Heading1", "Riesgo"), (None, "El stop es de 12 puntos.")])
    xlsx = make_xlsx({"Resultados": [["mes", "pnl"], ["enero", 120]]})
    pdf = make_pdf(["primera pagina con texto buscable"])
    for name, data in (("plan.docx", docx), ("datos.xlsx", xlsx), ("paper.pdf", pdf)):
        assert upload(client, project, name, data).status_code == 201, name
    with db.read() as u:
        library = u.library.find_student_library()
        assert [h.section for h in u.documents.search([library.id], "stop")] == ["Riesgo"]
        assert u.documents.search([library.id], "enero")[0].section.startswith('sheet "Resultados"')
        assert u.documents.search([library.id], "buscable")[0].section == "page 1"


def test_a_pdf_without_text_is_ready_with_the_notice(client, project):
    doc = upload(client, project, "scan.pdf", make_pdf([""])).json()
    assert doc["status"] == "ready" and doc["status_detail"] == t("docs.detail.no_text")


def test_a_damaged_or_protected_file_is_kept_as_failed_with_its_reason(client, project, db):
    broken = upload(client, project, "broken.pdf", b"%PDF-1.4 this is not a pdf")
    assert broken.status_code == 201
    doc = broken.json()
    assert doc["status"] == "failed" and doc["status_detail"].startswith(t("docs.detail.unreadable", reason="").strip())
    assert doc["chunk_count"] == 0
    locked = upload(client, project, "locked.pdf", make_encrypted_pdf(["secret"], "pw")).json()
    assert locked["status"] == "failed" and locked["status_detail"]
    # the reason comes from the catalog in the student's language, never the English text of the reader
    assert locked["status_detail"] == t("docs.detail.unreadable", reason=t("docs.detail.reason.pdf_password"))
    assert doc["status_detail"] == t("docs.detail.unreadable", reason=t("docs.detail.reason.pdf_damaged"))
    with db.read() as u:
        assert Path(u.documents.get_document(doc["id"]).stored_path).is_file()
    assert client.delete(url(project, doc["id"]), headers=H).status_code == 200


def test_truncated_reading_is_reported_in_spanish(client, project, monkeypatch):
    real = ingest.read_document
    monkeypatch.setattr(ingest, "read_document", lambda path, **kw: real(path, max_passages=1, **kw))
    text = ("palabra " * 600 + "\n\n") * 4
    doc = upload(client, project, "largo.txt", text.encode()).json()
    assert doc["status"] == "ready" and doc["status_detail"] == t("docs.detail.limit_passages", reached=1)


def test_remove_deletes_rows_passages_and_the_stored_file(client, project, db):
    doc = upload(client, project, "a.txt", b"material de referencia").json()
    with db.read() as u:
        stored = Path(u.documents.get_document(doc["id"]).stored_path)
    assert stored.is_file()
    res = client.delete(url(project, doc["id"]), headers=H)
    assert res.status_code == 200 and res.json() == {"removed": True, "file_removed": True}
    assert not stored.exists()
    assert client.get(url(project), headers=H).json() == {"documents": []}
    assert chunk_count(db, doc["id"]) == 0
    with db.read() as u:
        library = u.library.find_student_library()
        assert u.documents.search([library.id], "referencia") == []
    assert client.delete(url(project, doc["id"]), headers=H).status_code == 404


def test_removed_content_can_be_uploaded_again(client, project):
    first = upload(client, project, "a.txt", b"again").json()
    client.delete(url(project, first["id"]), headers=H)
    assert upload(client, project, "a.txt", b"again").status_code == 201


def test_every_project_sees_and_removes_the_same_library(client, project, db):
    other = client.post("/api/projects", headers=H, json={"name": "Beta", "kind": "other"}).json()["id"]
    doc = upload(client, project, "a.txt", b"shared by all").json()
    assert [d["id"] for d in client.get(url(other), headers=H).json()["documents"]] == [doc["id"]]
    assert doc["origin_project_id"] == project and doc["origin_project"] == "Alpha"
    assert client.delete(url(other, doc["id"]), headers=H).status_code == 200
    assert client.get(url(project), headers=H).json() == {"documents": []}


def test_remove_never_deletes_outside_the_libraries_folder(client, project, db, env):
    doc = upload(client, project, "a.txt", b"inside").json()
    outside = env / "outside.txt"
    outside.write_text("keep me", encoding="utf-8")
    with db.write() as u:
        u.session.execute("UPDATE documents SET stored_path = :p WHERE id = :d", {"p": str(outside), "d": doc["id"]})
    res = client.delete(url(project, doc["id"]), headers=H)
    assert res.status_code == 200 and res.json() == {"removed": True, "file_removed": False}
    assert outside.read_text(encoding="utf-8") == "keep me"


def test_a_missing_stored_file_does_not_block_the_removal(client, project, db):
    doc = upload(client, project, "a.txt", b"gone already").json()
    with db.read() as u:
        os.unlink(u.documents.get_document(doc["id"]).stored_path)
    res = client.delete(url(project, doc["id"]), headers=H)
    assert res.status_code == 200 and res.json()["removed"] is True


def test_names_with_paths_and_odd_characters_stay_inside_the_library(client, project, db, env):
    doc = upload(client, project, "..\\..\\evil/../report \u00f1and\u00fa (1).txt", b"contenido").json()
    assert doc["name"] == "report \u00f1and\u00fa (1).txt"
    with db.read() as u:
        stored = Path(u.documents.get_document(doc["id"]).stored_path)
    assert stored.parent.parent == env / "data" / "libraries" and stored.is_file()


def test_deleting_a_project_keeps_its_documents_and_clears_their_origin(client, project, db):
    doc = upload(client, project, "a.txt", b"stays in the library").json()
    with db.write() as u:
        u.session.execute("DELETE FROM projects WHERE id = :p", {"p": project})
    assert chunk_count(db, doc["id"]) > 0
    with db.read() as u:
        assert u.documents.get_document(doc["id"]).origin_project_id is None


def test_parse_upload_reads_file_and_description():
    boundary = "XBOUNDARY"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"description\"\r\n\r\nuna nota\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.txt\"\r\n"
            f"Content-Type: text/plain\r\n\r\nhola\r\n--{boundary}--\r\n").encode()
    parsed = docs_api.parse_upload(f"multipart/form-data; boundary={boundary}", body)
    assert parsed == ("a.txt", b"hola", "una nota")
    assert docs_api.parse_upload("application/json", b"{}") is None


def test_an_upload_over_the_passage_cap_is_kept_as_failed(client, project, monkeypatch):
    """Bug 1089: uploads respect the installation-wide passage cap, not only the disk folders."""
    from hokma.docs import folder_scan
    monkeypatch.setattr(folder_scan, "MAX_PASSAGES", 1)
    first = upload(client, project, "uno.md", b"# Uno\n\nTexto del primero.\n")
    assert first.status_code in (200, 201) and first.json()["status"] == "ready"
    second = upload(client, project, "dos.md", b"# Dos\n\nTexto del segundo.\n")
    body = second.json()
    assert body["status"] == "failed"
    assert body["status_detail"] == t("docs.disk.passage_cap", limit="1")


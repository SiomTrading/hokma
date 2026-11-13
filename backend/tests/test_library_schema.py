"""Schema of migration 0008 (SPEC-010 P1): one student Library, disk folders, Library folders, vector control table.

Postgres cases are skipped (with the reason) when HOKMA_TEST_POSTGRES_URL is not set, like test_docs_repo.py."""
import shutil
import sqlite3

import pytest
from dbsupport import env  # noqa: F401  (fixture)
from pgsupport import pg_admin_url, pg_template, pg_url, pg_url_migrated  # noqa: F401  (fixtures)

from hokma.db import IntegrityViolation, get_database, migrate
from hokma.db.dbconfig import DatabaseConfig, save_config
from hokma.db.migrate import MIGRATIONS_DIR
from hokma.db.paths import db_path

SHA_A, SHA_B = "a" * 64, "b" * 64


@pytest.fixture(params=["sqlite", "postgres"])
def database(request, env):  # noqa: F811
    if request.param == "postgres":
        save_config(DatabaseConfig("postgres", request.getfixturevalue("pg_url_migrated")))
    migrate()
    database = get_database()
    assert database.engine == request.param
    return database


def add(u, library_id, name, sha, chunks=(), **extra):
    return u.documents.add_document(
        library_id, name=name, stored_path=f"/data/libraries/{library_id}/{name}", mime_type="application/pdf",
        format="pdf", size_bytes=10, sha256=sha, chunks=chunks, **extra)


def test_0008_applies_on_a_0_0_26_database_without_touching_its_data(env):
    old = env / "migs-0007"
    old.mkdir()
    for path in MIGRATIONS_DIR.glob("*.sql"):
        if path.name < "0008":
            shutil.copy(path, old / path.name)
    assert migrate(migrations_dir=old).version == 7
    database = get_database()
    with database.write() as u:
        project = u.projects.create("Old", "/p/old")
        library = u.documents.create_project_library(project.id)
        # written by hand: the 0.0.26 shape, before the new columns exist
        doc_id = u.session.insert("documents", {
            "library_id": library.id, "name": "a.pdf", "stored_path": "/data/a.pdf", "mime_type": "application/pdf",
            "format": "pdf", "size_bytes": 10, "sha256": SHA_A, "status": "ready", "chunk_count": 1})
        u.session.execute("INSERT INTO doc_chunks (document_id, ord, section, body, search_text) "
                          "VALUES (:d, 0, 'p1', 'alpha beta gamma', 'alpha beta gamma')", {"d": doc_id})
    result = migrate()
    assert result.applied == [8, 9, 10, 11, 12, 13, 14] and result.version == 14 and result.backup is not None
    with database.read() as u:
        student = u.library.find_student_library()                 # the data step moved the document there
        assert u.documents.list_documents(library.id) == []
        docs = u.documents.list_documents(student.id)
        assert [d.name for d in docs] == ["a.pdf"] and docs[0].chunk_count == 1
        doc = docs[0]
        assert (doc.origin_project_id, doc.folder_id, doc.rel_path, doc.mtime_ns, doc.library_folder_id) == (
            project.id, None, None, None, None)
        assert u.documents.search([student.id], "gamma")[0].document == "a.pdf"
    con = sqlite3.connect(str(db_path()))
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    assert {"project_folders", "library_folders", "doc_chunk_vectors", "ux_libraries_student",
            "ux_libfolders_child", "ux_libfolders_root"} <= names


def test_two_calls_make_one_student_library(database):
    with database.write() as u:
        first = u.library.ensure_student_library()
    with database.write() as u:
        second = u.library.ensure_student_library()
    assert first.id == second.id and first.scope == "student" and first.project_id is None
    with database.read() as u:
        assert u.session.scalar("SELECT count(*) FROM libraries WHERE scope = 'student'") == 1


def test_the_database_refuses_a_second_student_library(database):
    with database.write() as u:
        u.library.ensure_student_library()
    with pytest.raises(IntegrityViolation):
        with database.write() as u:
            u.session.execute("INSERT INTO libraries (scope, project_id) VALUES ('student', NULL)")


def test_project_folders_are_unique_per_project_and_checked(database):
    with database.write() as u:
        p1, p2 = u.projects.create("One", "/p/one"), u.projects.create("Two", "/p/two")
        u.session.execute("INSERT INTO project_folders (project_id, path) VALUES (:p, '/data/a')", {"p": p1.id})
        u.session.execute("INSERT INTO project_folders (project_id, path) VALUES (:p, '/data/a')", {"p": p2.id})
        row = u.session.one("SELECT status, file_count FROM project_folders WHERE project_id = :p", {"p": p1.id})
        assert (row["status"], row["file_count"]) == ("scanning", 0)
    with pytest.raises(IntegrityViolation):
        with database.write() as u:
            u.session.execute("INSERT INTO project_folders (project_id, path) VALUES (:p, '/data/a')", {"p": p1.id})
    with pytest.raises(IntegrityViolation):
        with database.write() as u:
            u.session.execute("INSERT INTO project_folders (project_id, path, status) VALUES (:p, '/x', 'weird')",
                              {"p": p1.id})


def test_removing_a_project_cascades_to_its_disk_folders_and_their_documents(database):
    with database.write() as u:
        project = u.projects.create("Gone", "/p/gone")
        library = u.documents.create_project_library(project.id)
        folder = u.library.add_project_folder(project.id, "/data/papers")
        add(u, library.id, "p.pdf", SHA_A, [("s", "delta epsilon")], folder_id=folder.id, rel_path="p.pdf",
            mtime_ns=1_700_000_000_000_000_000)
        doc_id = u.session.scalar("SELECT id FROM documents WHERE folder_id = :f", {"f": folder.id})
        assert u.session.scalar("SELECT mtime_ns FROM documents WHERE id = :d", {"d": doc_id}) == 1_700_000_000_000_000_000
        u.library.remove_project_folder(folder.id)
        assert u.session.scalar("SELECT count(*) FROM documents") == 0
        assert u.session.scalar("SELECT count(*) FROM doc_chunks") == 0


def test_vector_control_rows_follow_their_passage(database):
    with database.write() as u:
        project = u.projects.create("V", "/p/v")
        library = u.documents.create_project_library(project.id)
        doc = add(u, library.id, "v.pdf", SHA_A, [("s", "one"), ("s", "two")])
        ids = [r["id"] for r in u.session.query("SELECT id FROM doc_chunks WHERE document_id = :d ORDER BY id",
                                                {"d": doc.id})]
        for chunk_id in ids:
            u.session.execute("INSERT INTO doc_chunk_vectors (chunk_id, model_id, dim) VALUES (:c, 'm', 384)",
                              {"c": chunk_id})
        assert u.session.scalar("SELECT count(*) FROM doc_chunk_vectors") == 2
        u.documents.remove_document(doc.id)
        assert u.session.scalar("SELECT count(*) FROM doc_chunk_vectors") == 0

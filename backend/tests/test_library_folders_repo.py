"""Folders of the student's Library and disk folders of a project (SPEC-010 P1), on SQLite and on Postgres.

Postgres cases are skipped (with the reason) when HOKMA_TEST_POSTGRES_URL is not set, like test_docs_repo.py."""
import pytest
from dbsupport import env  # noqa: F401  (fixture)
from pgsupport import pg_admin_url, pg_template, pg_url, pg_url_migrated  # noqa: F401  (fixtures)

from hokma.db import get_database, migrate
from hokma.db.dbconfig import DatabaseConfig, save_config
from hokma.db.errors import DuplicateName, NotFound, ValidationError
from hokma.db.repos.library import MAX_DEPTH, clean_folder_name

SHAS = ["%064x" % n for n in range(1, 10)]


@pytest.fixture(params=["sqlite", "postgres"])
def database(request, env):  # noqa: F811
    if request.param == "postgres":
        save_config(DatabaseConfig("postgres", request.getfixturevalue("pg_url_migrated")))
    migrate()
    database = get_database()
    assert database.engine == request.param
    return database


def add(u, library_id, name, sha, **extra):
    return u.documents.add_document(
        library_id, name=name, stored_path=f"/data/libraries/{library_id}/{name}", mime_type="application/pdf",
        format="pdf", size_bytes=10, sha256=sha, chunks=[("s", f"text of {name}")], **extra)


# ------------------------------------------------------------------------------------------ names
def test_folder_names_are_trimmed_and_checked():
    assert clean_folder_name("  Papers 2026 ") == "Papers 2026"
    for bad in ("", "   ", None, "a/b", "a\\b", "x" * 81):
        with pytest.raises(ValidationError):
            clean_folder_name(bad)
    assert clean_folder_name("x" * 80) == "x" * 80


# ------------------------------------------------------------------------------------------ tree
def test_folders_form_a_tree_with_a_path_and_breadcrumbs(database):
    with database.write() as u:
        lib = u.library.ensure_student_library()
        papers = u.library.create_folder(lib.id, "Papers")
        year = u.library.create_folder(lib.id, "2026", parent_id=papers.id)
        other = u.library.create_folder(lib.id, "Books")
        assert u.library.folder_path(year.id) == "Papers/2026" and u.library.folder_path(other.id) == "Books"
        assert [f.name for f in u.library.folder_chain(year.id)] == ["Papers", "2026"]
        assert u.library.descendant_ids(papers.id) == [papers.id, year.id]
        assert u.library.descendant_ids(year.id) == [year.id]
        assert [f.name for f in u.library.list_folders(lib.id)] == ["Papers", "2026", "Books"]
        assert u.library.get_folder(year.id).parent_id == papers.id and other.parent_id is None


def test_sibling_names_must_differ_also_at_the_top_level_and_ignoring_case(database):
    with database.write() as u:
        lib = u.library.ensure_student_library()
        papers = u.library.create_folder(lib.id, "Papers")
        for attempt in ("Papers", "papers", " PAPERS "):
            with pytest.raises(DuplicateName):
                u.library.create_folder(lib.id, attempt)
        u.library.create_folder(lib.id, "2026", parent_id=papers.id)
        with pytest.raises(DuplicateName):
            u.library.create_folder(lib.id, "2026", parent_id=papers.id)
        u.library.create_folder(lib.id, "2026")                    # same name under another parent is fine
        books = u.library.create_folder(lib.id, "Books")
        with pytest.raises(DuplicateName):
            u.library.rename_folder(books.id, "papers")
        assert u.library.rename_folder(books.id, "BOOKS").name == "BOOKS"      # renaming to itself in other case
        assert u.library.rename_folder(books.id, "Reading").name == "Reading"


def test_depth_is_limited_to_eight_levels(database):
    with database.write() as u:
        lib = u.library.ensure_student_library()
        parent = None
        for level in range(MAX_DEPTH):
            parent = u.library.create_folder(lib.id, f"L{level}", parent_id=parent.id if parent else None)
        with pytest.raises(ValidationError):
            u.library.create_folder(lib.id, "too deep", parent_id=parent.id)


def test_folders_only_exist_in_the_student_library_and_parents_must_match(database):
    with database.write() as u:
        project = u.projects.create("P", "/p/p")
        project_library = u.documents.create_project_library(project.id)
        with pytest.raises(ValidationError):
            u.library.create_folder(project_library.id, "Nope")
        lib = u.library.ensure_student_library()
        with pytest.raises(NotFound):
            u.library.create_folder(lib.id, "Orphan", parent_id=99999)
        with pytest.raises(NotFound):
            u.library.create_folder(99999, "Nowhere")
        with pytest.raises(NotFound):
            u.library.get_folder(99999)


# ------------------------------------------------------------------------------------------ documents
def test_a_document_moves_into_a_folder_and_back_to_the_top(database):
    with database.write() as u:
        lib = u.library.ensure_student_library()
        folder = u.library.create_folder(lib.id, "Papers")
        doc = add(u, lib.id, "a.pdf", SHAS[0])
        assert doc.library_folder_id is None
        assert u.library.move_document(doc.id, folder.id).library_folder_id == folder.id
        assert u.library.move_document(doc.id, None).library_folder_id is None
        with pytest.raises(NotFound):
            u.library.move_document(doc.id, 99999)
        with pytest.raises(NotFound):
            u.library.move_document(99999, folder.id)


def test_only_library_documents_move(database):
    with database.write() as u:
        project = u.projects.create("P", "/p/p")
        project_library = u.documents.create_project_library(project.id)
        doc = add(u, project_library.id, "a.pdf", SHAS[0])
        lib = u.library.ensure_student_library()
        folder = u.library.create_folder(lib.id, "Papers")
        with pytest.raises(ValidationError):
            u.library.move_document(doc.id, folder.id)


def test_removing_a_folder_deletes_subfolders_and_documents_and_reports_them(database):
    with database.write() as u:
        lib = u.library.ensure_student_library()
        papers = u.library.create_folder(lib.id, "Papers")
        year = u.library.create_folder(lib.id, "2026", parent_id=papers.id)
        keep = u.library.create_folder(lib.id, "Keep")
        d1 = add(u, lib.id, "one.pdf", SHAS[0], library_folder_id=papers.id)
        d2 = add(u, lib.id, "two.pdf", SHAS[1], library_folder_id=year.id)
        d3 = add(u, lib.id, "three.pdf", SHAS[2], library_folder_id=keep.id)
        d4 = add(u, lib.id, "top.pdf", SHAS[3])
        assert u.library.folder_contents(papers.id) == {"documents": 2, "folders": 1}
        assert u.library.folder_contents(keep.id) == {"documents": 1, "folders": 0}
        removed = u.library.remove_folder(papers.id)
        assert sorted(d.id for d in removed) == sorted([d1.id, d2.id])
        assert [f.name for f in u.library.list_folders(lib.id)] == ["Keep"]
        assert sorted(d.id for d in u.documents.list_documents(lib.id)) == sorted([d3.id, d4.id])
        assert u.session.scalar("SELECT count(*) FROM doc_chunks") == 2          # passages of the removed ones are gone
        assert u.documents.search([lib.id], "two") == []


def test_an_empty_folder_is_removed_without_documents(database):
    with database.write() as u:
        lib = u.library.ensure_student_library()
        folder = u.library.create_folder(lib.id, "Empty")
        assert u.library.folder_contents(folder.id) == {"documents": 0, "folders": 0}
        assert u.library.remove_folder(folder.id) == []
        with pytest.raises(NotFound):
            u.library.remove_folder(folder.id)


# ------------------------------------------------------------------------------------------ disk folders
def test_project_folders_register_report_status_and_go(database):
    with database.write() as u:
        project = u.projects.create("P", "/p/p")
        other = u.projects.create("Q", "/p/q")
        folder = u.library.add_project_folder(project.id, " /data/Papers ", label="Papers")
        assert (folder.path, folder.label, folder.status, folder.file_count) == ("/data/Papers", "Papers", "scanning", 0)
        with pytest.raises(DuplicateName):
            u.library.add_project_folder(project.id, "/data/Papers")
        u.library.add_project_folder(other.id, "/data/Papers")                    # another project may read it
        for bad in ("", "  "):
            with pytest.raises(ValidationError):
                u.library.add_project_folder(project.id, bad)
        with pytest.raises(NotFound):
            u.library.add_project_folder(99999, "/x")
        ready = u.library.set_project_folder_status(folder.id, "ready", file_count=12, scanned=True)
        assert (ready.status, ready.file_count) == ("ready", 12) and ready.last_scan_at
        missing = u.library.set_project_folder_status(folder.id, "missing", "folder not found")
        assert (missing.status, missing.status_detail, missing.file_count) == ("missing", "folder not found", 12)
        with pytest.raises(ValidationError):
            u.library.set_project_folder_status(folder.id, "weird")
        with pytest.raises(ValidationError):
            u.library.set_project_folder_status(folder.id, "ready", file_count=-1)
        assert [f.path for f in u.library.list_project_folders(project.id)] == ["/data/Papers"]
        assert u.library.remove_project_folder(folder.id).id == folder.id
        assert u.library.list_project_folders(project.id) == []
        assert len(u.library.list_project_folders(other.id)) == 1

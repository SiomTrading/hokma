"""Data step of migration 0008 (SPEC-010 P2): documents of the project libraries move to the student's Library.

Postgres cases are skipped (with the reason) when HOKMA_TEST_POSTGRES_URL is not set, like test_library_schema.py."""
import shutil

import pytest
from dbsupport import env  # noqa: F401  (fixture)
from pgsupport import pg_admin_url, pg_template, pg_url, pg_url_migrated  # noqa: F401  (fixtures)

from hokma.db import MigrationError, get_database, migrate
from hokma.db import datasteps
from hokma.db.dbconfig import DatabaseConfig, save_config
from hokma.db.migrate import MIGRATIONS_DIR, current_version
from hokma.docs.ingest import libraries_dir

SHA = {key: key * 64 for key in "abcd"}


@pytest.fixture(params=["sqlite", "postgres"])
def old_database(request, env, tmp_path):  # noqa: F811
    """A database at schema 7 (what 0.0.26 leaves), engine selected by the parameter."""
    if request.param == "postgres":
        save_config(DatabaseConfig("postgres", request.getfixturevalue("pg_url")))
    old = tmp_path / "migs-0007"
    old.mkdir()
    for path in MIGRATIONS_DIR.glob("*.sql"):
        if path.name < "0008":
            shutil.copy(path, old / path.name)
    assert migrate(migrations_dir=old).version == 7
    return get_database()


def put_document(u, project_id, name, sha, created, body="alpha beta"):
    """A document written the 0.0.26 way (no new columns) with a real file under the libraries folder."""
    library = u.documents.create_project_library(project_id)
    folder = libraries_dir() / str(library.id)
    folder.mkdir(parents=True, exist_ok=True)
    stored = folder / f"{name}"
    stored.write_text(body, encoding="utf-8")
    doc_id = u.session.insert("documents", {
        "library_id": library.id, "name": name, "stored_path": str(stored), "mime_type": "text/plain",
        "format": "txt", "size_bytes": 10, "sha256": sha, "status": "ready", "chunk_count": 2,
        "created_at": created})
    for order in range(2):
        u.session.execute("INSERT INTO doc_chunks (document_id, ord, section, body, search_text) "
                          "VALUES (:d, :o, 's', :b, :b)", {"d": doc_id, "o": order, "b": f"{name} part {order}"})
    return doc_id, stored


def counts(u):
    return (u.session.scalar("SELECT count(*) FROM documents"), u.session.scalar("SELECT count(*) FROM doc_chunks"))


def seed_three_projects(database, with_duplicates):
    ids = {}
    with database.write() as u:
        one, two, three = (u.projects.create(n, f"/p/{n}") for n in ("One", "Two", "Three"))
        ids["a"] = put_document(u, one.id, "a.txt", SHA["a"], "2026-01-01 00:00:00")
        ids["b"] = put_document(u, one.id, "b.txt", SHA["b"], "2026-01-02 00:00:00")
        ids["c"] = put_document(u, two.id, "c.txt", SHA["c"], "2026-01-03 00:00:00")
        ids["d"] = put_document(u, three.id, "d.txt", SHA["d"], "2026-01-04 00:00:00")
        if with_duplicates:
            ids["a2"] = put_document(u, two.id, "a-copy.txt", SHA["a"], "2026-02-01 00:00:00")
            ids["b2"] = put_document(u, three.id, "b-copy.txt", SHA["b"], "2025-12-01 00:00:00")   # older than b
        projects = (one.id, two.id, three.id)
    return ids, projects


def test_without_duplicates_nothing_is_lost(old_database):
    ids, (one, two, three) = seed_three_projects(old_database, with_duplicates=False)
    with old_database.read() as u:
        before = counts(u)
    assert migrate().version == 9
    with old_database.read() as u:
        assert counts(u) == before == (4, 8)
        library = u.library.find_student_library()
        rows = u.session.query("SELECT id, library_id, origin_project_id, library_folder_id, folder_id, description "
                               "FROM documents ORDER BY id")
        assert {r["library_id"] for r in rows} == {library.id}
        assert {r["library_folder_id"] for r in rows} == {None} and {r["folder_id"] for r in rows} == {None}
        assert {r["description"] for r in rows} == {None}
        assert {r["id"]: r["origin_project_id"] for r in rows} == {
            ids["a"][0]: one, ids["b"][0]: one, ids["c"][0]: two, ids["d"][0]: three}
        assert u.session.scalar("SELECT count(*) FROM documents d JOIN libraries l ON l.id = d.library_id "
                                "WHERE l.scope = 'project'") == 0
        # the search of the Library still finds what each project had
        assert u.documents.search([library.id], "part")
    assert all(path.exists() for _, path in ids.values())          # files never move


def test_duplicates_keep_the_oldest_and_drop_the_repeat_with_its_file(old_database):
    ids, (one, two, three) = seed_three_projects(old_database, with_duplicates=True)
    with old_database.read() as u:
        assert counts(u) == (6, 12)
    migrate()
    with old_database.read() as u:
        assert counts(u) == (4, 8)                                  # 6 - 2 exact duplicates, passages follow
        library = u.library.find_student_library()
        remaining = {r["id"]: r for r in u.session.query("SELECT * FROM documents")}
        assert set(remaining) == {ids["a"][0], ids["c"][0], ids["d"][0], ids["b2"][0]}
        assert u.session.scalar("SELECT count(*) FROM doc_chunks WHERE document_id IN (:x, :y)",
                                {"x": ids["a2"][0], "y": ids["b"][0]}) == 0
        assert remaining[ids["a"][0]]["description"] == "Also uploaded from Two"
        assert remaining[ids["b2"][0]]["description"] == "Also uploaded from One"
        assert remaining[ids["b2"][0]]["origin_project_id"] == three
        assert {r["library_id"] for r in remaining.values()} == {library.id}
    assert not ids["a2"][1].exists() and not ids["b"][1].exists()   # the repeated copies are gone from disk
    assert ids["a"][1].exists() and ids["b2"][1].exists() and ids["c"][1].exists() and ids["d"][1].exists()


def test_the_note_is_capped_to_the_description_limit(old_database):
    with old_database.write() as u:
        one, two = u.projects.create("One", "/p/1"), u.projects.create("Two", "/p/2")
        keep, _ = put_document(u, one.id, "k.txt", SHA["a"], "2026-01-01 00:00:00")
        u.session.execute("UPDATE documents SET description = :x WHERE id = :d", {"x": "d" * 490, "d": keep})
        put_document(u, two.id, "k2.txt", SHA["a"], "2026-01-02 00:00:00")
    migrate()
    with old_database.read() as u:
        description = u.session.scalar("SELECT description FROM documents WHERE id = :d", {"d": keep})
    assert len(description) == datasteps.MAX_DESCRIPTION_CHARS


def test_a_second_run_changes_nothing(old_database):
    ids, _ = seed_three_projects(old_database, with_duplicates=True)
    migrate()
    with old_database.read() as u:
        before = (counts(u), u.session.query("SELECT id, library_id, description FROM documents ORDER BY id"))
    assert migrate().applied == []                                  # the migration itself does not run twice
    with old_database.write() as u:
        assert datasteps.move_documents_to_student_library(u.session) == []   # the step alone is also a no-op
    with old_database.read() as u:
        after = (counts(u), u.session.query("SELECT id, library_id, description FROM documents ORDER BY id"))
        assert u.session.scalar("SELECT count(*) FROM libraries WHERE scope = 'student'") == 1
    assert before == after


def test_a_forced_failure_leaves_nothing_half_done(old_database, monkeypatch):
    ids, _ = seed_three_projects(old_database, with_duplicates=True)
    with old_database.read() as u:
        before = (counts(u), u.session.query("SELECT id, library_id, description FROM documents ORDER BY id"))

    real_step = datasteps.STEPS[8]

    def moves_then_breaks(session):
        datasteps.move_documents_to_student_library(session)        # all the changes are made...
        raise RuntimeError("forced failure")                        # ...and then the step fails

    monkeypatch.setitem(datasteps.STEPS, 8, moves_then_breaks)
    with pytest.raises(MigrationError):
        migrate()
    assert current_version() == 7
    with old_database.read() as u:
        after = (counts(u), u.session.query("SELECT id, library_id, description FROM documents ORDER BY id"))
        assert u.session.scalar("SELECT count(*) FROM libraries WHERE scope = 'student'") == 0
        assert not u.session.table_exists("project_folders")        # the schema change rolled back too
    assert before == after
    assert all(path.exists() for _, path in ids.values())           # no file was deleted
    monkeypatch.setitem(datasteps.STEPS, 8, real_step)
    assert migrate().version == 9                                   # and the next start migrates normally
    with old_database.read() as u:
        assert counts(u) == (4, 8)


def test_without_documents_no_library_is_created(old_database):
    with old_database.write() as u:
        project = u.projects.create("Empty", "/p/empty")
        u.documents.create_project_library(project.id)
    migrate()
    with old_database.read() as u:
        assert u.library.find_student_library() is None


def test_documents_from_disk_folders_stay_in_their_project(old_database):
    migrate()
    with old_database.write() as u:
        project = u.projects.create("Disk", "/p/disk")
        library = u.documents.create_project_library(project.id)
        folder = u.library.add_project_folder(project.id, "/data/papers")
        doc = u.documents.add_document(
            library.id, name="p.pdf", stored_path=f"folder:{folder.id}:p.pdf", mime_type="application/pdf",
            format="pdf", size_bytes=10, sha256=SHA["d"], folder_id=folder.id, rel_path="p.pdf")
        assert datasteps.move_documents_to_student_library(u.session) == []
        assert u.documents.get_document(doc.id).library_id == library.id


def test_files_outside_the_libraries_folder_are_never_deleted(env, tmp_path):
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("keep", encoding="utf-8")
    inside_dir = libraries_dir() / "1"
    inside_dir.mkdir(parents=True)
    inside = inside_dir / "gone.txt"
    inside.write_text("drop", encoding="utf-8")
    failed = datasteps.remove_stored_files([outside, inside, inside_dir / "missing.txt"])
    assert failed == [outside] and outside.exists() and not inside.exists()

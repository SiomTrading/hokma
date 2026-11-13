"""Project libraries, documents, passages and keyword search (SPEC-009 P1), on SQLite and on Postgres.

Postgres cases are skipped (with the reason) when HOKMA_TEST_POSTGRES_URL is not set, like test_db_postgres.py."""
import shutil
import sqlite3

import pytest
from dbsupport import env  # noqa: F401  (fixture)
from pgsupport import pg_admin_url, pg_template, pg_url, pg_url_migrated  # noqa: F401  (fixtures)

from hokma.db import get_database, migrate
from hokma.db.dbconfig import DatabaseConfig, save_config
from hokma.db.errors import DuplicateDocument, NotFound, ValidationError
from hokma.db.migrate import MIGRATIONS_DIR
from hokma.db.paths import db_path
from hokma.db.repos.documents import fold, make_excerpt, query_terms

SHA_A, SHA_B, SHA_C = "a" * 64, "b" * 64, "c" * 64


@pytest.fixture(params=["sqlite", "postgres"])
def database(request, env):  # noqa: F811
    if request.param == "postgres":
        save_config(DatabaseConfig("postgres", request.getfixturevalue("pg_url_migrated")))
    migrate()
    database = get_database()
    assert database.engine == request.param
    return database


def add(u, library_id, name, sha, chunks, **extra):
    return u.documents.add_document(
        library_id, name=name, stored_path=f"/data/libraries/{library_id}/{name}", mime_type="application/pdf",
        format="pdf", size_bytes=1234, sha256=sha, chunks=chunks, **extra)


def new_library(u, name="Challenge"):
    project = u.projects.create(name, f"/p/{name}")
    return project, u.documents.create_project_library(project.id)


# ------------------------------------------------------------------------------------------ text helpers
def test_fold_lowers_strips_accents_and_collapses_separators():
    assert fold("  Strategy of TRADING: Op\u00e7\u00e3o-Buy (\u00d1and\u00fa)!  ") == "strategy of trading opcao buy nandu"
    assert fold("") == "" and fold(None) == ""


def test_query_terms_are_distinct_words_in_search_form():
    assert query_terms('Op\u00e7\u00e3o "opcao" OR  buy*') == ["opcao", "or", "buy"]
    assert len(query_terms(" ".join(f"w{i}" for i in range(100)))) == 24


def test_excerpt_is_about_300_characters_around_the_first_matching_word():
    body = ("lorem ipsum " * 80) + "The Hedge delta lowers risk. " + ("dolor sit " * 80)
    excerpt = make_excerpt(body, ["hedge"])
    assert "Hedge delta" in excerpt and len(excerpt) <= 306
    assert excerpt.startswith("...") and excerpt.endswith("...")
    assert make_excerpt("short text", ["short"]) == "short text"


# ------------------------------------------------------------------------------------------ migration
def test_sqlite_in_this_environment_has_fts5(env):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='unicode61 remove_diacritics 2')")
    con.execute("INSERT INTO t(x) VALUES ('Caf\u00e9')")
    assert con.execute("SELECT count(*) FROM t WHERE t MATCH 'cafe'").fetchone()[0] == 1


def test_0007_applies_on_top_of_a_0006_database_without_touching_data(env):
    old = env / "migs-0006"
    old.mkdir()
    for path in MIGRATIONS_DIR.glob("*.sql"):
        if path.name < "0007":
            shutil.copy(path, old / path.name)
    assert migrate(migrations_dir=old).version == 6
    database = get_database()
    with database.write() as u:
        u.projects.create("Old project", "/p/old")
    result = migrate()
    assert result.applied == [7, 8, 9, 10, 11, 12, 13, 14] and result.version == 14
    with database.read() as u:
        assert [p.name for p in u.projects.list()] == ["Old project"]
        assert u.documents.find_project_library(u.projects.list()[0].id) is None
    con = sqlite3.connect(str(db_path()))
    assert {"libraries", "documents", "doc_chunks", "doc_chunks_fts"} <= {
        r[0] for r in con.execute("SELECT name FROM sqlite_master")}


# ------------------------------------------------------------------------------------------ libraries
def test_the_project_library_is_created_once(database):
    with database.write() as u:
        project, library = new_library(u)
        again = u.documents.create_project_library(project.id)
        assert again.id == library.id and library.scope == "project" and library.project_id == project.id
        assert library.name == "Challenge"
        with pytest.raises(NotFound):
            u.documents.create_project_library(9999)


def test_deleting_a_project_deletes_its_library_documents_and_passages(database):
    with database.write() as u:
        project, library = new_library(u)
        doc = add(u, library.id, "guide.pdf", SHA_A, [("page 1", "unique project text")])
    with database.write() as u:
        u.projects.delete(project.id)
    with database.read() as u:
        assert u.documents.find_project_library(project.id) is None
        assert u.session.scalar("SELECT count(*) FROM documents") == 0
        assert u.session.scalar("SELECT count(*) FROM doc_chunks") == 0
        assert u.documents.search([library.id], "unique") == []


# ------------------------------------------------------------------------------------------ documents
def test_add_list_and_status(database):
    with database.write() as u:
        _, library = new_library(u)
        doc = add(u, library.id, "a.pdf", SHA_A, [("page 1", "one two"), ("page 2", "three four"), ("page 3", "   ")],
                  source_url="https://example.org/a.pdf", description="a note")
        assert doc.status == "ready" and doc.chunk_count == 2 and doc.indexed_at and doc.source_url
        pending = add(u, library.id, "b.pdf", SHA_B, [], status="indexing")
        assert pending.chunk_count == 0 and pending.indexed_at is None
        assert u.documents.add_chunks(pending.id, [("page 1", "hello world")]) == 1
        assert u.documents.set_status(pending.id, "ready").chunk_count == 1
        failed = u.documents.set_status(pending.id, "failed", "no text")
        assert failed.status == "failed" and failed.status_detail == "no text"
        assert [d.name for d in u.documents.list_documents(library.id)] == ["a.pdf", "b.pdf"]


def test_bad_input_is_refused_before_sql(database):
    with database.write() as u:
        _, library = new_library(u)
        with pytest.raises(ValidationError):
            add(u, library.id, "x.pdf", "short", [])
        with pytest.raises(ValidationError):
            u.documents.add_document(library.id, name="x", stored_path="/x", mime_type="m", format="pdf",
                                     size_bytes=0, sha256=SHA_A)
        with pytest.raises(ValidationError):
            add(u, library.id, "x.pdf", SHA_A, [], status="weird")
        with pytest.raises(NotFound):
            add(u, 9999, "x.pdf", SHA_A, [])


def test_the_same_content_is_not_added_twice_to_a_library(database):
    with database.write() as u:
        project, library = new_library(u)
        first = add(u, library.id, "a.pdf", SHA_A, [("page 1", "content")])
        with pytest.raises(DuplicateDocument) as info:
            add(u, library.id, "copy.pdf", SHA_A.upper(), [("page 1", "content")])
        assert info.value.existing_id == first.id
        assert u.documents.find_by_sha256(library.id, SHA_A).id == first.id
        assert len(u.documents.list_documents(library.id)) == 1
        other = u.documents.create_project_library(u.projects.create("Other", "/p/other").id)
        assert add(u, other.id, "a.pdf", SHA_A, [("page 1", "content")]).library_id == other.id


# ------------------------------------------------------------------------------------------ search
def seed_corpus(u):
    _, library = new_library(u)
    add(u, library.id, "options.pdf", SHA_A, [
        ("page 1", "Introduction to financial options and their history."),
        ("page 2", "The delta hedge lowers the risk of an options book. Hedge, hedge."),
        ("page 3", "Premium and implied volatility in options.")])
    add(u, library.id, "futures.xlsx", SHA_B, [
        ("sheet Margins", "Initial margin of the NQ contract and maintenance margin."),
        ("sheet Hedge", "A simple hedge with futures.")])
    return library


def test_search_returns_file_section_and_excerpt(database):
    with database.write() as u:
        library = seed_corpus(u)
    with database.read() as u:
        hits = u.documents.search([library.id], "hedge delta")
        assert hits[0].document == "options.pdf" and hits[0].section == "page 2"
        assert "delta hedge" in hits[0].excerpt.lower() and hits[0].format == "pdf"
        assert hits[0].path.endswith("options.pdf") and hits[0].source_url is None and hits[0].score > 0
        assert [h.section for h in u.documents.search([library.id], "margin")] == ["sheet Margins"]


def test_search_ignores_accents_and_case_in_the_query_and_the_text(database):
    with database.write() as u:
        library = seed_corpus(u)
    with database.read() as u:
        for query in ("OPTIONS", "intr\u00f6duction", "Introduction", "h\u00efstory"):
            assert u.documents.search([library.id], query), query
        assert u.documents.search([library.id], "options")[0].document == "options.pdf"


def test_search_tries_all_words_first_then_any(database):
    with database.write() as u:
        library = seed_corpus(u)
    with database.read() as u:
        both = u.documents.search([library.id], "margin contract")
        assert [h.section for h in both] == ["sheet Margins"]
        either = u.documents.search([library.id], "margin volatility")
        assert {h.section for h in either} == {"sheet Margins", "page 3"}


def test_a_search_without_matches_is_empty(database):
    with database.write() as u:
        library = seed_corpus(u)
    with database.read() as u:
        assert u.documents.search([library.id], "quantum cryptography") == []
        assert u.documents.search([library.id], "") == []
        assert u.documents.search([library.id], "   ") == []
        assert u.documents.search([], "options") == []
        assert u.documents.search([library.id], "!!! ???") == []


def test_hostile_queries_never_reach_the_engine_syntax(database):
    with database.write() as u:
        library = seed_corpus(u)
    with database.read() as u:
        for query in ('"', "' OR 1=1 --", "options AND", "NEAR(a b)", "a:b", "(options", "*", "hedge & | !",
                      "options'; DROP TABLE documents; --"):
            u.documents.search([library.id], query)                      # must not raise
        assert u.documents.search([library.id], "NOT hedge")          # words, not operators


def test_ranking_puts_the_denser_passage_first_and_the_limit_applies(database):
    with database.write() as u:
        _, library = new_library(u)
        add(u, library.id, "r.txt", SHA_A, [("part 1", "swing " + "filler " * 40),
                                             ("part 2", "swing swing swing trading"),
                                             ("part 3", "other topic")])
    with database.read() as u:
        hits = u.documents.search([library.id], "swing")
        assert [h.section for h in hits] == ["part 2", "part 1"]
        assert len(u.documents.search([library.id], "swing", limit=1)) == 1


def test_search_stays_inside_the_libraries_asked_for(database):
    with database.write() as u:
        _, mine = new_library(u, "Mine")
        _, theirs = new_library(u, "Theirs")
        add(u, mine.id, "m.txt", SHA_A, [("part 1", "shared secret")])
        add(u, theirs.id, "t.txt", SHA_B, [("part 1", "other secret")])
    with database.read() as u:
        assert [h.document for h in u.documents.search([mine.id], "secret")] == ["m.txt"]
        assert {h.document for h in u.documents.search([mine.id, theirs.id], "secret")} == {"m.txt", "t.txt"}


def test_only_ready_documents_are_searched(database):
    with database.write() as u:
        _, library = new_library(u)
        doc = add(u, library.id, "p.txt", SHA_A, [("part 1", "waiting to be indexed")], status="indexing")
    with database.write() as u:
        assert u.documents.search([library.id], "waiting") == []
        u.documents.set_status(doc.id, "ready")
        assert len(u.documents.search([library.id], "waiting")) == 1
        u.documents.set_status(doc.id, "failed", "boom")
        assert u.documents.search([library.id], "waiting") == []


# ------------------------------------------------------------------------------------------ removal
def test_removing_a_document_deletes_its_passages_and_search_entries(database):
    with database.write() as u:
        library = seed_corpus(u)
    with database.write() as u:
        doc = u.documents.find_by_sha256(library.id, SHA_A)
        removed = u.documents.remove_document(doc.id)
        assert removed.stored_path.endswith("options.pdf")
    with database.read() as u:
        assert u.documents.search([library.id], "options") == []
        assert [h.document for h in u.documents.search([library.id], "hedge")] == ["futures.xlsx"]
        assert u.session.scalar("SELECT count(*) FROM doc_chunks WHERE document_id = :d", {"d": doc.id}) == 0
        assert u.session.scalar("SELECT count(*) FROM doc_chunks") == 2
        if database.engine == "sqlite":                                  # the index itself has no entries left
            assert u.session.scalar("SELECT count(*) FROM doc_chunks_fts WHERE doc_chunks_fts MATCH 'options'") == 0
            assert u.session.scalar("SELECT count(*) FROM doc_chunks_fts") == 2
        with pytest.raises(NotFound):
            u.documents.get_document(doc.id)


def test_a_removed_document_can_be_added_again(database):
    with database.write() as u:
        _, library = new_library(u)
        doc = add(u, library.id, "a.txt", SHA_A, [("part 1", "come back in")])
        u.documents.remove_document(doc.id)
        add(u, library.id, "a.txt", SHA_A, [("part 1", "come back in")])
        assert len(u.documents.search([library.id], "back")) == 1


# ------------------------------------------------------------------------------------------ engine copy
def test_the_search_index_is_filled_after_a_copy_from_sqlite_to_postgres(env, pg_url):
    from hokma.db.backend import SqliteBackend
    from hokma.db.database import Database
    from hokma.db.postgres import PostgresBackend
    from hokma.db.transfer import transfer

    migrate()
    with get_database().write() as u:
        library = seed_corpus(u)
    transfer(SqliteBackend(db_path()), PostgresBackend(pg_url))
    with Database(PostgresBackend(pg_url)).read() as u:
        assert u.documents.search([library.id], "hedge delta")[0].section == "page 2"
        assert len(u.documents.list_documents(library.id)) == 2


def test_rebuild_fills_the_sqlite_index_of_rows_copied_without_triggers(env):
    """The copy into SQLite inserts rows before the triggers exist; transfer then runs this statement."""
    migrate()
    con = sqlite3.connect(str(db_path()))
    with get_database().write() as u:
        library = seed_corpus(u)
    for name in ("ai", "ad", "au"):
        con.execute(f"DROP TRIGGER trg_doc_chunks_fts_{name}")
    con.execute("DELETE FROM doc_chunks_fts")                          # as if the rows had been copied without triggers
    con.commit()
    count = "SELECT count(*) FROM doc_chunks_fts WHERE doc_chunks_fts MATCH 'margin'"
    assert con.execute(count).fetchone()[0] == 0
    con.execute("INSERT INTO doc_chunks_fts(doc_chunks_fts) VALUES ('rebuild')")
    con.commit()
    assert con.execute(count).fetchone()[0] == 1

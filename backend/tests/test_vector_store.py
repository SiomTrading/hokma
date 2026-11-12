"""Vector store of the Library (SPEC-010 P11, section 4.5): `sqlite-vec` on SQLite, `pgvector` on Postgres and the
`BYTEA` + `numpy` fallback, all behind one API, with synthetic vectors (no model).

Postgres cases are skipped (with the reason) when HOKMA_TEST_POSTGRES_URL is not set. The fallback's own logic still
runs here: it only uses SQL that SQLite understands once its column exists, so its search is exercised on SQLite too.
"""
import math
import random
import re
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from dbsupport import env  # noqa: F401  (fixture)
from pgsupport import pg_admin_url, pg_template, pg_url, pg_url_migrated  # noqa: F401  (fixtures)

from hokma.db import get_database, migrate, vecext, vectors
from hokma.db.dbconfig import DatabaseConfig, save_config
from hokma.db.errors import ValidationError
from hokma.db.vectors import (DIM, ByteaVectorStore, PgVectorStore, SqliteVecStore, VectorUnavailable, backfill,
                              normalize, open_store, prepare)

MODEL = "test-model-v1"
ROOT = Path(__file__).resolve().parents[2]


def unit_vector(seed: int, near: list[float] | None = None, noise: float = 0.0) -> list[float]:
    rng = random.Random(seed)
    base = near or [rng.gauss(0, 1) for _ in range(DIM)]
    if near is not None:
        base = [x + rng.gauss(0, noise) for x in base]
    norm = math.sqrt(sum(x * x for x in base))
    return [x / norm for x in base]


def similarity(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))


# ------------------------------------------------------------------------------------------ the three stores
def _sqlite_bytea_schema(self):
    """SQLite has no `ADD COLUMN IF NOT EXISTS`; the same column, added once."""
    cols = {r["name"] for r in self.s.query("PRAGMA table_info(doc_chunk_vectors)")}
    if "vec" not in cols:
        self.s.execute("ALTER TABLE doc_chunk_vectors ADD COLUMN vec BLOB")


@pytest.fixture(params=["sqlite-vec", "bytea", "pgvector", "bytea-on-postgres"])
def store_kind(request, env, monkeypatch):  # noqa: F811
    """Yields `(database, kind)` for each store; Postgres kinds are skipped without a server."""
    kind = request.param
    if kind in ("pgvector", "bytea-on-postgres"):
        save_config(DatabaseConfig("postgres", request.getfixturevalue("pg_url_migrated")))
    elif kind == "bytea":
        monkeypatch.setattr(ByteaVectorStore, "ensure_schema", _sqlite_bytea_schema)
    migrate()
    database = get_database()
    if kind == "pgvector":
        status = prepare(database)
        if status.store != "pgvector":
            pytest.skip("this Postgres server does not offer the pgvector extension")
    elif kind == "bytea-on-postgres":
        monkeypatch.setattr(vectors.PgVectorStore, "ensure_schema", lambda self: (_ for _ in ()).throw(RuntimeError("no ext")))
        status = prepare(database)
        assert status.store == "bytea"
    elif kind == "bytea":
        with database.write() as u:
            ByteaVectorStore(u.session).ensure_schema()
    else:
        assert prepare(database).store == "sqlite-vec"
    vectors.reset_status()
    return database, kind


def store_of(unit, kind):
    if kind == "bytea":
        return ByteaVectorStore(unit.session)
    return open_store(unit.session)


def seed(db, count_per_doc=6, docs=4, status="ready"):
    """A student Library with a Library folder tree, a project with a disk folder, and documents with passages: the
    first `docs` of root, papers, deep, other in the Library, plus `disk.md` read from the project's disk folder."""
    with db.write() as u:
        lib = u.library.ensure_student_library()
        papers = u.library.create_folder(lib.id, "Papers")
        y2026 = u.library.create_folder(lib.id, "2026", parent_id=papers.id)
        other = u.library.create_folder(lib.id, "Other")
        project = u.projects.create("P", "/p/p")
        plib = u.documents.create_project_library(project.id)
        pf = u.library.add_project_folder(project.id, "/tmp/notes", label="notes")
        specs = [("root.pdf", lib.id, None, None), ("papers.pdf", lib.id, papers.id, None),
                 ("deep.pdf", lib.id, y2026.id, None), ("other.pdf", lib.id, other.id, None)][:docs]
        specs.append(("disk.md", plib.id, None, pf.id))
        out = {"lib": lib.id, "papers": papers.id, "y2026": y2026.id, "other": other.id, "plib": plib.id, "pf": pf.id,
               "docs": {}, "chunks": {}}
        n = 0
        for name, library_id, lf, folder_id in specs:
            n += 1
            doc = u.documents.add_document(
                library_id, name=name, stored_path=f"/data/{library_id}/{name}" if folder_id is None else f"folder:{folder_id}:{name}",
                mime_type="text/plain", format="txt", size_bytes=10, sha256=f"{n:064x}", status=status,
                library_folder_id=lf, folder_id=folder_id, rel_path=name if folder_id else None,
                chunks=[(f"page {i}", f"text of {name} {i}") for i in range(count_per_doc)])
            out["docs"][name] = doc.id
            out["chunks"][name] = [int(r["id"]) for r in u.session.query(
                "SELECT id FROM doc_chunks WHERE document_id = :d ORDER BY ord", {"d": doc.id})]
    return out


def fill(db, kind, layout, model=MODEL):
    """One distinct vector per passage, and a planted passage per document close to a probe vector."""
    vecs = {}
    with db.write() as u:
        store = store_of(u, kind)
        items = []
        for name, chunks in layout["chunks"].items():
            for index, chunk in enumerate(chunks):
                vecs[chunk] = unit_vector(chunk * 7919)
                items.append((chunk, vecs[chunk]))
        assert store.put(items, model) == len(items)
    return vecs


# ------------------------------------------------------------------------------------------ done criterion 1 and 2
def test_sqlite_vec_is_pinned_with_wheels_for_the_four_platforms_and_linux_arm():
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    block = lock[lock.index('[[package]]\nname = "sqlite-vec"'):]
    block = block[:block.index("\n[[package]]", 10)]
    for wheel in ("win_amd64", "macosx_11_0_arm64", "macosx_10_6_x86_64", "manylinux2014_x86_64", "manylinux2014_aarch64"):
        assert re.search(rf"sqlite_vec-0\.1\.9-py3-none-[\w.]*{wheel}[\w.]*\.whl", block), wheel
    assert "sdist" not in block                       # no source build is ever needed
    assert re.search(r'"sqlite-vec==0\.1\.9"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_the_python_of_hokma_loads_sqlite_vec_and_finds_the_nearest_vector():
    """The smoke the CI repeats on each platform with the Python the product ships (P14)."""
    support = vecext.check_support()
    assert support.ok, support
    conn = sqlite3.connect(":memory:")
    assert vecext.load_into(conn) is True
    conn.execute("CREATE VIRTUAL TABLE t USING vec0(embedding float[2])")
    conn.execute("INSERT INTO t(rowid, embedding) VALUES (1, ?), (2, ?)", (b"\x00\x00\x80?\x00\x00\x00\x00", b"\x00\x00\x00\x00\x00\x00\x80?"))
    assert conn.execute("SELECT rowid FROM t WHERE embedding MATCH ? AND k = 1", (b"\x00\x00\x80?\x00\x00\x00\x00",)).fetchone()[0] == 1
    conn.close()


# ------------------------------------------------------------------------------------------ done criterion 3
class _NoExtensionConnection:
    """A `sqlite3.Connection` of a Python built without extension loading: the method does not exist."""


def test_a_python_without_load_extension_gives_a_clear_app_error_not_a_traceback(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("HOKMA_LANG", "es")
    fake = SimpleNamespace(Connection=_NoExtensionConnection, connect=sqlite3.connect, Error=sqlite3.Error)
    monkeypatch.setattr(vecext, "sqlite3", fake)
    monkeypatch.setattr("hokma.db.backend.load_vec_extension", vecext.load_into)
    assert vecext.load_into(_NoExtensionConnection()) is False
    support = vecext.check_support()
    assert (support.ok, support.code, support.retry) == (False, "no_load_extension", False)

    migrate()
    db = get_database()
    monkeypatch.setattr("hokma.db.backend.load_vec_extension", lambda conn: False)
    vectors.reset_status()
    status = prepare(db)
    assert status.ok is False and status.code == "no_load_extension" and status.retry is False
    assert "Traceback" not in status.message and "enable_load_extension" not in status.message
    import json
    spanish = json.loads((ROOT / "backend/hokma/catalog/es.json").read_text(encoding="utf-8"))
    assert status.message == spanish["library.vectors.no_load_extension"]
    with db.read() as u:
        with pytest.raises(VectorUnavailable) as caught:
            open_store(u.session)
    assert caught.value.code == "no_load_extension"
    vectors.reset_status()


def test_a_blocked_library_offers_retry_and_the_status_reaches_the_library_screen(env, monkeypatch):  # noqa: F811
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from hokma.core import session
    from hokma.docs.api import router as docs_router
    from hokma.rpc.ws import SESSION_TOKEN

    migrate()
    db = get_database()
    monkeypatch.setattr(vecext, "check_support", lambda: vecext.VecSupport(False, "load_failed", "blocked", True))
    vectors.reset_status()
    monkeypatch.setattr(session, "owner_matches", lambda header: True)
    app = FastAPI()
    app.include_router(docs_router)
    with TestClient(app) as client:
        body = client.get("/api/library/status", headers={"X-Hokma-Token": SESSION_TOKEN}).json()
    assert body["vectors"]["ok"] is False and body["vectors"]["error"] == "load_failed" and body["vectors"]["retry"] is True
    assert "antivirus" in body["vectors"]["message"]
    monkeypatch.undo()
    vectors.reset_status()
    assert prepare(db).ok is True                               # Retry: the same call, now it works
    vectors.reset_status()


def test_every_vector_error_has_a_message_in_both_languages():
    import json
    for lang in ("en", "es"):
        catalog = json.loads((ROOT / "backend/hokma/catalog" / f"{lang}.json").read_text(encoding="utf-8"))
        for code in ("no_load_extension", "module_missing", "load_failed", "not_prepared", "schema_failed"):
            assert catalog[f"library.vectors.{code}"].strip()


def test_a_session_without_the_extension_cannot_open_the_sqlite_store(env, monkeypatch):  # noqa: F811
    migrate()
    monkeypatch.setattr("hokma.db.backend.load_vec_extension", lambda conn: False)
    with get_database().read() as u:
        with pytest.raises(VectorUnavailable):
            SqliteVecStore(u.session)


# ------------------------------------------------------------------------------------------ done criteria 4 and 5
def test_postgres_store_is_chosen_and_registered(env, pg_url_migrated, caplog):  # noqa: F811
    save_config(DatabaseConfig("postgres", pg_url_migrated))
    migrate()
    db = get_database()
    with caplog.at_level("INFO", logger="hokma.vectors"):
        status = prepare(db)
    assert status.ok and status.store in ("pgvector", "bytea")
    assert any("vector store" in r.getMessage() for r in caplog.records)
    with db.read() as u:
        assert open_store(u.session).name == status.store
    vectors.reset_status()


def test_the_same_questions_give_the_same_order_in_every_store(store_kind):
    db, kind = store_kind
    layout = seed(db)
    vecs = fill(db, kind, layout)
    everything = dict(library_ids=[layout["lib"], layout["plib"]], folder_ids=[layout["pf"]])
    probes = [vecs[layout["chunks"]["papers.pdf"][2]],                                 # an exact passage
              unit_vector(3, near=vecs[layout["chunks"]["deep.pdf"][1]], noise=0.02),  # close to one
              unit_vector(99)]                                                         # unrelated
    with db.read() as u:
        store = store_of(u, kind)
        got = [store.search(p, limit=10, model_id=MODEL, **everything) for p in probes]
    for probe, hits in zip(probes, got):
        expected = sorted(((similarity(probe, v), c) for c, v in vecs.items()), key=lambda x: (-x[0], x[1]))[:10]
        assert len(hits) == 10
        for hit, (sim, chunk) in zip(hits, expected):
            assert hit.similarity == pytest.approx(sim, abs=1e-4)
            if hit.chunk_id != chunk:                                                  # an order swap is only a tie
                assert vecs[hit.chunk_id] is not None
                assert similarity(probe, vecs[hit.chunk_id]) == pytest.approx(sim, abs=1e-4)
    assert got[0][0].chunk_id == layout["chunks"]["papers.pdf"][2] and got[0][0].similarity == pytest.approx(1.0, abs=1e-5)
    assert got[1][0].chunk_id == layout["chunks"]["deep.pdf"][1]
    ids = [[h.chunk_id for h in hits] for hits in got]
    assert all(len(set(i)) == len(i) for i in ids)


def test_filters_by_library_disk_folder_library_folder_and_subfolders(store_kind):
    db, kind = store_kind
    layout = seed(db)
    vecs = fill(db, kind, layout)
    chunks = layout["chunks"]
    probe = vecs[chunks["papers.pdf"][0]]
    with db.read() as u:
        store = store_of(u, kind)
        tree = u.library.descendant_ids(layout["papers"])
        assert tree == sorted([layout["papers"], layout["y2026"]])
        def ids(**kw):
            return {h.chunk_id for h in store.search(probe, limit=200, model_id=MODEL, **kw)}
        library_only = ids(library_ids=[layout["lib"]])
        assert library_only == {c for n in ("root.pdf", "papers.pdf", "deep.pdf", "other.pdf") for c in chunks[n]}
        assert ids(library_ids=[layout["lib"]], library_folder_ids=tree) == set(chunks["papers.pdf"]) | set(chunks["deep.pdf"])
        assert ids(library_ids=[layout["lib"]], library_folder_ids=[layout["y2026"]]) == set(chunks["deep.pdf"])
        assert ids(library_ids=[layout["lib"]], library_folder_ids=[]) == set()
        assert ids(folder_ids=[layout["pf"]]) == set(chunks["disk.md"])
        assert ids(library_ids=[layout["lib"]], folder_ids=[layout["pf"]], library_folder_ids=[layout["other"]]) == \
            set(chunks["other.pdf"]) | set(chunks["disk.md"])
        assert ids() == set() and ids(library_ids=[999]) == set()
        assert store.search(probe, library_ids=[layout["lib"]], limit=3, model_id="another-model") == []
        assert len(store.search(probe, library_ids=[layout["lib"]], limit=3)) == 3


def test_passages_of_documents_not_ready_are_never_returned(store_kind):
    db, kind = store_kind
    layout = seed(db)
    vecs = fill(db, kind, layout)
    with db.write() as u:
        u.documents.set_status(layout["docs"]["papers.pdf"], "indexing")
        u.documents.set_status(layout["docs"]["other.pdf"], "failed", "x")
    with db.read() as u:
        hits = store_of(u, kind).search(vecs[layout["chunks"]["papers.pdf"][0]], library_ids=[layout["lib"]], limit=100)
    returned = {h.chunk_id for h in hits}
    assert returned == set(layout["chunks"]["root.pdf"]) | set(layout["chunks"]["deep.pdf"])


def test_put_replaces_skips_missing_passages_and_validates(store_kind):
    db, kind = store_kind
    layout = seed(db, count_per_doc=2, docs=1)
    chunk = layout["chunks"]["root.pdf"][0]
    other = layout["chunks"]["root.pdf"][1]
    with db.write() as u:
        store = store_of(u, kind)
        assert store.put([(chunk, unit_vector(1)), (other, unit_vector(2)), (987654, unit_vector(3))], MODEL) == 2
        assert store.put([(chunk, unit_vector(50))], "model-2") == 1
        assert store.put([], MODEL) == 0
        for bad in ([1.0] * (DIM - 1), [0.0] * DIM, [float("nan")] * DIM):
            with pytest.raises(ValidationError):
                store.put([(chunk, bad)], MODEL)
        with pytest.raises(ValidationError):
            store.put([(chunk, unit_vector(1))], "")
        row = u.session.one("SELECT model_id, dim FROM doc_chunk_vectors WHERE chunk_id = :c", {"c": chunk})
        assert row == {"model_id": "model-2", "dim": DIM}
        hits = store.search(unit_vector(50), library_ids=[layout["lib"]], limit=5)
        assert hits[0].chunk_id == chunk and hits[0].similarity == pytest.approx(1.0, abs=1e-5)
        assert store.search(unit_vector(50), library_ids=[layout["lib"]], limit=5, model_id=MODEL)[0].chunk_id == other


def test_normalize_gives_unit_float32():
    v = normalize([3.0] + [4.0] + [0.0] * (DIM - 2))
    assert len(v) == DIM and v.typecode == "f"
    assert math.sqrt(sum(x * x for x in v)) == pytest.approx(1.0, abs=1e-6) and v[0] == pytest.approx(0.6)


# ------------------------------------------------------------------------------------------ storage hygiene
def test_removing_a_document_removes_its_vectors_and_prune_clears_the_rest(env):  # noqa: F811
    migrate()
    db = get_database()
    prepare(db)
    layout = seed(db, count_per_doc=3, docs=2)
    fill(db, "sqlite-vec", layout)
    with db.write() as u:
        u.documents.remove_document(layout["docs"]["papers.pdf"])
    with db.write() as u:
        assert u.session.scalar("SELECT COUNT(*) FROM doc_chunk_vectors") == 3 * 2          # control rows cascade
        assert u.session.scalar("SELECT COUNT(*) FROM doc_chunk_vec") == 3 * 3              # vectors wait for prune
        store = open_store(u.session)
        assert store.prune() == 3
        assert u.session.scalar("SELECT COUNT(*) FROM doc_chunk_vec") == 3 * 2
        assert store.prune() == 0
    vectors.reset_status()


def test_numpy_is_not_imported_by_the_sqlite_store(env):  # noqa: F811
    import subprocess
    import sys
    code = ("import sys; import hokma.db.vectors as v, hokma.db.database; "
            "assert 'numpy' not in sys.modules, 'numpy was imported'")
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True,
                          env={"PYTHONPATH": str(ROOT / "backend"), "PATH": ""})
    # faster-whisper pulls numpy only when voice code is imported, which this import chain never does
    assert done.returncode == 0, done.stderr[-400:]


def test_transfer_copies_only_the_declared_columns_of_the_control_table():
    from hokma.db import transfer

    class Recorder:
        engine = "sqlite"
        sqls: list[str] = []

        def query(self, sql, params=None):
            self.sqls.append(sql)
            return []

    transfer._copy_table(Recorder(), Recorder(), transfer.TableInfo("doc_chunk_vectors"))
    assert Recorder.sqls == ["SELECT chunk_id, model_id, dim FROM doc_chunk_vectors ORDER BY 1"]


def test_the_fallback_matrix_is_rebuilt_when_vectors_change(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(ByteaVectorStore, "ensure_schema", _sqlite_bytea_schema)
    migrate()
    db = get_database()
    layout = seed(db, count_per_doc=3, docs=1)
    with db.write() as u:
        ByteaVectorStore(u.session).ensure_schema()
    a, b, c = layout["chunks"]["root.pdf"]
    with db.write() as u:
        ByteaVectorStore(u.session).put([(a, unit_vector(1)), (b, unit_vector(2))], MODEL)
    with db.read() as u:
        first = ByteaVectorStore(u.session).search(unit_vector(1), library_ids=[layout["lib"]], limit=5)
    assert [h.chunk_id for h in first][0] == a and len(first) == 2
    with db.write() as u:
        ByteaVectorStore(u.session).put([(c, unit_vector(1))], MODEL)
        u.session.execute("DELETE FROM doc_chunks WHERE id = :c", {"c": b})
    with db.read() as u:
        again = ByteaVectorStore(u.session).search(unit_vector(1), library_ids=[layout["lib"]], limit=5)
    assert {h.chunk_id for h in again} == {a, c}


# ------------------------------------------------------------------------------------------ scale
def test_searching_thousands_of_passages_is_fast_and_exact(env):  # noqa: F811
    migrate()
    db = get_database()
    prepare(db)
    layout = seed(db, count_per_doc=1, docs=1)
    lib = layout["lib"]
    rng = random.Random(5)
    with db.write() as u:
        doc = layout["docs"]["root.pdf"]
        rows = [{"d": doc, "o": i + 1, "s": "", "b": f"b{i}", "t": f"b{i}"} for i in range(3000)]
        u.session.execute_many("INSERT INTO doc_chunks (document_id, ord, section, body, search_text) VALUES (:d,:o,:s,:b,:t)", rows)
        ids = [int(r["id"]) for r in u.session.query("SELECT id FROM doc_chunks WHERE document_id = :d", {"d": doc})]
        vecs = {i: unit_vector(rng.randrange(10 ** 9)) for i in ids}
        assert open_store(u.session).put(list(vecs.items()), MODEL) == len(ids)
    probe = unit_vector(77)
    start = time.perf_counter()
    with db.read() as u:
        hits = open_store(u.session).search(probe, library_ids=[lib], limit=50, model_id=MODEL)
    assert time.perf_counter() - start < 2.0
    best = sorted(((similarity(probe, v), i) for i, v in vecs.items()), reverse=True)[:50]
    assert [h.chunk_id for h in hits] == [i for _, i in best]
    vectors.reset_status()


# ------------------------------------------------------------------------------------------ backfill
def fake_embed(texts):
    return [unit_vector(sum(ord(ch) for ch in t) + len(t)) for t in texts]


def test_backfill_vectorizes_in_batches_and_marks_documents_ready(env):  # noqa: F811
    migrate()
    db = get_database()
    prepare(db)
    layout = seed(db, count_per_doc=5, docs=2, status="indexing")
    with db.read() as u:
        store = open_store(u.session)
        assert store.count_pending(MODEL) == 15
        assert len(store.pending(4, MODEL)) == 4
    calls = []
    result = backfill(db, lambda texts: calls.append(len(texts)) or fake_embed(texts), MODEL, batch=4)
    assert (result.vectorized, result.documents_ready, result.error, result.stopped) == (15, 3, None, False)
    assert calls == [4, 4, 4, 3]
    with db.read() as u:
        assert open_store(u.session).count_pending(MODEL) == 0
        assert {d.status for d in u.documents.list_documents(layout["lib"])} == {"ready"}
        assert u.session.scalar("SELECT COUNT(*) FROM doc_chunk_vec") == 15
    assert backfill(db, fake_embed, MODEL).vectorized == 0                                     # nothing left
    vectors.reset_status()


def test_backfill_is_resumable_after_a_failure_and_a_stop(env):  # noqa: F811
    migrate()
    db = get_database()
    prepare(db)
    layout = seed(db, count_per_doc=6, docs=0, status="indexing")
    state = {"n": 0}

    def flaky(texts):
        state["n"] += 1
        if state["n"] == 2:
            raise RuntimeError("model went away")
        return fake_embed(texts)

    first = backfill(db, flaky, MODEL, batch=4)
    assert first.vectorized == 4 and first.error.startswith("RuntimeError") and first.documents_ready == 0
    with db.read() as u:
        assert u.documents.get_document(layout["docs"]["disk.md"]).status == "indexing"
    stop = backfill(db, fake_embed, MODEL, batch=1, should_stop=lambda: True)
    assert stop.stopped and stop.vectorized == 0
    capped = backfill(db, fake_embed, MODEL, batch=1, max_batches=1)
    assert capped.vectorized == 1 and capped.batches == 1
    last = backfill(db, fake_embed, MODEL, batch=4)
    assert last.vectorized == 1 and last.documents_ready == 1 and last.error is None
    with db.read() as u:
        assert u.documents.get_document(layout["docs"]["disk.md"]).status == "ready"
    vectors.reset_status()


def test_backfill_skips_failed_documents_recomputes_for_a_new_model_and_checks_the_count(env):  # noqa: F811
    migrate()
    db = get_database()
    prepare(db)
    layout = seed(db, count_per_doc=2, docs=2, status="indexing")
    with db.write() as u:
        u.documents.set_status(layout["docs"]["papers.pdf"], "failed", "unreadable")
    assert backfill(db, fake_embed, MODEL).vectorized == 4                                     # 3 docs - 1 failed
    bad = backfill(db, lambda texts: [], "model-2", batch=2)
    assert bad.vectorized == 0 and "returned 0 vectors" in bad.error
    again = backfill(db, fake_embed, "model-2")
    assert again.vectorized == 4
    with db.read() as u:
        assert u.session.scalar("SELECT COUNT(DISTINCT model_id) FROM doc_chunk_vectors") == 1
    vectors.reset_status()


def test_a_database_moved_between_engines_shows_its_passages_as_pending(env):  # noqa: F811
    """Control rows travel with a move but vectors do not: the passages that lack a stored vector are pending."""
    migrate()
    db = get_database()
    prepare(db)
    layout = seed(db, count_per_doc=2, docs=0)
    with db.write() as u:
        for chunk in layout["chunks"]["disk.md"]:
            u.session.insert_row("doc_chunk_vectors", {"chunk_id": chunk, "model_id": MODEL, "dim": DIM})
    with db.read() as u:
        assert open_store(u.session).count_pending(MODEL) == 2
    vectors.reset_status()

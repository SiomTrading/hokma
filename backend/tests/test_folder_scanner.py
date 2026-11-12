"""The Library's reading thread (SPEC-010 P8): it indexes, reindexes only on a content change, forgets what
disappeared, marks a vanished folder `missing`, respects the caps and reports its queue. Folders are temporary and
the clock is driven by hand, so nothing here waits a real minute."""
import hashlib
import os
import threading
import time
from pathlib import Path

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hokma.core import session
from hokma.docs import diskfolders, folder_scan, folder_worker
from hokma.docs.api import router as docs_router
from hokma.projects.api import router as projects_router
from hokma.rpc.ws import SESSION_TOKEN

H = {"X-Hokma-Token": SESSION_TOKEN}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("HOKMA_LANG", "es")
    monkeypatch.setattr(session, "owner_matches", lambda header: True)
    with db.write() as u:
        u.profile.save(owner_name="Ana", fund_name="Fondo Prueba")
    app = FastAPI()
    app.include_router(projects_router)
    app.include_router(docs_router)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def folder(client, db, tmp_path):
    """A disk folder of a new project, outside the project's own folder."""
    project = client.post("/api/projects", headers=H, json={"name": "Proyecto"}).json()
    root = tmp_path / "notas"
    root.mkdir()
    fid = diskfolders.add_folder(project["id"], str(root), db)["id"]
    return fid, root, project["id"]


def _docs(u, fid):
    from hokma.db.models import Document
    return [Document.from_row(r) for r in u.session.query("SELECT * FROM documents WHERE folder_id = :f", {"f": fid})]


def make_worker(db, clock, **kw):
    return folder_worker.FolderWorker(get_db=lambda: db, interval=60.0, clock=clock, **kw)


def test_new_changed_and_removed_files_show_within_one_poll(db, folder):
    fid, root, _ = folder
    clock = Clock()
    worker = make_worker(db, clock)
    worker.tick()                                                              # first poll runs right away
    (root / "a.md").write_text("# A\n\nLa cobertura delta del zafiro.\n", encoding="utf-8")
    clock.now += 59
    assert worker.tick() == 0                                                  # not due yet: nothing is read
    assert "a.md" not in {d.rel_path for d in _docs_in(db, fid)}
    clock.now += 1                                                             # 60 s: due
    worker.tick()
    assert {d.rel_path for d in _docs_in(db, fid)} == {"a.md"}
    (root / "a.md").write_text("# A\n\nAhora habla del cuarzo y su teorema.\n", encoding="utf-8")
    os.utime(root / "a.md", ns=(10**18, 10**18 + 5))
    clock.now += 60
    worker.tick()
    with db.read() as u:
        assert u.documents.search([u.documents.find_project_library(folder[2]).id], "cuarzo")
    (root / "a.md").unlink()
    clock.now += 60
    worker.tick()
    assert _docs_in(db, fid) == []


def _docs_in(db, fid):
    with db.read() as u:
        return _docs(u, fid)


def test_a_touched_file_with_the_same_content_is_not_reindexed(db, folder, monkeypatch):
    fid, root, _ = folder
    (root / "a.md").write_text("# A\n\nContenido estable.\n", encoding="utf-8")
    clock = Clock()
    worker = make_worker(db, clock)
    worker.tick()
    first = _docs_in(db, fid)[0]
    calls = []
    real = folder_scan._index
    monkeypatch.setattr(folder_scan, "_index", lambda *a, **k: calls.append(1) or real(*a, **k))
    os.utime(root / "a.md", ns=(2 * 10**18, 2 * 10**18))                       # touched, same bytes
    clock.now += 60
    worker.tick()
    again = _docs_in(db, fid)[0]
    assert calls == [] and again.id == first.id and again.mtime_ns == 2 * 10**18


def test_a_folder_that_vanishes_is_missing_and_keeps_its_passages_until_it_returns(db, folder, tmp_path):
    fid, root, _ = folder
    (root / "a.md").write_text("# A\n\nLa tesis del cuarzo.\n", encoding="utf-8")
    clock = Clock()
    worker = make_worker(db, clock)
    worker.tick()
    moved = tmp_path / "movida"
    root.rename(moved)
    clock.now += 60
    worker.tick()
    with db.read() as u:
        assert u.library.get_project_folder(fid).status == "missing"
    assert len(_docs_in(db, fid)) == 1
    moved.rename(root)
    clock.now += 60
    worker.tick()
    with db.read() as u:
        assert u.library.get_project_folder(fid).status == "ready"
    assert len(_docs_in(db, fid)) == 1


def test_5000_files_are_walked_and_compared_quickly(db, folder):
    fid, root, pid = folder
    for i in range(5000):
        (root / f"n{i:04d}.md").write_text(f"x{i}", encoding="utf-8")
    seen, over_cap, _ = folder_scan.walk_supported(root)
    assert len(seen) == 5000 and over_cap == 0
    with db.write() as u:                                                     # the state after a first full read
        library = u.documents.create_project_library(pid)
        for rel, (size, mtime_ns) in seen.items():
            u.documents.add_document(library.id, name=rel, stored_path=folder_scan.stored_path(fid, rel),
                                     mime_type="text/markdown", format="markdown", size_bytes=size, sha256=hashlib.sha256(rel.encode()).hexdigest(),
                                     status="ready", folder_id=fid, rel_path=rel, mtime_ns=mtime_ns)
    start = time.perf_counter()
    result = folder_scan.scan_folder(fid, db)
    elapsed = time.perf_counter() - start
    assert result.unchanged == 5000 and result.added == result.updated == result.removed == 0
    assert elapsed < 10.0, elapsed


def test_the_passage_cap_stops_reading_and_says_so(db, folder, monkeypatch):
    fid, root, _ = folder
    monkeypatch.setattr(folder_scan, "MAX_PASSAGES", 1)
    for name in ("a.md", "b.md", "c.md"):
        (root / name).write_text(f"# {name}\n\nTexto de {name}.\n", encoding="utf-8")
    result = folder_scan.scan_folder(fid, db)
    assert result.passage_cap and result.added < 3
    with db.read() as u:
        detail = u.library.get_project_folder(fid).status_detail
    assert detail and "1" in detail


def test_at_the_cap_a_changed_file_is_reindexed_and_a_new_one_waits(db, folder, monkeypatch):
    fid, root, _ = folder
    monkeypatch.setattr(folder_scan, "MAX_PASSAGES", 1)
    (root / "a.md").write_text("# A\n\nTexto inicial.\n", encoding="utf-8")
    folder_scan.scan_folder(fid, db)
    before = folder_scan.total_passages(db)
    (root / "a.md").write_text("# A\n\nTexto cambiado del todo.\n", encoding="utf-8")
    (root / "b.md").write_text("# B\n\nArchivo nuevo.\n", encoding="utf-8")
    result = folder_scan.scan_folder(fid, db)
    assert result.updated == 1 and result.added == 0 and result.passage_cap
    assert folder_scan.total_passages(db) == before                           # replaced, not summed
    with db.read() as u:
        assert [d.rel_path for d in _docs(u, fid)] == ["a.md"]


def test_a_failing_folder_does_not_stop_the_thread(db, folder, monkeypatch):
    fid, root, _ = folder
    (root / "a.md").write_text("# A\n\ncuarzo\n", encoding="utf-8")
    clock = Clock()
    worker = make_worker(db, clock)
    real = folder_scan.scan_folder
    monkeypatch.setattr(folder_scan, "scan_folder", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert worker.tick() == 0
    monkeypatch.setattr(folder_scan, "scan_folder", real)
    clock.now += 60
    assert worker.tick() >= 1


def test_a_folder_asked_twice_waits_once(db, folder):
    fid, _, _ = folder
    worker = make_worker(db, Clock())
    worker.request(fid)
    worker.request(fid)
    assert worker.queue_state()["queued_folders"] == 1


def test_the_thread_reads_by_itself_and_stops(db, folder):
    fid, root, _ = folder
    worker = folder_worker.FolderWorker(get_db=lambda: db, interval=0.2)
    worker.start()
    try:
        (root / "late.md").write_text("# L\n\narrived late\n", encoding="utf-8")
        deadline = time.time() + 10
        while time.time() < deadline and not _docs_in(db, fid):
            time.sleep(0.1)
        assert [d.rel_path for d in _docs_in(db, fid)] == ["late.md"]
        assert worker.running
    finally:
        worker.stop()
    assert not worker.running


def test_opening_a_project_asks_for_a_pass_only_when_the_worker_runs(db, folder, monkeypatch):
    fid, _, pid = folder
    assert folder_worker.get_worker() is None
    diskfolders.list_folders(pid, db)                                         # no worker: nothing happens, no error
    worker = make_worker(db, Clock())
    monkeypatch.setattr(folder_worker, "_WORKER", worker)
    monkeypatch.setattr(worker, "_thread", threading.Thread(target=lambda: time.sleep(5), daemon=True))
    worker._thread.start()
    diskfolders.list_folders(pid, db)
    assert worker.queue_state()["queued_folders"] >= 1


def test_status_endpoint_reports_the_queue(client, db, folder):
    fid, root, _ = folder
    (root / "a.md").write_text("# A\n\nPrimer pasaje.\n", encoding="utf-8")
    r = client.get("/api/library/status", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["worker"] is False and body["files_to_read"] == 0 and body["passages"] >= 0
    assert body["max_passages"] == folder_scan.MAX_PASSAGES and body["active_folder"] is None
    folder_scan.scan_folder(fid, db)
    body = client.get("/api/library/status", headers=H).json()
    assert body["passages"] >= 1 and body["passages_to_vectorize"] == body["passages"]
    assert client.get("/api/library/status").status_code in (401, 403)


def test_status_shows_the_folder_being_read(client, db, folder, monkeypatch):
    fid, root, _ = folder
    worker = make_worker(db, Clock())
    monkeypatch.setattr(folder_worker, "_WORKER", worker)
    worker._active = fid
    body = client.get("/api/library/status", headers=H).json()
    assert body["active_folder"]["id"] == fid


# ------------------------------------------------------------------ vectors (the thread also gives each passage its vector)

def _waiting(db):
    with db.read() as u:
        return int(u.session.scalar(
            "SELECT COUNT(*) FROM doc_chunks c WHERE NOT EXISTS (SELECT 1 FROM doc_chunk_vectors v WHERE v.chunk_id = c.id)") or 0)


@pytest.fixture
def model(monkeypatch):
    from test_vector_store import MODEL, unit_vector

    from hokma.db import vectors
    from hokma.library import runtime

    vectors.reset_status()
    calls = []
    monkeypatch.setattr(runtime, "model_ready", lambda: True)
    monkeypatch.setattr(runtime, "model_id", lambda: MODEL)
    monkeypatch.setattr(runtime, "embed_passages", lambda texts: calls.append(list(texts)) or [unit_vector(len(t) + 1) for t in texts])
    return calls


def test_the_thread_gives_a_vector_to_every_new_passage(db, folder, model):
    fid, root, _ = folder
    clock = Clock()
    (root / "a.md").write_text("# A\n\nvolatility regimes\n", encoding="utf-8")
    from hokma.db import vectors
    vectors.prepare(db)
    worker = make_worker(db, clock)
    worker.tick()
    assert model and _waiting(db) == 0


def test_without_the_model_the_passages_wait_and_nothing_fails(db, folder, monkeypatch):
    fid, root, _ = folder
    clock = Clock()
    (root / "a.md").write_text("# A\n\nvolatility regimes\n", encoding="utf-8")
    from hokma.library import runtime
    monkeypatch.setattr(runtime, "model_ready", lambda: False)
    make_worker(db, clock).tick()
    assert _waiting(db) >= 1


def test_a_failing_model_never_stops_the_thread(db, folder, model, monkeypatch):
    fid, root, _ = folder
    clock = Clock()
    (root / "a.md").write_text("# A\n\nvolatility regimes\n", encoding="utf-8")
    from hokma.library import runtime
    monkeypatch.setattr(runtime, "embed_passages", lambda texts: (_ for _ in ()).throw(RuntimeError("boom")))
    worker = make_worker(db, clock)
    worker.tick()                                      # does not raise
    assert _waiting(db) >= 1


def test_wake_runs_a_step_now(db, folder):
    worker = folder_worker.FolderWorker(get_db=lambda: db, interval=3600.0)
    worker.start()
    try:
        fid, root, _ = folder
        time.sleep(0.5)                                       # first tick done; the next poll is an hour away
        (root / "b.md").write_text("# B\n\nsoon\n", encoding="utf-8")
        worker.request(fid)
        deadline = time.time() + 10
        while time.time() < deadline and not _docs_in(db, fid):
            time.sleep(0.1)
        assert _docs_in(db, fid)
    finally:
        worker.stop()

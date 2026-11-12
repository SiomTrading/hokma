"""The single worker thread of the Library (SPEC-010 section 4.3).

While Hokma is open, one thread walks every disk folder of every project each 60 seconds, and any folder asked for
sooner (a project was opened, "Rescan" was pressed). Folders wait in one queue and a single worker reads them one at a
time, so indexing never runs inside a request. The scan itself lives in `folder_scan`: this module only decides when
and in what order, and tells the interface what is going on.
"""
import logging
import threading
import time
from collections import deque
from typing import Callable

from hokma.db import Database, DbError, get_database

log = logging.getLogger("hokma.library")

POLL_SECONDS = 60.0


class FolderWorker:
    def __init__(self, get_db: Callable[[], Database] = get_database, *, interval: float = POLL_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._get_db = get_db
        self.interval = interval
        self._clock = clock
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._queue: deque[int] = deque()
        self._next_poll: float | None = None          # None: the first tick polls right away
        self._active: int | None = None
        self.last_pass_at: str | None = None

    # ------------------------------------------------------------------ control

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="hokma-library", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------ requests

    def request(self, folder_id: int) -> None:
        """Ask for a folder to be read soon. A folder already waiting is not queued twice."""
        with self._lock:
            if folder_id not in self._queue and folder_id != self._active:
                self._queue.append(folder_id)
        self._wake.set()

    def request_project(self, project_id: int) -> None:
        """Queue every folder of a project (opening the project)."""
        try:
            with self._get_db().read() as u:
                ids = [f.id for f in u.library.list_project_folders(project_id)]
        except DbError:
            return
        for folder_id in ids:
            self.request(folder_id)

    # ------------------------------------------------------------------ one step

    def tick(self, now: float | None = None) -> int:
        """Queue every folder when the poll is due, then read what waits. Returns how many folders were read."""
        now = self._clock() if now is None else now
        if self._next_poll is None or now >= self._next_poll:
            self._next_poll = now + self.interval
            self._queue_all()
        done = 0
        while not self._stop.is_set():
            with self._lock:
                if not self._queue:
                    break
                self._active = self._queue.popleft()
                folder_id = self._active
            try:
                from hokma.docs import folder_scan          # lazy: folder_scan imports diskfolders, which imports this module
                folder_scan.scan_folder(folder_id, self._get_db())
                done += 1
            except Exception:                              # a folder removed meanwhile or a bad file never stops the thread
                log.exception("library scan failed for folder %s", folder_id)
            finally:
                with self._lock:
                    self._active = None
        if done:
            self.last_pass_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._vectorize()
        return done

    def _vectorize(self) -> None:
        """Give a vector to every passage that has none, once the search model is on the disk. Runs on this thread, never
        inside a request. Without the model it does nothing and the next tick tries again."""
        if self._stop.is_set():
            return
        try:
            from hokma.db import vectors
            from hokma.library import runtime

            if not runtime.model_ready():
                return
            result = vectors.backfill(self._get_db(), runtime.embed_passages, runtime.model_id(), should_stop=self._stop.is_set)
            if result.error:
                log.warning("library vectors stopped: %s", result.error)
        except Exception:                                  # the model or the store failed: the thread must stay alive
            log.exception("library vectorization failed")

    def wake(self) -> None:
        """Something new waits (an upload, a download): run a step now instead of at the next poll."""
        self._wake.set()

    def _queue_all(self) -> None:
        try:
            with self._get_db().read() as u:
                ids = [int(r["id"]) for r in u.session.query("SELECT id FROM project_folders ORDER BY id")]
        except DbError:
            return
        for folder_id in ids:
            self.request(folder_id)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:                              # pragma: no cover (tick already guards each folder)
                log.exception("library worker tick failed")
            wait = self.interval
            if self._next_poll is not None:
                wait = max(0.05, min(self.interval, self._next_poll - self._clock()))
            self._wake.wait(wait)
            self._wake.clear()

    # ------------------------------------------------------------------ what the interface shows

    def queue_state(self) -> dict:
        with self._lock:
            return {"running": self.running, "queued_folders": len(self._queue), "active_folder_id": self._active,
                    "last_pass_at": self.last_pass_at}


_WORKER: FolderWorker | None = None


def get_worker() -> FolderWorker | None:
    return _WORKER


def start_worker(**kwargs) -> FolderWorker:
    global _WORKER
    if _WORKER is None or not _WORKER.running:
        _WORKER = FolderWorker(**kwargs)
        _WORKER.start()
    return _WORKER


def stop_worker() -> None:
    global _WORKER
    if _WORKER is not None:
        _WORKER.stop()
        _WORKER = None


def wake() -> None:
    """Called after a document is added. Nothing happens when the worker is not running (tests, one-off commands)."""
    if _WORKER is not None and _WORKER.running:
        _WORKER.wake()


def request_project(project_id: int) -> None:
    """Called when a project is opened. Nothing happens when the worker is not running (tests, one-off commands)."""
    if _WORKER is not None and _WORKER.running:
        _WORKER.request_project(project_id)


def status(db: Database | None = None) -> dict:
    """`GET /api/library/status`: what is waiting to be read, how many passages wait for a vector and which folder is
    being read now."""
    from hokma.db import vectors
    from hokma.docs import folder_scan, ingest

    database = ingest._db(db)
    worker = get_worker()
    state = worker.queue_state() if worker is not None else {
        "running": False, "queued_folders": 0, "active_folder_id": None, "last_pass_at": None}
    with database.read() as u:
        reading = int(u.session.scalar("SELECT COUNT(*) FROM documents WHERE status = 'indexing'") or 0)
        passages = int(u.session.scalar("SELECT COUNT(*) FROM doc_chunks") or 0)
        waiting = int(u.session.scalar(
            "SELECT COUNT(*) FROM doc_chunks c WHERE NOT EXISTS (SELECT 1 FROM doc_chunk_vectors v WHERE v.chunk_id = c.id)")
            or 0)
        active = None
        if state["active_folder_id"] is not None:
            row = u.session.one("SELECT id, project_id, path FROM project_folders WHERE id = :i",
                                {"i": state["active_folder_id"]})
            if row is not None:
                active = {"id": int(row["id"]), "project_id": int(row["project_id"]), "path": row["path"]}
    return {"worker": state["running"], "files_to_read": reading, "queued_folders": state["queued_folders"],
            "passages": passages, "passages_to_vectorize": waiting, "max_passages": folder_scan.MAX_PASSAGES,
            "active_folder": active, "last_pass_at": state["last_pass_at"],
            "vectors": vectors.current_status(database).public()}

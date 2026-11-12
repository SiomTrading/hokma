"""Vector store of the Library: one API, three stores (SPEC-010 section 4.5).

| Engine                    | Store             | Where the vector lives                                      |
|---------------------------|-------------------|-------------------------------------------------------------|
| SQLite (default)          | `SqliteVecStore`  | virtual table `doc_chunk_vec` (`sqlite-vec`, `vec0`, rowid = chunk id) |
| Postgres with `pgvector`  | `PgVectorStore`   | column `doc_chunk_vectors.embedding vector(384)`            |
| Postgres without it       | `ByteaVectorStore`| column `doc_chunk_vectors.vec BYTEA` + `numpy` in memory    |

`doc_chunk_vectors` (migration 0008) records which passage has a vector and from which model on every engine; the
vector itself is stored by the engine specific store. Vectors are unit length `float32`, so cosine similarity and the
dot product agree. `numpy` is imported only by the Postgres fallback; SQLite never needs it.

Searching is exact, never approximate: SQLite scans the vectors of the eligible passages with
`vec_distance_cosine` (the `vec0` nearest-neighbour query caps `k` at 4096 and filters after choosing, which would
drop results for a narrow filter), Postgres orders by `<=>` without an approximate index, and the fallback multiplies
the eligible rows of a cached matrix.

Use `prepare(db)` once at start (creates what the engine needs and reports an unusable setup as a status, never as a
traceback), then `open_store(session)` inside a unit of work.
"""
from __future__ import annotations

import logging
import math
import threading
from abc import ABC, abstractmethod
from array import array
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Iterable, Sequence

from hokma.db import vecext
from hokma.db.backend import Session
from hokma.db.errors import DbError, ValidationError
from hokma.db.timeutil import utcnow
from hokma.i18n import t

if TYPE_CHECKING:
    from hokma.db.database import Database

DIM = 384
DEFAULT_CANDIDATES = 50
STORE_SQLITE_VEC, STORE_PGVECTOR, STORE_BYTEA = "sqlite-vec", "pgvector", "bytea"
NOT_PREPARED = "not_prepared"                  # prepare() has not run on this database
SCHEMA_FAILED = "schema_failed"                # the Postgres server refused the vector column; retry may help

log = logging.getLogger("hokma.vectors")


class VectorUnavailable(DbError):
    """The vector store cannot be used here. `code` is the catalog suffix (`library.vectors.<code>`)."""

    def __init__(self, code: str, detail: str = "", retry: bool = False) -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
        self.retry = retry


@dataclass(frozen=True)
class VectorHit:
    chunk_id: int
    similarity: float                           # cosine similarity, 1.0 = same direction


@dataclass(frozen=True)
class VectorStatus:
    """What `prepare` found: the store in use, or why there is none (shown to the student as a plain message)."""

    ok: bool
    store: str | None = None
    code: str | None = None
    detail: str = ""
    retry: bool = False

    @property
    def message(self) -> str | None:
        if self.ok:
            return None
        return {
            vecext.NO_LOAD_EXTENSION: lambda: t("library.vectors.no_load_extension"),
            vecext.MODULE_MISSING: lambda: t("library.vectors.module_missing"),
            vecext.LOAD_FAILED: lambda: t("library.vectors.load_failed"),
            NOT_PREPARED: lambda: t("library.vectors.not_prepared"),
        }.get(self.code or "", lambda: t("library.vectors.schema_failed"))()

    def public(self) -> dict:
        return {"ok": self.ok, "store": self.store, "error": self.code, "message": self.message, "retry": self.retry}


# ------------------------------------------------------------------------------------------------ helpers
def _ints(values: Iterable[int]) -> str:
    """A comma list of integers for an `IN (...)`: every value is cast, so nothing else can reach the SQL."""
    return ",".join(str(int(v)) for v in values)


def normalize(vector: Sequence[float]) -> array:
    """A unit length `float32` copy of `vector`; wrong size or a zero vector is a ValidationError."""
    if len(vector) != DIM:
        raise ValidationError(f"a vector has {DIM} numbers, got {len(vector)}")
    values = [float(x) for x in vector]
    norm = math.sqrt(sum(x * x for x in values))
    if not norm or not math.isfinite(norm):
        raise ValidationError("a vector cannot be all zeros or contain non-finite numbers")
    return array("f", (x / norm for x in values))


def eligible_where(library_ids: Sequence[int], folder_ids: Sequence[int], library_folder_ids: Sequence[int] | None) -> str | None:
    """The WHERE fragment (over `d` = documents) of the passages a search may return; None when nothing qualifies.

    A passage qualifies when its document is ready and either lives in one of `library_ids` (and, when
    `library_folder_ids` is given, in one of those Library folders; subfolders already expanded by the caller) or was
    read from one of the disk folders `folder_ids`."""
    parts = []
    libraries = _ints(library_ids)
    if libraries:
        part = f"d.library_id IN ({libraries})"
        if library_folder_ids is not None:
            inside = _ints(library_folder_ids)
            part += f" AND d.library_folder_id IN ({inside})" if inside else " AND 1 = 0"
        parts.append(f"({part})")
    disk = _ints(folder_ids)
    if disk:
        parts.append(f"d.folder_id IN ({disk})")
    if not parts:
        return None
    return "d.status = 'ready' AND (" + " OR ".join(parts) + ")"


# ------------------------------------------------------------------------------------------------ the API
class VectorStore(ABC):
    """What every store does. All methods run on the session they were opened with, inside its transaction."""

    name: str

    def __init__(self, session: Session) -> None:
        self.s = session

    @abstractmethod
    def ensure_schema(self) -> None:
        """Create what this store needs (idempotent)."""

    @abstractmethod
    def lacks_vector(self, alias: str = "c") -> str:
        """SQL boolean over a passage `alias` (doc_chunks row): true when it has no usable vector for `:model`."""

    @abstractmethod
    def _write(self, rows: list[tuple[int, array]], model_id: str) -> None: ...

    @abstractmethod
    def search(self, query: Sequence[float], *, library_ids: Sequence[int] = (), folder_ids: Sequence[int] = (),
               library_folder_ids: Sequence[int] | None = None, limit: int = DEFAULT_CANDIDATES,
               model_id: str | None = None) -> list[VectorHit]:
        """The `limit` eligible passages most similar to `query`, best first (ties by passage id)."""

    def put(self, items: Sequence[tuple[int, Sequence[float]]], model_id: str) -> int:
        """Store (or replace) the vector of each `(chunk_id, vector)`; passages that no longer exist are skipped.
        Returns how many were stored."""
        if not model_id:
            raise ValidationError("model_id is required")
        rows = [(int(chunk), normalize(vector)) for chunk, vector in items]
        if not rows:
            return 0
        present = {int(r["id"]) for r in self.s.query(
            f"SELECT id FROM doc_chunks WHERE id IN ({_ints(c for c, _ in rows)})")}
        rows = [(c, v) for c, v in rows if c in present]
        if rows:
            self.ensure_schema()
            self._write(rows, model_id)
        return len(rows)

    def pending(self, limit: int, model_id: str) -> list[int]:
        """Ids of up to `limit` passages that still need a vector for `model_id`, oldest first (failed documents are
        left alone)."""
        rows = self.s.query(
            "SELECT c.id FROM doc_chunks c JOIN documents d ON d.id = c.document_id "
            f"WHERE d.status <> 'failed' AND {self.lacks_vector('c')} ORDER BY c.id LIMIT :n",
            {"model": model_id, "n": max(1, int(limit))})
        return [int(r["id"]) for r in rows]

    def count_pending(self, model_id: str) -> int:
        return int(self.s.scalar(
            "SELECT COUNT(*) FROM doc_chunks c JOIN documents d ON d.id = c.document_id "
            f"WHERE d.status <> 'failed' AND {self.lacks_vector('c')}", {"model": model_id}) or 0)

    def mark_ready(self, document_ids: Iterable[int], model_id: str) -> int:
        """Documents still `indexing` whose every passage now has a vector become `ready`. Returns how many."""
        ids = _ints(document_ids)
        if not ids:
            return 0
        return self.s.execute(
            "UPDATE documents SET status = 'ready', status_detail = NULL, indexed_at = :now "
            f"WHERE id IN ({ids}) AND status = 'indexing' AND NOT EXISTS ("
            f"SELECT 1 FROM doc_chunks c WHERE c.document_id = documents.id AND {self.lacks_vector('c')})",
            {"now": utcnow(), "model": model_id})

    def prune(self) -> int:
        """Remove stored vectors whose passage is gone. Returns how many were removed."""
        return 0


class SqliteVecStore(VectorStore):
    name = STORE_SQLITE_VEC

    def __init__(self, session: Session) -> None:
        if not getattr(session, "vec_loaded", False):
            support = vecext.check_support()
            raise VectorUnavailable(support.code or vecext.LOAD_FAILED, support.detail, support.retry)
        super().__init__(session)

    def ensure_schema(self) -> None:
        self.s.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS doc_chunk_vec USING vec0("
                       f"embedding float[{DIM}] distance_metric=cosine)")

    def lacks_vector(self, alias: str = "c") -> str:
        if not self.s.table_exists("doc_chunk_vec"):                  # nothing stored yet: every passage lacks one
            return "1 = 1"
        return (f"(NOT EXISTS (SELECT 1 FROM doc_chunk_vectors cv WHERE cv.chunk_id = {alias}.id AND cv.model_id = :model)"
                f" OR NOT EXISTS (SELECT 1 FROM doc_chunk_vec v WHERE v.rowid = {alias}.id))")

    def _write(self, rows: list[tuple[int, array]], model_id: str) -> None:
        ids = _ints(c for c, _ in rows)
        self.s.execute(f"DELETE FROM doc_chunk_vec WHERE rowid IN ({ids})")
        self.s.execute_many("INSERT INTO doc_chunk_vec(rowid, embedding) VALUES (:id, :vec)",
                            [{"id": c, "vec": v.tobytes()} for c, v in rows])
        self.s.execute_many(
            "INSERT INTO doc_chunk_vectors (chunk_id, model_id, dim) VALUES (:id, :model, :dim) "
            "ON CONFLICT (chunk_id) DO UPDATE SET model_id = excluded.model_id, dim = excluded.dim",
            [{"id": c, "model": model_id, "dim": DIM} for c, _ in rows])

    def prune(self) -> int:
        if not self.s.table_exists("doc_chunk_vec"):
            return 0
        orphans = [int(r["rowid"]) for r in self.s.query(
            "SELECT v.rowid AS rowid FROM doc_chunk_vec v WHERE NOT EXISTS ("
            "SELECT 1 FROM doc_chunk_vectors cv WHERE cv.chunk_id = v.rowid)")]
        for start in range(0, len(orphans), 500):
            self.s.execute(f"DELETE FROM doc_chunk_vec WHERE rowid IN ({_ints(orphans[start:start + 500])})")
        return len(orphans)

    def search(self, query, *, library_ids=(), folder_ids=(), library_folder_ids=None, limit=DEFAULT_CANDIDATES,
               model_id=None) -> list[VectorHit]:
        where = eligible_where(library_ids, folder_ids, library_folder_ids)
        if where is None or not self.s.table_exists("doc_chunk_vec"):
            return []
        params = {"q": normalize(query).tobytes(), "n": max(1, int(limit))}
        model = ""
        if model_id:
            model = " AND cv.model_id = :model"
            params["model"] = model_id
        rows = self.s.query(
            "SELECT c.id AS chunk_id, vec_distance_cosine(v.embedding, :q) AS distance "
            "FROM doc_chunks c JOIN documents d ON d.id = c.document_id "
            "JOIN doc_chunk_vectors cv ON cv.chunk_id = c.id "
            "JOIN doc_chunk_vec v ON v.rowid = c.id "
            f"WHERE {where}{model} ORDER BY distance, c.id LIMIT :n", params)
        return [VectorHit(int(r["chunk_id"]), 1.0 - float(r["distance"])) for r in rows]


class PgVectorStore(VectorStore):
    name = STORE_PGVECTOR

    def ensure_schema(self) -> None:
        self.s.execute(f"ALTER TABLE doc_chunk_vectors ADD COLUMN IF NOT EXISTS embedding vector({DIM})")

    def lacks_vector(self, alias: str = "c") -> str:
        return (f"NOT EXISTS (SELECT 1 FROM doc_chunk_vectors cv WHERE cv.chunk_id = {alias}.id "
                "AND cv.model_id = :model AND cv.embedding IS NOT NULL)")

    @staticmethod
    def literal(vector: Sequence[float]) -> str:
        return "[" + ",".join(repr(float(x)) for x in vector) + "]"

    def _write(self, rows: list[tuple[int, array]], model_id: str) -> None:
        self.s.execute_many(
            "INSERT INTO doc_chunk_vectors (chunk_id, model_id, dim, embedding) "
            "VALUES (:id, :model, :dim, CAST(:vec AS vector)) "
            "ON CONFLICT (chunk_id) DO UPDATE SET model_id = EXCLUDED.model_id, dim = EXCLUDED.dim, "
            "embedding = EXCLUDED.embedding",
            [{"id": c, "model": model_id, "dim": DIM, "vec": self.literal(v)} for c, v in rows])

    def search(self, query, *, library_ids=(), folder_ids=(), library_folder_ids=None, limit=DEFAULT_CANDIDATES,
               model_id=None) -> list[VectorHit]:
        where = eligible_where(library_ids, folder_ids, library_folder_ids)
        if where is None:
            return []
        params = {"q": self.literal(normalize(query)), "n": max(1, int(limit))}
        model = ""
        if model_id:
            model = " AND cv.model_id = :model"
            params["model"] = model_id
        rows = self.s.query(
            "SELECT c.id AS chunk_id, 1 - (cv.embedding <=> CAST(:q AS vector)) AS similarity "
            "FROM doc_chunk_vectors cv JOIN doc_chunks c ON c.id = cv.chunk_id "
            "JOIN documents d ON d.id = c.document_id "
            f"WHERE cv.embedding IS NOT NULL AND {where}{model} "
            "ORDER BY cv.embedding <=> CAST(:q AS vector), c.id LIMIT :n", params)
        return [VectorHit(int(r["chunk_id"]), float(r["similarity"])) for r in rows]


@dataclass
class _Matrix:
    signature: tuple
    ids: object                                  # numpy int64 array, ascending
    data: object                                 # numpy float32 matrix, one row per id


_matrices: dict[tuple, _Matrix] = {}
_matrices_lock = threading.Lock()


class ByteaVectorStore(VectorStore):
    """Postgres without `pgvector`: `float32` bytes in `doc_chunk_vectors.vec`, multiplied in memory with `numpy`.
    One matrix per library is cached (about 77 MB for 50,000 passages) and rebuilt when the library's vectors
    change (count or highest passage id) or this process writes a vector."""

    name = STORE_BYTEA

    def ensure_schema(self) -> None:
        self.s.execute("ALTER TABLE doc_chunk_vectors ADD COLUMN IF NOT EXISTS vec BYTEA")

    def lacks_vector(self, alias: str = "c") -> str:
        return (f"NOT EXISTS (SELECT 1 FROM doc_chunk_vectors cv WHERE cv.chunk_id = {alias}.id "
                "AND cv.model_id = :model AND cv.vec IS NOT NULL)")

    def _db_key(self) -> str:
        info = getattr(getattr(self.s, "raw", None), "info", None)
        if info is not None and hasattr(info, "dbname"):
            return f"{info.host}:{info.port}/{info.dbname}"
        return str(self.s.scalar("SELECT file FROM pragma_database_list WHERE name = 'main'") or "")

    def _forget(self) -> None:
        key = self._db_key()
        with _matrices_lock:
            for entry in [k for k in _matrices if k[0] == key]:
                del _matrices[entry]

    def _write(self, rows: list[tuple[int, array]], model_id: str) -> None:
        self.s.execute_many(
            "INSERT INTO doc_chunk_vectors (chunk_id, model_id, dim, vec) VALUES (:id, :model, :dim, :vec) "
            "ON CONFLICT (chunk_id) DO UPDATE SET model_id = EXCLUDED.model_id, dim = EXCLUDED.dim, vec = EXCLUDED.vec",
            [{"id": c, "model": model_id, "dim": DIM, "vec": v.tobytes()} for c, v in rows])
        self._forget()

    def _matrix(self, library_id: int, model_id: str | None):
        import numpy as np

        model = " AND cv.model_id = :model" if model_id else ""
        params = {"l": library_id} | ({"model": model_id} if model_id else {})
        base = ("FROM doc_chunk_vectors cv JOIN doc_chunks c ON c.id = cv.chunk_id "
                "JOIN documents d ON d.id = c.document_id "
                f"WHERE d.library_id = :l AND cv.vec IS NOT NULL{model}")
        head = self.s.one(f"SELECT COUNT(*) AS n, COALESCE(MAX(cv.chunk_id), 0) AS top {base}", params)
        signature = (int(head["n"]), int(head["top"]), model_id)
        key = (self._db_key(), library_id, model_id)
        with _matrices_lock:
            cached = _matrices.get(key)
        if cached is not None and cached.signature == signature:
            return cached
        rows = self.s.query(f"SELECT cv.chunk_id AS id, cv.vec AS vec {base} ORDER BY cv.chunk_id", params)
        ids = np.fromiter((int(r["id"]) for r in rows), dtype=np.int64, count=len(rows))
        data = (np.frombuffer(b"".join(bytes(r["vec"]) for r in rows), dtype=np.float32).reshape(len(rows), DIM)
                if rows else np.zeros((0, DIM), dtype=np.float32))
        entry = _Matrix(signature, ids, data)
        with _matrices_lock:
            _matrices[key] = entry
        return entry

    def search(self, query, *, library_ids=(), folder_ids=(), library_folder_ids=None, limit=DEFAULT_CANDIDATES,
               model_id=None) -> list[VectorHit]:
        import numpy as np

        where = eligible_where(library_ids, folder_ids, library_folder_ids)
        if where is None:
            return []
        params = {"model": model_id} if model_id else {}
        model = " AND cv.model_id = :model" if model_id else ""
        eligible = self.s.query(
            "SELECT c.id AS chunk_id, d.library_id AS library_id FROM doc_chunk_vectors cv "
            "JOIN doc_chunks c ON c.id = cv.chunk_id JOIN documents d ON d.id = c.document_id "
            f"WHERE cv.vec IS NOT NULL AND {where}{model}", params)
        by_library: dict[int, list[int]] = {}
        for row in eligible:
            by_library.setdefault(int(row["library_id"]), []).append(int(row["chunk_id"]))
        q = np.frombuffer(normalize(query).tobytes(), dtype=np.float32)
        found_ids: list[np.ndarray] = []
        found_scores: list[np.ndarray] = []
        for library_id, chunk_ids in by_library.items():
            matrix = self._matrix(library_id, model_id)
            keep = np.isin(matrix.ids, np.asarray(chunk_ids, dtype=np.int64))
            if keep.any():
                found_ids.append(matrix.ids[keep])
                found_scores.append(matrix.data[keep] @ q)
        if not found_ids:
            return []
        ids = np.concatenate(found_ids)
        scores = np.concatenate(found_scores).astype(np.float64)
        order = np.lexsort((ids, -scores))[:max(1, int(limit))]
        return [VectorHit(int(ids[i]), float(scores[i])) for i in order]


# ------------------------------------------------------------------------------------------------ opening
def _has_column(session: Session, column: str) -> bool:
    return session.scalar(
        "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
        "AND table_name = 'doc_chunk_vectors' AND column_name = :c", {"c": column}) is not None


def open_store(session: Session) -> VectorStore:
    """The store of this session's engine, without changing the schema. `VectorUnavailable` when it cannot be used
    (SQLite without the extension; Postgres where `prepare` has not run)."""
    if session.engine == "sqlite":
        return SqliteVecStore(session)
    if _has_column(session, "embedding"):
        return PgVectorStore(session)
    if _has_column(session, "vec"):
        return ByteaVectorStore(session)
    raise VectorUnavailable(NOT_PREPARED, "run prepare() before the first use", True)


def _prepare_postgres(session: Session) -> VectorStore:
    """Choose and create the Postgres store: `pgvector` when the server has (or lets us create) the extension,
    otherwise the `BYTEA` fallback. The attempt runs under a savepoint, so a refusal leaves the transaction usable."""
    if _has_column(session, "embedding"):
        return PgVectorStore(session)
    session.execute("SAVEPOINT hk_vector")
    try:
        session.execute("CREATE EXTENSION IF NOT EXISTS vector")
        store = PgVectorStore(session)
        store.ensure_schema()
        session.execute("RELEASE SAVEPOINT hk_vector")
        return store
    except Exception as exc:                                          # noqa: BLE001  (no extension on this server)
        session.execute("ROLLBACK TO SAVEPOINT hk_vector")
        session.execute("RELEASE SAVEPOINT hk_vector")
        log.info("pgvector is not available on this server (%s); using the BYTEA fallback", type(exc).__name__)
    store = ByteaVectorStore(session)
    store.ensure_schema()
    return store


_last: tuple[tuple, VectorStatus] | None = None


def _key(db: "Database") -> tuple:
    return (db.engine, str(db.path) if db.path is not None else repr(db.backend))


def prepare(db: "Database") -> VectorStatus:
    """Detect and create the vector store of `db`'s engine; log which one runs. Never raises: a setup that cannot
    work comes back as a status with a plain message (and `retry` when trying again may fix it)."""
    global _last
    try:
        if db.engine == "sqlite":
            support = vecext.check_support()
            if not support.ok:
                status = VectorStatus(False, None, support.code, support.detail, support.retry)
                log.error("sqlite-vec cannot be used: %s (%s)", support.code, support.detail)
            else:
                with db.write() as u:
                    store = SqliteVecStore(u.session)
                    store.ensure_schema()
                    store.prune()
                status = VectorStatus(True, STORE_SQLITE_VEC)
        else:
            with db.write() as u:
                store = _prepare_postgres(u.session)
            status = VectorStatus(True, store.name)
    except VectorUnavailable as exc:
        status = VectorStatus(False, None, exc.code, exc.detail, exc.retry)
    except DbError as exc:
        status = VectorStatus(False, None, SCHEMA_FAILED, str(exc)[:200], True)
    except Exception as exc:                                          # noqa: BLE001  (the server refused the DDL)
        status = VectorStatus(False, None, SCHEMA_FAILED, f"{type(exc).__name__}: {str(exc)[:160]}", True)
    if status.ok:
        log.info("vector store: %s", status.store)
    _last = (_key(db), status)
    return status


def current_status(db: "Database") -> VectorStatus:
    """The result of the last `prepare`, running it first when the app has not yet."""
    return _last[1] if _last is not None and _last[0] == _key(db) else prepare(db)


def reset_status() -> None:
    """Forget the cached status (tests, and a changed engine)."""
    global _last
    _last = None


# ------------------------------------------------------------------------------------------------ backfill
@dataclass
class BackfillResult:
    vectorized: int = 0
    documents_ready: int = 0
    stopped: bool = False
    error: str | None = None
    batches: int = 0


Embed = Callable[[list[str]], Sequence[Sequence[float]]]


def backfill(db: "Database", embed: Embed, model_id: str, *, batch: int = 16,
             should_stop: Callable[[], bool] | None = None, max_batches: int | None = None) -> BackfillResult:
    """Compute the vectors of passages that have none, `batch` at a time, and turn a document `ready` when its last
    passage is done. Resumable: every batch commits on its own, and the next one starts from what is still missing.

    `embed` receives the passage texts and returns one vector each (the caller adds the model's text prefix). A
    failure of `embed` stops the run with `error` set; nothing from that batch is stored."""
    result = BackfillResult()
    while max_batches is None or result.batches < max_batches:
        if should_stop is not None and should_stop():
            result.stopped = True
            break
        try:
            with db.read() as u:
                store = open_store(u.session)
                ids = store.pending(batch, model_id)
                if not ids:
                    break
                rows = u.session.query(
                    f"SELECT id, document_id, body FROM doc_chunks WHERE id IN ({_ints(ids)}) ORDER BY id")
        except VectorUnavailable as exc:
            result.error = exc.code
            break
        try:
            vectors = embed([r["body"] for r in rows])
            if len(vectors) != len(rows):
                raise ValueError(f"embed returned {len(vectors)} vectors for {len(rows)} passages")
            items = [(int(r["id"]), v) for r, v in zip(rows, vectors)]
            with db.write() as u:
                store = open_store(u.session)
                result.vectorized += store.put(items, model_id)
                result.documents_ready += store.mark_ready({int(r["document_id"]) for r in rows}, model_id)
        except Exception as exc:                                      # noqa: BLE001  (the model or the store failed)
            result.error = f"{type(exc).__name__}: {str(exc)[:200]}"
            break
        result.batches += 1
    return result

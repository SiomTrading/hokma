"""Copy the whole database from one engine to the other ("Change and migrate", D11).

One transaction on the destination: build the schema, copy every table in dependency order, rebuild the identity
counters, create the triggers, check the counts against the source, commit. Any failure rolls the transaction
back, so the destination is either complete or untouched. The source is only read.

- Triggers are created AFTER the data goes in, inside the same transaction. Otherwise the event trigger would
  write a second `task_events` row for every copied task and the append-only triggers would refuse nothing useful.
- SQLite destination: the new file is built next to the final one and moved into place only after the commit; a
  file that was already there is first copied to the backups folder.
- Postgres destination: Hokma tables that are already there are dropped and rebuilt inside the transaction, but
  only when `replace=True`; otherwise a destination that holds data is refused.
"""
from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hokma.db import dbconfig
from hokma.db.backend import Backend, Session, SqliteBackend
from hokma.db.errors import ConnectionFailed, DbError, MigrationError
from hokma.db.migrate import discover, render
from hokma.db.paths import backups_dir
from hokma.db.timeutil import utcnow

REFERENCE_TABLES = ("schema_version", "status_transitions")   # created and filled by the migrations themselves
BATCH = 500
APP_ROLE, AGENT_ROLE = "hokma_app", "hokma_agent"
_CREATE_TABLE = re.compile(r"^CREATE TABLE (\w+)\s*\(", re.IGNORECASE)
_REFERENCE = re.compile(r"^\s*(\w+)\s+.*?REFERENCES\s+(\w+)\s*\(", re.IGNORECASE)
_ALTER_REFERENCE = re.compile(r"^ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)\b.*?REFERENCES\s+(\w+)\s*\(",
                              re.IGNORECASE | re.DOTALL)
_IDENTITY = re.compile(r"^\s*id\s+\{PK\}", re.IGNORECASE)
_DEFERRED = re.compile(r"^(CREATE\s+(OR\s+REPLACE\s+)?(TRIGGER|FUNCTION)|DROP\s+(TRIGGER|FUNCTION))\b", re.IGNORECASE)
_FUNCTION = re.compile(r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+(\w+)\s*\(", re.IGNORECASE)
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


class TransferError(DbError):
    """The copy failed and nothing was changed. `code` is a catalog key suffix; `detail` never holds a password."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass
class TableInfo:
    name: str
    identity: bool = False
    self_refs: list[str] = field(default_factory=list)
    parents: set[str] = field(default_factory=set)


@dataclass
class TransferResult:
    counts: dict[str, int]
    schema_version: int
    roles: str = "skipped"                  # created | exists | skipped (no permission or not Postgres)
    previous_copy: Path | None = None       # copy of a SQLite file that was replaced

    @property
    def rows(self) -> int:
        return sum(self.counts.values())


def table_plan(script: str) -> list[TableInfo]:
    """Tables of the schema in dependency order (parents first), read from the DDL (SQLite-rendered)."""
    tables: dict[str, TableInfo] = {}
    for statement in SqliteBackend(Path("unused")).split_statements(script):
        added = _ALTER_REFERENCE.match(statement)
        if added and added.group(1) in tables:                     # a column added later that points at a table
            if added.group(3) == added.group(1):
                tables[added.group(1)].self_refs.append(added.group(2))
            else:
                tables[added.group(1)].parents.add(added.group(3))
            continue
        head = _CREATE_TABLE.match(statement)
        if not head:
            continue
        info = TableInfo(head.group(1))
        for line in statement.splitlines()[1:]:
            if _IDENTITY.match(line):
                info.identity = True
            ref = _REFERENCE.match(line)
            if ref:
                if ref.group(2) == info.name:
                    info.self_refs.append(ref.group(1))
                else:
                    info.parents.add(ref.group(2))
        tables[info.name] = info
    ordered: list[TableInfo] = []
    placed: set[str] = set()
    pending = list(tables.values())
    while pending:
        ready = [t for t in pending if t.parents <= placed]
        if not ready:
            raise TransferError("failed", "circular references between tables")
        for table in ready:
            ordered.append(table)
            placed.add(table.name)
            pending.remove(table)
    return ordered


class _RawMarks(Backend):
    """Renders the SQLite sections but leaves {PK} and {TS} in place, so the plan can see which tables have an
    identity key (no connection)."""

    engine = "sqlite"
    marks: dict[str, str] = {}

    def connect(self):                                             # pragma: no cover
        raise NotImplementedError

    def split_statements(self, script):                            # pragma: no cover
        raise NotImplementedError

    def exists(self) -> bool:                                      # pragma: no cover
        return False

    def backup(self, destination) -> None:                         # pragma: no cover
        raise NotImplementedError


def _plan(files) -> list[TableInfo]:
    return table_plan("\n".join(s for _v, s in _script(_RawMarks(), files)))


def _script(backend: Backend, files) -> list[tuple[int, str]]:
    return [(version, render(path.read_text(encoding="utf-8"), backend)) for version, _name, path in files]


def _count(session: Session, table: str) -> int:
    return int(session.scalar(f"SELECT count(*) FROM {table}") or 0)


def _has_data(session: Session, tables: list[TableInfo]) -> bool:
    for table in tables:
        if table.name in REFERENCE_TABLES or not session.table_exists(table.name):
            continue
        if _count(session, table.name):
            return True
    return False


def _drop_hokma_objects(session: Session, tables: list[TableInfo], full_script: str) -> None:
    """Postgres only: remove the Hokma tables and functions so the schema is rebuilt from the migrations."""
    existing = [t.name for t in reversed(tables) if session.table_exists(t.name)]
    if existing:
        session.execute(f"DROP TABLE IF EXISTS {', '.join(existing)} CASCADE")
    for name in dict.fromkeys(_FUNCTION.findall(full_script)):
        if _IDENT.match(name):
            session.execute(f"DROP FUNCTION IF EXISTS {name}() CASCADE")


def _apply_schema(session: Session, backend: Backend, files) -> list[str]:
    """Run every migration except triggers and functions; return those, in order, to run after the copy."""
    deferred: list[str] = []
    for version, script in _script(backend, files):
        for statement in backend.split_statements(script):
            if _DEFERRED.match(statement):
                deferred.append(statement)
            else:
                session.execute(statement)
        session.execute("INSERT INTO schema_version (version) VALUES (:v)", {"v": version})
    return deferred


def _referenced_first(rows: list[dict[str, Any]], columns: list[str]) -> list[dict[str, Any]]:
    """Order rows of a self-referencing table so every row comes after the row it points at (CHECKs such as
    'superseded needs superseded_by' forbid inserting it empty first and filling it later)."""
    by_id = {row["id"] for row in rows}
    placed: set[Any] = set()
    ordered: list[dict[str, Any]] = []
    pending = rows
    while pending:
        ready = [r for r in pending if all(r[c] is None or r[c] in placed or r[c] not in by_id for c in columns)]
        if not ready:
            raise TransferError("failed", "circular references between rows")
        ordered.extend(ready)
        placed.update(r["id"] for r in ready)
        pending = [r for r in pending if r["id"] not in placed]
    return ordered


def _copy_table(source: Session, target: Session, table: TableInfo) -> int:
    rows = source.query(f"SELECT * FROM {table.name} ORDER BY 1")
    if not rows:
        return 0
    columns = list(rows[0])
    for column in columns:
        Session._check_name(column)
    if table.self_refs:
        rows = _referenced_first(rows, table.self_refs)
    keyword = "OVERRIDING SYSTEM VALUE " if table.identity and target.engine == "postgres" else ""
    sql = (f"INSERT INTO {table.name} ({', '.join(columns)}) {keyword}"
           f"VALUES ({', '.join(':' + c for c in columns)})")
    for start in range(0, len(rows), BATCH):
        target.execute_many(sql, rows[start:start + BATCH])
    return len(rows)


def _reset_identity(target: Session, table: TableInfo) -> None:
    if table.identity and target.engine == "postgres":
        target.execute(f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), "
                       f"COALESCE((SELECT MAX(id) FROM {table.name}), 0) + 1, false)")


def _verify(source: Session, target: Session, tables: list[TableInfo]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in tables:
        a, b = _count(source, table.name), _count(target, table.name)
        if a != b:
            raise TransferError("count_mismatch", f"{table.name}: {a} rows in the source, {b} in the destination")
        if table.identity:
            ids_a = source.scalar(f"SELECT COALESCE(SUM(id), 0) FROM {table.name}")
            ids_b = target.scalar(f"SELECT COALESCE(SUM(id), 0) FROM {table.name}")
            if int(ids_a) != int(ids_b):
                raise TransferError("count_mismatch", f"{table.name}: ids differ")
        counts[table.name] = a
    return counts


def transfer(source: Backend, target: Backend, *, replace: bool = False) -> TransferResult:
    """Copy `source` into `target` (different engines). Raises TransferError; on failure `target` is unchanged."""
    if source.engine == target.engine:
        raise TransferError("same_engine")
    files = discover()
    latest = files[-1][0] if files else 0
    tables = _plan(files)
    pg_script = "\n".join(s for _v, s in _script(_PG_MARKS, files))

    try:
        src = source.connect()
    except ConnectionFailed as exc:
        raise TransferError("source_unreachable", str(exc)) from None
    previous: Path | None = None
    work_target: Backend = target
    work_path: Path | None = None
    done = False
    try:
        src.begin(write=False)
        if not src.table_exists("schema_version"):
            raise TransferError("source_empty")
        version = int(src.scalar("SELECT COALESCE(MAX(version), 0) FROM schema_version") or 0)
        if version != latest:
            raise TransferError("source_outdated", f"{version} != {latest}")

        if isinstance(target, SqliteBackend):
            if target.exists() and not replace:
                probe = target.connect()
                try:
                    if probe.table_exists("schema_version") and _has_data(probe, tables):
                        raise TransferError("target_not_empty")
                finally:
                    probe.close()
            work_path = target.path.with_name(target.path.name + ".migrating")
            for leftover in (work_path, Path(str(work_path) + "-wal"), Path(str(work_path) + "-shm")):
                leftover.unlink(missing_ok=True)
            work_target = SqliteBackend(work_path)

        try:
            dst = work_target.connect()
        except ConnectionFailed as exc:
            raise TransferError("target_unreachable", str(exc)) from None
        try:
            dst.begin(write=True)
            if work_target.engine == "postgres":
                if dst.table_exists("schema_version") or any(dst.table_exists(t.name) for t in tables):
                    if _has_data(dst, tables) and not replace:
                        raise TransferError("target_not_empty")
                    _drop_hokma_objects(dst, tables, pg_script)
            deferred = _apply_schema(dst, work_target, files)
            for table in tables:
                if table.name in REFERENCE_TABLES:
                    continue
                _copy_table(src, dst, table)
                _reset_identity(dst, table)
            for statement in deferred:
                dst.execute(statement)
            if dst.table_exists("profile"):
                dst.execute("UPDATE profile SET database_engine = :e WHERE id = 1", {"e": target.engine})
            counts = _verify(src, dst, tables)
            dst.commit()
        except BaseException:
            dst.rollback()
            raise
        finally:
            dst.close()

        if work_path is not None:
            _checkpoint(work_path)
            assert isinstance(target, SqliteBackend)
            if target.exists():
                previous = backups_dir(target.path) / f"hokma-before-switch-{utcnow().replace('-', '').replace(':', '').replace(' ', '-')}.db"
                target.backup(previous)
            os.replace(work_path, target.path)
            for suffix in ("-wal", "-shm"):
                Path(str(target.path) + suffix).unlink(missing_ok=True)
        done = True
    except TransferError:
        raise
    except MigrationError as exc:
        raise TransferError("failed", dbconfig.redact(str(exc))) from None
    except Exception as exc:
        raise TransferError("failed", _safe_text(exc, target)) from None
    finally:
        src.close()
        if work_path is not None and not done:
            for leftover in (work_path, Path(str(work_path) + "-wal"), Path(str(work_path) + "-shm")):
                leftover.unlink(missing_ok=True)

    roles = "skipped"
    if target.engine == "postgres":
        roles = provision_roles(target)
    return TransferResult(counts=counts, schema_version=latest, roles=roles, previous_copy=previous)


def _checkpoint(path: Path) -> None:
    """Fold the WAL into the main file so moving only the .db file keeps every row."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode = DELETE")
    finally:
        conn.close()


def _safe_text(exc: BaseException, target: Backend) -> str:
    url = getattr(target, "url", None)
    return dbconfig.redact(str(exc).strip(), url)


class _PgMarks(Backend):
    """Only to render the Postgres sections of the migrations (no connection)."""

    engine = "postgres"
    marks = {"{PK}": "BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY", "{TS}": "TIMESTAMPTZ"}

    def connect(self):                                             # pragma: no cover
        raise NotImplementedError

    def split_statements(self, script):                            # pragma: no cover
        raise NotImplementedError

    def exists(self) -> bool:                                      # pragma: no cover
        return False

    def backup(self, destination) -> None:                         # pragma: no cover
        raise NotImplementedError


_PG_MARKS = _PgMarks()


def provision_roles(target: Backend) -> str:
    """Create the NOLOGIN roles `hokma_app` and `hokma_agent` when the user may create roles (SPEC-003 2.6).

    Returns "created", "exists" or "skipped". Never raises: without the privilege the given user is used as is."""
    try:
        session = target.connect()
    except DbError:
        return "skipped"
    try:
        can = session.scalar("SELECT rolcreaterole OR rolsuper FROM pg_roles WHERE rolname = current_user")
        schema = session.scalar("SELECT current_schema()")
        if not can or not isinstance(schema, str) or not _IDENT.match(schema):
            return "skipped"
        existing = {r["rolname"] for r in session.query(
            "SELECT rolname FROM pg_roles WHERE rolname IN (:a, :b)", {"a": APP_ROLE, "b": AGENT_ROLE})}
        session.begin(write=True)
        try:
            for role in (APP_ROLE, AGENT_ROLE):
                if role not in existing:
                    session.execute(f"CREATE ROLE {role} NOLOGIN")
                session.execute(f"GRANT USAGE ON SCHEMA {schema} TO {role}")
                session.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {schema} TO {role}")
                session.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {schema} TO {role}")
            session.execute(f"REVOKE INSERT, UPDATE, DELETE ON {schema}.status_transitions, {schema}.approvals "
                            f"FROM {AGENT_ROLE}")
            session.commit()
        except Exception:
            session.rollback()
            return "skipped"
        return "exists" if len(existing) == 2 else "created"
    except Exception:
        return "skipped"
    finally:
        session.close()


def sample_counts(backend: Backend) -> dict[str, Any]:
    """Row counts per table (for the panel and tests)."""
    files = discover()
    tables = _plan(files)
    session = backend.connect()
    try:
        return {t.name: _count(session, t.name) for t in tables if session.table_exists(t.name)}
    finally:
        session.close()

"""The engine layer: the only place that knows which database runs underneath.

Repositories talk to a `Session` with named `:param` SQL that is valid on SQLite and Postgres. A new engine
(D11) adds a `Backend` subclass and the matching sections in the migrations; repositories do not change.
"""
from __future__ import annotations

import re
import sqlite3
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Iterator, Mapping

from hokma.db.errors import DbError, IntegrityViolation, RuleViolation
from hokma.db.vecext import load_into as load_vec_extension

Params = Mapping[str, Any] | None

SQLITE_CONSTRAINT_TRIGGER = 1811
_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")


class Session(ABC):
    """One connection, one transaction. Created by `Backend.connect()`; used by repositories."""

    engine: str

    @abstractmethod
    def query(self, sql: str, params: Params = None) -> list[dict[str, Any]]: ...

    def one(self, sql: str, params: Params = None) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Params = None) -> Any:
        row = self.one(sql, params)
        return next(iter(row.values())) if row else None

    @abstractmethod
    def execute(self, sql: str, params: Params = None) -> int:
        """Run a statement; returns the number of rows it changed."""

    def execute_many(self, sql: str, rows: list[Mapping[str, Any]]) -> int:
        """Run one statement once per parameter set (bulk copy). Engines may batch it."""
        return sum(self.execute(sql, row) for row in rows)

    def insert(self, table: str, values: Mapping[str, Any]) -> int:
        """INSERT one row and return its generated `id`."""
        self._check_name(table)
        for column in values:
            self._check_name(column)
        columns = ", ".join(values)
        marks = ", ".join(f":{c}" for c in values)
        row = self.one(f"INSERT INTO {table} ({columns}) VALUES ({marks}) RETURNING id", values)
        return int(row["id"]) if row else 0

    def insert_row(self, table: str, values: Mapping[str, Any]) -> int:
        """INSERT a row of a table without an `id` column (composite or text key). Returns rows inserted."""
        self._check_name(table)
        for column in values:
            self._check_name(column)
        columns = ", ".join(values)
        marks = ", ".join(f":{c}" for c in values)
        return self.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", values)

    def update(self, table: str, key: Mapping[str, Any], values: Mapping[str, Any]) -> int:
        """UPDATE `values` WHERE every `key` column matches."""
        if not values:
            return 0
        self._check_name(table)
        for column in (*values, *key):
            self._check_name(column)
        sets = ", ".join(f"{c} = :v_{c}" for c in values)
        where = " AND ".join(f"{c} = :k_{c}" for c in key)
        params = {f"v_{c}": v for c, v in values.items()} | {f"k_{c}": v for c, v in key.items()}
        return self.execute(f"UPDATE {table} SET {sets} WHERE {where}", params)

    @staticmethod
    def _check_name(name: str) -> None:
        if not _NAME.match(name):
            raise DbError(f"invalid identifier: {name!r}")

    @abstractmethod
    def table_exists(self, name: str) -> bool: ...

    @abstractmethod
    def begin(self, *, write: bool) -> None: ...

    @abstractmethod
    def commit(self) -> None: ...

    @abstractmethod
    def rollback(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...


class Backend(ABC):
    """An engine: opens sessions and knows its dialect."""

    engine: str
    marks: dict[str, str]                     # {PK} and {TS} replacements for the DDL

    @abstractmethod
    def connect(self) -> Session: ...

    @abstractmethod
    def split_statements(self, script: str) -> Iterator[str]: ...

    @abstractmethod
    def exists(self) -> bool:
        """True when the database already holds data (a file with content, a reachable schema)."""

    @abstractmethod
    def backup(self, destination: Path) -> None:
        """Write a consistent copy of the database to `destination`."""


# --------------------------------------------------------------------------------------------- SQLite

WAL_RETRY_SECONDS = 5.0


def map_sqlite_error(exc: sqlite3.Error) -> DbError | sqlite3.Error:
    """Constraint failures become IntegrityViolation; trigger aborts become RuleViolation."""
    if isinstance(exc, sqlite3.IntegrityError):
        message = str(exc)
        if getattr(exc, "sqlite_errorcode", None) == SQLITE_CONSTRAINT_TRIGGER:
            return RuleViolation(message)
        return IntegrityViolation(message)
    return exc


class SqliteSession(Session):
    engine = "sqlite"

    def __init__(self, path: Path) -> None:
        self._conn = sqlite3.connect(str(path), isolation_level=None, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._enable_wal()
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self.vec_loaded = load_vec_extension(self._conn)      # sqlite-vec for the Library's vectors; False when absent

    def _enable_wal(self) -> None:
        """Switching a new file to WAL needs a lock that `busy_timeout` does not wait for: several processes opening a
        file that does not exist yet can get "database is locked" on this one statement. Retry it for a few seconds."""
        deadline = time.monotonic() + WAL_RETRY_SECONDS
        while True:
            try:
                self._conn.execute("PRAGMA journal_mode = WAL")
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    self._conn.close()
                    raise
                time.sleep(0.02)

    def query(self, sql: str, params: Params = None) -> list[dict[str, Any]]:
        try:
            cursor = self._conn.execute(sql, dict(params or {}))
            return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as exc:
            mapped = map_sqlite_error(exc)
            if mapped is exc:
                raise
            raise mapped from exc

    def execute(self, sql: str, params: Params = None) -> int:
        try:
            return self._conn.execute(sql, dict(params or {})).rowcount
        except sqlite3.Error as exc:
            mapped = map_sqlite_error(exc)
            if mapped is exc:
                raise
            raise mapped from exc

    def table_exists(self, name: str) -> bool:
        return self.scalar("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :n", {"n": name}) is not None

    def begin(self, *, write: bool) -> None:
        self._conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")

    def commit(self) -> None:
        if self._conn.in_transaction:
            self._conn.execute("COMMIT")

    def rollback(self) -> None:
        if self._conn.in_transaction:
            self._conn.execute("ROLLBACK")

    def close(self) -> None:
        self._conn.close()

    @property
    def raw(self) -> sqlite3.Connection:
        """The underlying connection (migration runner and tests only)."""
        return self._conn


class SqliteBackend(Backend):
    engine = "sqlite"
    marks = {"{PK}": "INTEGER PRIMARY KEY AUTOINCREMENT", "{TS}": "TEXT"}

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def connect(self) -> SqliteSession:
        if sqlite3.sqlite_version_info < (3, 35):
            raise DbError(f"SQLite 3.35 or newer is required (found {sqlite3.sqlite_version})")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return SqliteSession(self.path)

    def exists(self) -> bool:
        try:
            return self.path.is_file() and self.path.stat().st_size > 0
        except OSError:
            return False

    def split_statements(self, script: str) -> Iterator[str]:
        """Split on statement ends, keeping CREATE TRIGGER ... BEGIN ... END; in one piece."""
        buffer = ""
        for line in script.splitlines():
            if not buffer.strip() and (not line.strip() or line.lstrip().startswith("--")):
                continue
            buffer += line + "\n"
            if sqlite3.complete_statement(buffer):
                yield buffer.strip()
                buffer = ""
        if buffer.strip():
            yield buffer.strip()

    def backup(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(str(self.path), timeout=5.0)
        target = sqlite3.connect(str(destination))
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()

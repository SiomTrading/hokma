"""Postgres engine: `PostgresBackend` and `PostgresSession` (psycopg 3).

Same contract as SQLite (`backend.py`): repositories send named `:param` SQL and get dict rows. Differences handled
here and nowhere else:
- `:name` becomes `%(name)s`; values are bound on the client (`ClientCursor`) so a parameter never needs a type.
- Timestamps are TIMESTAMPTZ in the database and `YYYY-MM-DD HH:MM:SS` UTC strings in Python, like SQLite.
- Trigger aborts (SQLSTATE P0001) become RuleViolation; constraint failures (class 23) IntegrityViolation.
- Connection errors become ConnectionFailed with the password removed from every text.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

import psycopg
from psycopg import errors as pgerr
from psycopg.rows import dict_row

from hokma.db import dbconfig
from hokma.db.backend import Backend, Params, Session
from hokma.db.errors import ConnectionFailed, DbError, IntegrityViolation, RuleViolation

CONNECT_TIMEOUT_SECONDS = 5
MIN_SERVER_VERSION = 120000
FORMAT = "%Y-%m-%d %H:%M:%S"
_WRITE = re.compile(r"\s*(INSERT|UPDATE|DELETE|WITH)\b", re.IGNORECASE)
_PARAM = re.compile(r"""'(?:[^']|'')*'|"[^"]*"|::|(?<![\w:]):([A-Za-z_]\w*)|%""")


@lru_cache(maxsize=512)
def convert_sql(sql: str) -> str:
    """`:name` -> `%(name)s` outside string literals and quoted identifiers; literal `%` -> `%%`."""
    def swap(match: re.Match) -> str:
        if match.group(1):
            return f"%({match.group(1)})s"
        return match.group(0).replace("%", "%%")

    return _PARAM.sub(swap, sql)


def to_python(value: Any) -> Any:
    if isinstance(value, datetime):
        return (value if value.tzinfo is None else value.astimezone(timezone.utc)).strftime(FORMAT)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, memoryview):
        return bytes(value)
    return value


def map_error(exc: psycopg.Error) -> DbError | psycopg.Error:
    state = getattr(exc, "sqlstate", None) or ""
    diag = getattr(exc, "diag", None)
    message = diag.message_primary if diag is not None and diag.message_primary else str(exc)
    if state == "P0001":
        return RuleViolation(message)
    if state.startswith("23"):
        return IntegrityViolation(message)
    return exc


def classify(exc: BaseException) -> str:
    """Catalog code suffix for a failed connection."""
    text = str(exc).lower()
    if isinstance(exc, pgerr.InvalidPassword) or "authentication failed" in text:
        return "auth"
    if isinstance(exc, pgerr.InvalidCatalogName) or re.search(r'database ".*" does not exist', text):
        return "no_database"
    if "role" in text and "does not exist" in text:
        return "auth"
    if "no pg_hba.conf entry" in text:
        return "not_allowed"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if any(w in text for w in ("could not translate host", "name or service not known", "nodename nor servname",
                               "temporary failure in name resolution", "no address associated")):
        return "host"
    if "connection refused" in text or "could not connect" in text:
        return "refused"
    if "ssl" in text:
        return "ssl"
    return "unreachable"


def connect_raw(url: str, *, timeout: int = CONNECT_TIMEOUT_SECONDS) -> psycopg.Connection:
    try:
        conn = psycopg.connect(url, autocommit=True, connect_timeout=timeout, row_factory=dict_row,
                               cursor_factory=psycopg.ClientCursor)
    except psycopg.Error as exc:
        raise ConnectionFailed(dbconfig.redact(str(exc).strip(), url), classify(exc)) from None
    except Exception as exc:                                            # malformed conninfo reaching libpq
        raise ConnectionFailed(dbconfig.redact(str(exc).strip(), url), "unreachable") from None
    try:
        conn.execute("SET TIME ZONE 'UTC'")
    except psycopg.Error:
        conn.close()
        raise
    return conn


class PostgresSession(Session):
    engine = "postgres"

    def __init__(self, url: str) -> None:
        self._conn = connect_raw(url)
        self._in_tx = False

    def _run(self, sql: str, params: Params):
        # Postgres aborts the whole transaction after one failed statement; SQLite only undoes the statement.
        # Repositories rely on the SQLite behaviour (catch a constraint error, go on), so every write inside a
        # transaction runs under a savepoint that is rolled back alone.
        guarded = self._in_tx and _WRITE.match(sql) is not None
        try:
            if guarded:
                self._conn.execute("SAVEPOINT hk_stmt")
            try:
                cursor = self._conn.execute(convert_sql(sql), dict(params)) if params else self._conn.execute(sql)
            except psycopg.Error:
                if guarded:
                    self._conn.execute("ROLLBACK TO SAVEPOINT hk_stmt")
                    self._conn.execute("RELEASE SAVEPOINT hk_stmt")
                raise
            if guarded:
                self._conn.execute("RELEASE SAVEPOINT hk_stmt")
            return cursor
        except psycopg.Error as exc:
            mapped = map_error(exc)
            if mapped is exc:
                raise
            raise mapped from exc

    def query(self, sql: str, params: Params = None) -> list[dict[str, Any]]:
        cursor = self._run(sql, params)
        if cursor.description is None:
            return []
        return [{k: to_python(v) for k, v in row.items()} for row in cursor.fetchall()]

    def execute(self, sql: str, params: Params = None) -> int:
        return max(self._run(sql, params).rowcount, 0)

    def execute_many(self, sql: str, rows: list[Any]) -> int:
        if not rows:
            return 0
        try:
            with self._conn.cursor() as cursor:
                cursor.executemany(convert_sql(sql), [dict(r) for r in rows])
                return max(cursor.rowcount, 0)
        except psycopg.Error as exc:
            mapped = map_error(exc)
            if mapped is exc:
                raise
            raise mapped from exc

    def table_exists(self, name: str) -> bool:
        return self.scalar("SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() "
                           "AND table_name = :n", {"n": name}) is not None

    def begin(self, *, write: bool) -> None:
        # A read unit is a snapshot that is rolled back at exit, like SQLite's deferred transaction (writes inside
        # it are allowed and discarded, which the D1 tests rely on).
        self._conn.execute("BEGIN" if write else "BEGIN ISOLATION LEVEL REPEATABLE READ")
        self._in_tx = True

    def commit(self) -> None:
        if self._in_tx:
            self._in_tx = False
            self._conn.execute("COMMIT")

    def rollback(self) -> None:
        if self._in_tx:
            self._in_tx = False
            try:
                self._conn.execute("ROLLBACK")
            except psycopg.Error:
                pass

    def close(self) -> None:
        try:
            self.rollback()
        finally:
            self._conn.close()

    @property
    def raw(self) -> psycopg.Connection:
        return self._conn


class PostgresBackend(Backend):
    engine = "postgres"
    marks = {"{PK}": "BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY", "{TS}": "TIMESTAMPTZ"}

    def __init__(self, url: str) -> None:
        self.url = dbconfig.parse_url(url)

    def __repr__(self) -> str:                                          # never print the password
        return f"PostgresBackend({dbconfig.display_url(self.url)})"

    def connect(self) -> PostgresSession:
        return PostgresSession(self.url)

    def exists(self) -> bool:
        session = self.connect()
        try:
            return session.table_exists("schema_version") and bool(session.scalar("SELECT count(*) FROM schema_version"))
        finally:
            session.close()

    def backup(self, destination: Path) -> None:
        raise DbError("a Postgres database is backed up on the server, not by Hokma")

    def split_statements(self, script: str) -> Iterator[str]:
        """Split on `;` outside quotes, `--` comments and `$tag$ ... $tag$` bodies (function definitions)."""
        buffer: list[str] = []
        i, n = 0, len(script)
        while i < n:
            ch = script[i]
            if ch == "-" and script.startswith("--", i):
                end = script.find("\n", i)
                i = n if end < 0 else end
                continue
            if ch == "'":
                j = i + 1
                while j < n:
                    if script[j] == "'":
                        if script.startswith("''", j):
                            j += 2
                            continue
                        break
                    j += 1
                buffer.append(script[i:j + 1])
                i = j + 1
                continue
            if ch == "$":
                tag = re.match(r"\$[A-Za-z_]*\$", script[i:])
                if tag:
                    close = script.find(tag.group(0), i + len(tag.group(0)))
                    if close < 0:
                        raise DbError("unterminated dollar-quoted body in a migration")
                    buffer.append(script[i:close + len(tag.group(0))])
                    i = close + len(tag.group(0))
                    continue
            if ch == ";":
                statement = "".join(buffer).strip()
                if statement:
                    yield statement
                buffer = []
                i += 1
                continue
            buffer.append(ch)
            i += 1
        tail = "".join(buffer).strip()
        if tail:
            yield tail

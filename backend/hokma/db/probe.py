"""'Test connection': connect to a Postgres address with a short timeout and say in words what is wrong.

Nothing is created or written. The result never carries the password; error texts go through `dbconfig.redact`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import psycopg

from hokma.db import dbconfig
from hokma.db.errors import ConnectionFailed, ValidationError
from hokma.db.postgres import CONNECT_TIMEOUT_SECONDS, MIN_SERVER_VERSION, connect_raw
from hokma.i18n import t


@dataclass
class ProbeResult:
    ok: bool
    code: str                                   # "ok" or the failure: auth, host, refused, timeout, url_*, ...
    message: str
    notes: list[str] = field(default_factory=list)
    server_version: str | None = None
    database: str | None = None
    host: str | None = None
    has_data: bool = False
    can_create_tables: bool | None = None
    can_create_roles: bool | None = None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def url_message(code: str) -> str:
    """Words for a rejected address (`dbconfig.parse_url` codes)."""
    return {
        "url_empty": lambda: t("db.url.empty"),
        "url_scheme": lambda: t("db.url.scheme"),
        "url_host": lambda: t("db.url.host"),
        "url_database": lambda: t("db.url.database"),
        "url_option": lambda: t("db.url.option"),
    }.get(code, lambda: t("db.url.invalid"))()


def failure_message(code: str, detail: str = "") -> str:
    """Words for a failed connection (`postgres.classify` codes)."""
    table = {
        "auth": lambda: t("db.conn.auth"),
        "no_database": lambda: t("db.conn.no_database"),
        "host": lambda: t("db.conn.host"),
        "refused": lambda: t("db.conn.refused"),
        "ssl": lambda: t("db.conn.ssl"),
        "not_allowed": lambda: t("db.conn.not_allowed"),
        "timeout": lambda: t("db.conn.timeout", seconds=CONNECT_TIMEOUT_SECONDS),
    }
    return table.get(code, lambda: t("db.conn.unreachable", detail=detail or "-"))()


def probe_connection(raw_url: object) -> ProbeResult:
    try:
        url = dbconfig.parse_url(raw_url)
    except ValidationError as exc:
        code = str(exc.args[0]) if exc.args else "url_invalid"
        return ProbeResult(False, code, url_message(code))
    info = dbconfig.describe(url)
    try:
        conn = connect_raw(url, timeout=CONNECT_TIMEOUT_SECONDS)
    except ConnectionFailed as exc:
        # Keep the first line only: libpq adds hints on the following ones.
        detail = str(exc).splitlines()[0] if str(exc) else ""
        return ProbeResult(False, exc.code, failure_message(exc.code, detail), host=str(info["host"]),
                           database=str(info["database"]))
    try:
        conn.execute(f"SET statement_timeout = {CONNECT_TIMEOUT_SECONDS * 1000}")
        row = conn.execute(
            "SELECT current_setting('server_version') AS version, current_setting('server_version_num')::int AS num, "
            "current_database() AS db, has_schema_privilege(current_user, current_schema(), 'CREATE') AS can_create, "
            "(SELECT rolcreaterole OR rolsuper FROM pg_roles WHERE rolname = current_user) AS can_roles").fetchone()
        has_data = False
        exists = conn.execute("SELECT to_regclass('schema_version') IS NOT NULL AS found").fetchone()["found"]
        if exists:
            has_data = bool(conn.execute("SELECT count(*) AS n FROM schema_version").fetchone()["n"])
    except psycopg.Error as exc:
        return ProbeResult(False, "unreachable", failure_message("unreachable", dbconfig.redact(str(exc).splitlines()[0], url)),
                           host=str(info["host"]), database=str(info["database"]))
    finally:
        conn.close()

    version, host, database = str(row["version"]), str(info["host"]), str(row["db"])
    result = ProbeResult(True, "ok", t("db.test.ok", database=database, host=host, version=version),
                         server_version=version, database=database, host=host, has_data=has_data,
                         can_create_tables=bool(row["can_create"]), can_create_roles=bool(row["can_roles"]))
    if row["num"] < MIN_SERVER_VERSION:
        result.ok, result.code = False, "too_old"
        result.message = t("db.test.too_old", version=version)
        return result
    if not row["can_create"]:
        result.ok, result.code = False, "cannot_create"
        result.message = t("db.test.cannot_create")
        return result
    if has_data:
        result.notes.append(t("db.test.has_data"))
    result.notes.append(t("db.test.roles") if row["can_roles"] else t("db.test.no_roles"))
    return result

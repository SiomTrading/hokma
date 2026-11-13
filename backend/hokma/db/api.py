"""Settings > General > database: status, "Test connection" and "Change and migrate" (D11), plus the app lifespan
that keeps the database open for the endpoints of every other delivery.

Routes (session token as `X-Hokma-Token` or `?token=`; errors are `{"detail": {"code", "message"}}`):
    GET  /api/database           engine in use, where it lives (password hidden), schema version, reachability
    POST /api/database/test      {url?}                      -> ProbeResult (200 even when the connection fails)
    POST /api/database/migrate   {target, url?, replace?}    -> copy everything, switch, report counts

The URL (with its password) is stored only in `<data>/database.json`, owner-only. No response and no log line
carries it: it is shown as `postgresql://user@host:port/database`.
"""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from hokma.core.origin import token_matches
from hokma.db import dbconfig
from hokma.db.database import Database, configured_backend, get_database, open_database, open_postgres
from hokma.db.dbconfig import DatabaseConfig
from hokma.db.errors import ConnectionFailed, DbError, MigrationError, ValidationError
from hokma.db.migrate import current_version, migrate
from hokma.db.paths import db_path
from hokma.db.probe import failure_message, probe_connection, url_message
from hokma.db.transfer import TransferError, transfer
from hokma.i18n import t
from hokma.rpc.ws import SESSION_TOKEN

router = APIRouter(prefix="/api/database", tags=["database"])
_switch_lock = threading.Lock()


class ProbeRequest(BaseModel):
    url: str | None = Field(default=None, max_length=dbconfig.MAX_URL_LENGTH)


class MigrateRequest(BaseModel):
    target: str = Field(pattern="^(postgres|sqlite)$")
    url: str | None = Field(default=None, max_length=dbconfig.MAX_URL_LENGTH)
    replace: bool = False


def _fail(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _auth(header_token: str, query_token: str) -> None:
    if not token_matches(header_token or query_token, SESSION_TOKEN):
        raise _fail(401, "unauthorized", "bad token")


def _where(config: DatabaseConfig) -> str:
    return dbconfig.display_url(config.url) if config.engine == "postgres" and config.url else str(db_path())


@router.get("")
def status(x_hokma_token: str = Header(default=""), token: str = Query(default="")) -> dict[str, Any]:
    _auth(x_hokma_token, token)
    config = dbconfig.load_config()
    body: dict[str, Any] = {"engine": config.engine, "location": _where(config), "saved_url": None,
                            "reachable": True, "schema_version": None, "error": None}
    if config.url:
        body["saved_url"] = dbconfig.display_url(config.url)
    try:
        body["schema_version"] = current_version() or None
    except DbError as exc:
        body["reachable"] = False
        body["error"] = t("db.status.unreachable", detail=dbconfig.redact(str(exc), config.url))
    return body


@router.post("/test")
def test(request: ProbeRequest, x_hokma_token: str = Header(default=""), token: str = Query(default="")) -> dict:
    _auth(x_hokma_token, token)
    url = request.url or dbconfig.load_config().url
    if not url:
        return {"ok": False, "code": "url_empty", "message": t("db.url.empty"), "notes": []}
    return probe_connection(url).to_dict()


def switch_message(code: str, detail: str = "") -> str:
    table = {
        "same_engine": lambda: t("db.switch.same_engine"),
        "no_url": lambda: t("db.switch.no_url"),
        "target_not_empty": lambda: t("db.switch.target_not_empty"),
        "source_empty": lambda: t("db.switch.source_empty"),
        "source_outdated": lambda: t("db.switch.source_outdated"),
        "source_unreachable": lambda: t("db.switch.source_unreachable", detail=detail or "-"),
        "target_unreachable": lambda: t("db.switch.target_unreachable", detail=detail or "-"),
        "count_mismatch": lambda: t("db.switch.count_mismatch", detail=detail or "-"),
        "busy": lambda: t("db.switch.busy"),
    }
    return table.get(code, lambda: t("db.switch.failed", detail=detail or "-"))()


def _transfer_error(exc: TransferError, url: str | None) -> HTTPException:
    detail = dbconfig.redact(exc.detail, url)
    status = {"target_not_empty": 409, "same_engine": 409, "source_outdated": 409}.get(exc.code, 400)
    return _fail(status, exc.code, switch_message(exc.code, detail))


def run_switch(target: str, url: str | None, replace: bool) -> dict[str, Any]:
    """Copy the current database into `target` and make it the active one. Raises HTTPException."""
    config = dbconfig.load_config()
    if target == "postgres":
        raw = url or config.url
        if not raw:
            raise _fail(400, "no_url", switch_message("no_url"))
        try:
            clean = dbconfig.parse_url(raw)
        except ValidationError as exc:
            code = str(exc.args[0]) if exc.args else "url_invalid"
            raise _fail(400, code, url_message(code)) from None
        if config.engine == "postgres":
            raise _fail(409, "same_engine", switch_message("same_engine"))
        from hokma.db.postgres import PostgresBackend

        source, destination, new_config = configured_backend(), PostgresBackend(clean), DatabaseConfig("postgres", clean)
    else:
        if config.engine != "postgres":
            raise _fail(409, "same_engine", switch_message("same_engine"))
        from hokma.db.backend import SqliteBackend

        source, destination = configured_backend(), SqliteBackend(db_path())
        new_config = DatabaseConfig("sqlite", config.url)

    if not _switch_lock.acquire(blocking=False):
        raise _fail(409, "busy", switch_message("busy"))
    try:
        try:
            result = transfer(source, destination, replace=replace)
        except TransferError as exc:
            raise _transfer_error(exc, new_config.url or config.url) from None
        dbconfig.save_config(new_config)
    finally:
        _switch_lock.release()

    notes = []
    if target == "postgres":
        notes.append(t("db.switch.roles_created") if result.roles in ("created", "exists")
                     else t("db.switch.roles_skipped"))
    return {"ok": True, "engine": new_config.engine, "location": _where(new_config), "rows": result.rows,
            "counts": result.counts, "schema_version": result.schema_version, "roles": result.roles,
            "previous_copy": str(result.previous_copy) if result.previous_copy else None, "notes": notes,
            "message": (t("db.switch.done_postgres", rows=result.rows) if target == "postgres"
                        else t("db.switch.done_sqlite", rows=result.rows))}


@router.post("/migrate")
def change_and_migrate(request: MigrateRequest, http: Request, x_hokma_token: str = Header(default=""),
                       token: str = Query(default="")) -> dict[str, Any]:
    _auth(x_hokma_token, token)
    body = run_switch(request.target, request.url, request.replace)
    http.app.state.db = get_database()                        # the singleton follows database.json
    http.app.state.db_error = None
    return body


def open_for_app() -> tuple[Database, str | None]:
    """Migrate the configured database and return it with a readable error (None when all is well).

    A Postgres that is down does not stop the app: Settings has to stay reachable to switch back."""
    try:
        migrate()
        return get_database(), None
    except (MigrationError, ConnectionFailed, DbError) as exc:
        config = dbconfig.load_config()
        return get_database(), dbconfig.redact(str(exc), config.url)


@asynccontextmanager
async def database_lifespan(app):
    """FastAPI lifespan: open the database at start; endpoints read it from `request.app.state.db`
    (or call `hokma.db.get_database()`, which returns the same object)."""
    from hokma.backup.restore import apply_pending            # a restore prepared before the restart goes in first
    apply_pending()
    app.state.db, app.state.db_error = open_for_app()
    yield


def current_database(request: Request) -> Database:
    """FastAPI dependency: `db: Database = Depends(current_database)`."""
    return request.app.state.db if getattr(request.app.state, "db", None) is not None else get_database()


__all__ = ["router", "database_lifespan", "current_database", "open_for_app", "run_switch", "open_database",
           "open_postgres", "failure_message"]

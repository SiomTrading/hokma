"""HTTP surface of the project backup on the desktop. Every route needs the per-boot session token (`X-Hokma-Token` or
`?token=`). Errors are `{"detail": {"code", "message"}}`; the panel translates by `code`.

The GitHub login is the one of Settings > Connect GitHub (`/api/github`); the backup has no connection of its own.

    GET  /api/backup               {connected, repo, last_backup_at, last_size, status, message, total_size_estimate,
                                    omitted: [{path, reason}], key_ready}
    POST /api/backup/run           {status}
    POST /api/backup/key           {key}; new key until it is confirmed, 409 after /key/confirm
    POST /api/backup/key/confirm   {last4} -> {ok}
    GET  /api/backup/list          [{name, size, created_at}]
    POST /api/backup/restore       {name, key} -> {status, reconnect}; the engine restarts by itself and applies it
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query, Request
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from hokma.backup import cloud_runner, restore
from hokma.backup.bundle import BackupError
from hokma.connections.store import StoreUnavailable
from hokma.core.origin import token_matches
from hokma.github_connect.flow import GitHubError
from hokma.i18n import t
from hokma.rpc.ws import SESSION_TOKEN


class ConfirmBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    last4: str = Field(max_length=8)


class RestoreBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=200)
    key: str = Field(max_length=64)


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _auth(header_token: str, query_token: str) -> None:
    if not token_matches(header_token or query_token, SESSION_TOKEN):
        raise _error(401, "unauthorized", "bad token")


def _guard(fn, *args):
    try:
        return fn(*args)
    except cloud_runner.KeyExists as exc:
        raise _error(409, "key_exists", str(exc)) from None
    except BackupError as exc:
        raise _error(400, "backup_failed", str(exc)) from None
    except GitHubError as exc:
        raise _error(exc.status, exc.code, t(exc.code)) from None
    except StoreUnavailable:
        raise _error(503, "store_unavailable", t("github.store_unavailable")) from None


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/backup")
    def backup_state(request: Request, x_hokma_token: str = Header(default=""), token: str = Query(default="")):
        _auth(x_hokma_token, token)
        return _guard(cloud_runner.status, request.app.state.db)

    @router.post("/api/backup/run")
    def backup_run(request: Request, x_hokma_token: str = Header(default=""), token: str = Query(default="")):
        _auth(x_hokma_token, token)
        cloud_runner.start(request.app.state.db)
        return {"status": "running"}

    @router.post("/api/backup/key")
    def backup_key(x_hokma_token: str = Header(default=""), token: str = Query(default="")):
        _auth(x_hokma_token, token)
        return {"key": _guard(cloud_runner.new_key)}

    @router.post("/api/backup/key/confirm")
    def backup_key_confirm(body: ConfirmBody, x_hokma_token: str = Header(default=""), token: str = Query(default="")):
        _auth(x_hokma_token, token)
        return {"ok": _guard(cloud_runner.confirm_key, body.last4)}

    @router.get("/api/backup/list")
    def backup_list(x_hokma_token: str = Header(default=""), token: str = Query(default="")):
        _auth(x_hokma_token, token)
        return _guard(restore.list_backups)

    @router.post("/api/backup/restore")
    def backup_restore(body: RestoreBody, request: Request, x_hokma_token: str = Header(default=""),
                       token: str = Query(default="")):
        _auth(x_hokma_token, token)
        staged = _guard(restore.prepare, request.app.state.db, body.name, body.key)
        lifecycle = getattr(request.app.state, "lifecycle", None)    # the supervisor starts the engine again by itself
        return JSONResponse(staged, background=BackgroundTask(lifecycle.restart) if lifecycle else None)

    return router


router = create_router()

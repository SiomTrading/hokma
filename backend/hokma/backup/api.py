"""HTTP surface of the project backup on the desktop. Every route needs the per-boot session token (`X-Hokma-Token` or
`?token=`). Errors are `{"detail": {"code", "message"}}`; the panel translates by `code`.

The GitHub login is the one of Settings > Connect GitHub (`/api/github`); the backup has no connection of its own.

    GET  /api/backup               {connected, repo, last_backup_at, last_size, status (idle|running|ok|warning|error),
                                    message, code, total_size_estimate, omitted: [{path, reason}], key_ready,
                                    restore_failed: {at} | null}
    POST /api/backup/run           {status}
    POST /api/backup/key           {key}; new key until it is confirmed, 409 after /key/confirm
    POST /api/backup/key/confirm   {last4} -> {ok: true}; 400 key_mismatch when the 4 characters are not the key's
    DELETE /api/backup/restore-failed   {ok}; dismisses the notice of a restore that could not be applied
    GET  /api/backup/list          [{name, size, created_at}]
    POST /api/backup/restore       {name, key} -> {status, reconnect}; the engine restarts by itself and applies it.
                                   409 `restore.in_use` while an agent works, unless `?force=true`
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
from hokma.core.shutdown_api import refuse_if_busy
from hokma.github_connect.flow import GitHubError
from hokma.i18n import accepted_language, t, use_language
from hokma.rpc.ws import SESSION_TOKEN


class ConfirmBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    last4: str = Field(min_length=4, max_length=8)


class RestoreBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=200)
    key: str = Field(max_length=64)


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _auth(header_token: str, query_token: str) -> None:
    if not token_matches(header_token or query_token, SESSION_TOKEN):
        raise _error(401, "unauthorized", "bad token")


def _lang(request: Request) -> str | None:
    return accepted_language(request.headers.get("accept-language", ""))


def _guard(request: Request, fn, *args):
    """Runs `fn` with the messages in the language of the panel that asked (not the engine's own)."""
    with use_language(_lang(request)):
        try:
            return fn(*args)
        except cloud_runner.KeyExists as exc:
            raise _error(409, "key_exists", str(exc)) from None
        except cloud_runner.Failure as exc:
            raise _error(400, "backup_failed", str(exc)) from None
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
        return _guard(request, cloud_runner.status, request.app.state.db)

    @router.post("/api/backup/run")
    def backup_run(request: Request, x_hokma_token: str = Header(default=""), token: str = Query(default="")):
        _auth(x_hokma_token, token)
        cloud_runner.start(request.app.state.db, _lang(request))
        return {"status": "running"}

    @router.post("/api/backup/key")
    def backup_key(request: Request, x_hokma_token: str = Header(default=""), token: str = Query(default="")):
        _auth(x_hokma_token, token)
        return {"key": _guard(request, cloud_runner.new_key)}

    @router.post("/api/backup/key/confirm")
    def backup_key_confirm(body: ConfirmBody, request: Request, x_hokma_token: str = Header(default=""),
                           token: str = Query(default="")):
        _auth(x_hokma_token, token)
        if not _guard(request, cloud_runner.confirm_key, body.last4):
            with use_language(_lang(request)):
                raise _error(400, "key_mismatch", t("backup.key.last4.wrong"))
        return {"ok": True}

    @router.get("/api/backup/list")
    def backup_list(request: Request, x_hokma_token: str = Header(default=""), token: str = Query(default="")):
        _auth(x_hokma_token, token)
        return _guard(request, restore.list_backups)

    @router.post("/api/backup/restore")
    def backup_restore(body: RestoreBody, request: Request, x_hokma_token: str = Header(default=""),
                       token: str = Query(default=""), force: bool = Query(default=False)):
        _auth(x_hokma_token, token)
        refuse_if_busy(force, "restore.in_use", t("restore.err.busy"))
        staged = _guard(request, restore.prepare, request.app.state.db, body.name, body.key)
        lifecycle = getattr(request.app.state, "lifecycle", None)    # the supervisor starts the engine again by itself
        return JSONResponse(staged, background=BackgroundTask(lifecycle.restart) if lifecycle else None)

    @router.delete("/api/backup/restore-failed")
    def backup_restore_failed_dismiss(x_hokma_token: str = Header(default=""), token: str = Query(default="")):
        _auth(x_hokma_token, token)
        restore.dismiss_failure()
        return {"ok": True}

    return router


router = create_router()

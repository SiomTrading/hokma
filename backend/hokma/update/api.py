"""HTTP endpoints behind the Novedades screen.

- Every route needs the per-boot session token in the `X-Hokma-Token` header (same token as /rpc).
- `POST /api/update` takes NO source from the request: the update uses the HOKMA_UPDATE_SOURCE override if set and
  the latest GitHub Release otherwise, so a caller cannot point an update at an arbitrary URL.
- It first asks the source whether there is anything newer; an up-to-date Hokma answers so and keeps running.
- Otherwise it does NOT run the update in this server (task 3146): the server's own files live in a runtime slot and
  Windows cannot replace files a running process loaded. It starts the detached updater (`hokma.update.detached`)
  with the session secrets on its standard input, answers `{"status": "launched", "restarting": true, ...}` and then
  shuts the engine down the way "Shut down Hokma" does, with the socket reason `updating`. The updater stops what is
  left, updates, and starts Hokma again on the same port with the same session; the tab reconnects by itself.
  The outcome is in `<data>/update-status.json`; `GET /api/update/state` reports a failure from there.
"""
import asyncio
import os

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask

from hokma import __version__
from hokma.core import enginelock, session
from hokma.core.lifecycle import WS_UPDATING_REASON
from hokma.core.origin import token_matches
from hokma.db.api import current_database
from hokma.i18n import t
from hokma.rpc.ws import SESSION_TOKEN
from hokma.update import detached
from hokma.update.source import UpdateError

router = APIRouter()


def clean_cli_error(text: str) -> str:
    """The command line's message without its own `[FAIL]`/`[warn]` tags: the student reads the sentence, not the tag."""
    tags = tuple(t(k).strip() for k in ("cli.tag.fail", "cli.tag.warn", "cli.tag.ok"))
    lines = []
    for line in text.splitlines():
        line = line.strip()
        for tag in tags:
            if line.startswith(tag):
                line = line[len(tag):].strip()
                break
        if line:
            lines.append(line)
    return "\n".join(lines)


def _auth(token: str) -> None:
    if not token_matches(token, SESSION_TOKEN):
        raise HTTPException(status_code=401, detail=t("update.api.bad_token"))


@router.get("/api/update/state")
def update_state(request: Request, x_hokma_token: str = Header(default="")) -> dict:
    _auth(x_hokma_token)
    return request.app.state.updates.snapshot(current_database(request))


@router.post("/api/update")
async def update(request: Request, x_hokma_token: str = Header(default="")) -> JSONResponse:
    _auth(x_hokma_token)
    state, lifecycle = request.app.state.updates, request.app.state.lifecycle
    if not state.begin_update():
        raise HTTPException(status_code=409, detail=t("update.in_progress"))
    try:
        target = await asyncio.to_thread(detached.check_newer)
        if target is None:
            state.finish_update({"status": "up-to-date"}, False)
            return JSONResponse({"status": "up-to-date", "from_version": __version__, "to_version": __version__,
                                 "restart_required": False, "restarting": False})
        lock = enginelock.read()
        await asyncio.to_thread(
            detached.launch, handoff=session.handoff_secrets(), port=lock["port"] if lock else request.url.port,
            wait_pids=[os.getpid()] + ([lock["pid"]] if lock else []), restart=True, to_version=target)
    except UpdateError as exc:
        state.fail_update(str(exc))
        raise HTTPException(status_code=502, detail=str(exc))
    except (OSError, ValueError):
        state.fail_update(t("update.api.failed"))
        raise HTTPException(status_code=502, detail=t("update.api.failed"))
    state.launched(target)
    body = {"status": "launched", "from_version": __version__, "to_version": target, "restart_required": False,
            "restarting": True}
    return JSONResponse(body, headers={"Cache-Control": "no-store"},
                        background=BackgroundTask(lifecycle.shutdown, WS_UPDATING_REASON))

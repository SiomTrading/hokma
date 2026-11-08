"""HTTP endpoints behind the Novedades screen.

- Every route needs the per-boot session token in the `X-Hokma-Token` header (same token as /rpc).
- `POST /api/update` takes NO source from the request: the child process uses the HOKMA_UPDATE_SOURCE override
  if set and the latest GitHub Release otherwise, so a caller cannot point an update at an arbitrary URL.
- It runs `hokma update --json` in an isolated child process (the running server is not the one being replaced)
  and answers with its result. The child reports its real stage in a progress file that `GET /api/update/state`
  serves, so the screen can show it with a live clock.
- When a supervisor runs this engine, a successful update ends with the sockets closed with the reason
  `restarting` and the engine exiting with code 75; the supervisor starts the new version. Otherwise the
  result says a manual restart is needed.
"""
import asyncio
import json
import sys

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from hokma.core.proc import spawn_kwargs
from hokma.core.origin import token_matches
from hokma.db.api import current_database
from hokma.i18n import t
from hokma.rpc.ws import SESSION_TOKEN
from hokma.update.state import progress_path

router = APIRouter()
UPDATE_TIMEOUT_SECONDS = 1800


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
async def update(request: Request, background: BackgroundTasks, x_hokma_token: str = Header(default="")) -> dict:
    _auth(x_hokma_token)
    state, lifecycle = request.app.state.updates, request.app.state.lifecycle
    if not state.begin_update():
        raise HTTPException(status_code=409, detail=t("update.in_progress"))
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-m", "hokma_cli.main", "update", "--json", "--progress-file", str(progress_path()),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL,
            **spawn_kwargs())
        try:
            out, err = await asyncio.wait_for(proc.communicate(), UPDATE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            raise HTTPException(status_code=504, detail=t("update.api.timeout"))
        if proc.returncode != 0:
            raise HTTPException(status_code=502,
                                detail=clean_cli_error(err.decode(errors="replace")) or t("update.api.failed"))
        result = json.loads(out.decode().strip().splitlines()[-1])
    except HTTPException as exc:
        state.fail_update(str(exc.detail))
        raise
    except (ValueError, IndexError, OSError):
        state.fail_update(t("update.api.failed"))
        raise HTTPException(status_code=502, detail=t("update.api.failed"))
    restarting = result.get("status") == "updated" and lifecycle.supervised
    state.finish_update(result, restarting)
    result["restarting"] = restarting
    if restarting:
        background.add_task(lifecycle.restart)             # runs after this answer has been sent
    return result

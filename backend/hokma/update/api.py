"""HTTP endpoint behind the Novedades "Update" button.

- Needs the per-boot session token in the `X-Hokma-Token` header (same token as /rpc).
- Takes NO source from the request: the child process uses the HOKMA_UPDATE_SOURCE override if set and the
  latest GitHub Release otherwise, so a caller cannot point an update at an arbitrary URL.
- Runs `hokma update --json` in an isolated child process (the running server is not the one being
  replaced) and answers with its result. The new version runs after Hokma is restarted.
"""
import asyncio
import json
import secrets
import sys

from fastapi import APIRouter, Header, HTTPException

from hokma.i18n import t
from hokma.rpc.ws import SESSION_TOKEN

router = APIRouter()
UPDATE_TIMEOUT_SECONDS = 1800


@router.post("/api/update")
async def update(x_hokma_token: str = Header(default="")) -> dict:
    if not secrets.compare_digest(x_hokma_token, SESSION_TOKEN):
        raise HTTPException(status_code=401, detail=t("update.api.bad_token"))
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-m", "hokma_cli.main", "update", "--json",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), UPDATE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(status_code=504, detail=t("update.api.timeout"))
    if proc.returncode != 0:
        raise HTTPException(status_code=502, detail=err.decode(errors="replace").strip() or t("update.api.failed"))
    return json.loads(out.decode().strip().splitlines()[-1])

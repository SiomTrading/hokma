"""POST /api/native/retry: the "Try again" button of the notice about optional parts that are off (task 3288).

It deletes `native-probe.json` (the remembered answers and safe mode) and restarts the engine, which probes every part
again. Needs the per-boot session token. 409 `native.in_use` while an agent works, unless `?force=true`.
"""
from fastapi import APIRouter, Header, HTTPException, Query, Request
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse

from hokma.core import nativeprobe
from hokma.core.origin import token_matches
from hokma.core.shutdown_api import refuse_if_busy
from hokma.i18n import accepted_language, t, use_language
from hokma.rpc.ws import SESSION_TOKEN

router = APIRouter()


@router.post("/api/native/retry")
def native_retry(request: Request, x_hokma_token: str = Header(default=""), token: str = Query(default=""),
                 force: bool = Query(default=False)):
    if not token_matches(x_hokma_token or token, SESSION_TOKEN):
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "bad token"})
    with use_language(accepted_language(request.headers.get("accept-language", ""))):
        refuse_if_busy(force, "native.in_use", t("native.err.busy"))
    nativeprobe.clear()
    lifecycle = getattr(request.app.state, "lifecycle", None)     # the supervisor starts the engine again by itself
    return JSONResponse({"restarting": True}, background=BackgroundTask(lifecycle.restart) if lifecycle else None)

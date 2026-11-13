"""HTTP surface of the persistent agent preferences. Every route needs the per-boot session token
(`X-Hokma-Token` or `?token=`). Errors are `{"detail": {"code", "message"}}`."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from hokma.core.origin import token_matches
from hokma.db import DbError
from hokma.i18n import t
from hokma.power import keepawake, wake
from hokma.rpc.ws import SESSION_TOKEN

log = logging.getLogger("hokma.power")
CHECK_SECONDS = 15.0


class PowerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keep_awake: bool | None = None
    wake_enabled: bool | None = None


def _auth(header_token: str, query_token: str) -> None:
    if not token_matches(header_token or query_token, SESSION_TOKEN):
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "bad token"})


def _state(db: Any) -> dict[str, Any]:
    with db.read() as u:
        profile = u.profile.get_or_default()
    return {"keep_awake": profile.keep_awake, "wake_enabled": profile.wake_enabled,
            "active": keepawake.keep_awake().is_active(), "supported": keepawake.supported(), "wake": wake.waker().info()}


def apply_preference(db: Any, busy: Callable[[], bool]) -> bool:
    """Hold or release the block from the stored preference and the current work."""
    with db.read() as u:
        preference = u.profile.get_or_default().keep_awake
    wake.apply_wake(db)
    return keepawake.keep_awake().sync(preference, busy())


async def watch(app: Any, busy: Callable[[], bool]) -> None:
    """Every few seconds: the block follows the preference and the work in progress."""
    while True:
        try:
            await asyncio.to_thread(apply_preference, app.state.db, busy)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("keep-awake check failed")
        await asyncio.sleep(CHECK_SECONDS)


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/power")
    def get_power(request: Request, x_hokma_token: str = Header(default=""),
                  token: str = Query(default="")) -> dict[str, Any]:
        _auth(x_hokma_token, token)
        return _state(request.app.state.db)

    @router.put("/api/power")
    def set_power(body: PowerBody, request: Request, x_hokma_token: str = Header(default=""),
                  token: str = Query(default="")) -> dict[str, Any]:
        _auth(x_hokma_token, token)
        db = request.app.state.db
        values = {k: v for k, v in body.model_dump().items() if v is not None}
        if values:
            try:
                with db.write() as u:
                    if u.profile.get() is None:
                        raise HTTPException(status_code=409, detail={"code": "no_profile",
                                                                     "message": t("power.error.no_profile")})
                    u.profile.save(**values)
            except DbError as exc:
                raise HTTPException(status_code=422, detail={"code": "invalid", "message": str(exc)}) from exc
            busy = getattr(request.app.state, "power_busy", None)
            apply_preference(db, busy or (lambda: False))
        return _state(db)

    return router

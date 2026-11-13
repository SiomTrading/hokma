"""GitHub connection over HTTP. Every route needs the per-boot session token (`X-Hokma-Token` or `?token=`).

    POST   /api/github/connect   -> {user_code, verification_uri, interval, expires_in}
    GET    /api/github/connect   -> {status: none|pending|expired|denied|connected, user?}   (one check per call)
    GET    /api/github           -> {connected, user, store_available}
    DELETE /api/github           -> {connected: false}
"""
import asyncio

from fastapi import APIRouter, Header, HTTPException, Query

from hokma.connections.store import KeyStore
from hokma.core.origin import token_matches
from hokma.github_connect import flow
from hokma.i18n import t
from hokma.rpc.ws import SESSION_TOKEN

router = APIRouter(prefix="/api/github")


async def _go(header_token: str, query_token: str, fn):
    if not token_matches(header_token or query_token, SESSION_TOKEN):
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "unauthorized"})
    try:
        return await asyncio.to_thread(fn)
    except flow.GitHubError as error:
        raise HTTPException(status_code=error.status, detail={"code": error.code, "message": t(error.code)}) from None


def _state() -> dict:
    login = flow.user()
    return {"connected": login is not None, "user": login, "store_available": KeyStore().available()}


@router.post("/connect")
async def connect(x_hokma_token: str = Header(default=""), token: str = Query(default="")):
    return await _go(x_hokma_token, token, flow.start)


@router.get("/connect")
async def check(x_hokma_token: str = Header(default=""), token: str = Query(default="")):
    return await _go(x_hokma_token, token, flow.poll)


@router.get("")
async def state(x_hokma_token: str = Header(default=""), token: str = Query(default="")):
    return await _go(x_hokma_token, token, _state)


@router.delete("")
async def disconnect(x_hokma_token: str = Header(default=""), token: str = Query(default="")):
    await _go(x_hokma_token, token, flow.disconnect)
    return {"connected": False}

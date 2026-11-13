"""Settings > Telegram. Every route needs the session token (`X-Hokma-Token` or `?token=`); the ones that change the
link also need `X-Hokma-Owner`, the confirmation of the owner's open tab. Errors are `{"detail": {"code", "message"}}`.

    GET    /api/telegram           -> {linked, connected, user_name, group_linked, avisos}
                                      (avisos: "ok" | "no_permission" | null when no group)
    PUT    /api/telegram/token     {token} -> {connected}          (saves the bot token and connects)
    POST   /api/telegram/approve   {code}  -> {linked, user_name}  (the code the bot sent to the chat)
    DELETE /api/telegram           -> {linked: false}              (forgets the token and the link)
    POST   /api/telegram/group/approve {code} -> {group_linked, avisos}  (the code the bot sent with /vincular)
    DELETE /api/telegram/group     -> {group_linked: false}        (the private link stays)
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from hokma.connections.store import StoreUnavailable
from hokma.core import session
from hokma.core.origin import token_matches
from hokma.i18n import t
from hokma.rpc.ws import SESSION_TOKEN
from hokma.telegram.core import AlreadyLinked, InvalidBotToken, NotLinked
from hokma.telegram.glue import TOKEN_ENTRY, TelegramGlue


class TokenBody(BaseModel):
    token: str = Field(max_length=100)


class CodeBody(BaseModel):
    code: str = Field(max_length=16)


def _fail(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def create_router() -> APIRouter:
    router = APIRouter(prefix="/api/telegram")

    def glue(request: Request) -> TelegramGlue:
        return request.app.state.telegram

    def auth(header: str, query: str) -> None:
        if not token_matches(header or query, SESSION_TOKEN):
            raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "bad token"})

    def owner(value: str) -> None:
        if not session.owner_matches(value):
            raise _fail(403, "owner_confirmation_required", t("telegram.err.owner_confirmation_required"))

    @router.get("")
    def status(request: Request, x_hokma_token: str = Header(default=""), token: str = Query(default="")) -> dict:
        auth(x_hokma_token, token)
        g = glue(request)
        record = g.store.load()
        grouped = bool((record or {}).get("group_id"))
        return {"linked": record is not None, "connected": g.link.connected,
                "user_name": (record or {}).get("user_name", ""),
                "group_linked": grouped, "avisos": (record or {}).get("avisos") if grouped else None}

    @router.put("/token")
    async def set_token(body: TokenBody, request: Request, x_hokma_token: str = Header(default=""),
                        x_hokma_owner: str = Header(default=""), token: str = Query(default="")) -> dict:
        auth(x_hokma_token, token)
        owner(x_hokma_owner)
        g = glue(request)
        try:
            g.keys.set(TOKEN_ENTRY, body.token.strip())
        except StoreUnavailable:
            raise _fail(503, "no_key_store", t("telegram.err.no_key_store")) from None
        try:
            await g.start()
        except InvalidBotToken:
            g.keys.delete(TOKEN_ENTRY)
            raise _fail(400, "invalid_token", t("telegram.err.invalid_token")) from None
        except Exception:
            await g.disconnect()
            raise _fail(502, "cannot_connect", t("telegram.err.cannot_connect")) from None
        return {"connected": g.link.connected}

    @router.post("/approve")
    def approve(body: CodeBody, request: Request, x_hokma_token: str = Header(default=""),
                x_hokma_owner: str = Header(default=""), token: str = Query(default="")) -> dict:
        auth(x_hokma_token, token)
        owner(x_hokma_owner)
        try:
            found = glue(request).link.approve_pairing_code(body.code.strip())
        except AlreadyLinked:
            raise _fail(409, "already_linked", t("telegram.err.already_linked")) from None
        if found is None:
            raise _fail(400, "invalid_code", t("telegram.err.invalid_code"))
        return {"linked": True, "user_name": found.get("user_name", "")}

    @router.delete("")
    async def unlink(request: Request, x_hokma_token: str = Header(default=""),
                     x_hokma_owner: str = Header(default=""), token: str = Query(default="")) -> dict:
        auth(x_hokma_token, token)
        owner(x_hokma_owner)
        await glue(request).disconnect()
        return {"linked": False}

    @router.post("/group/approve")
    async def approve_group(body: CodeBody, request: Request, x_hokma_token: str = Header(default=""),
                            x_hokma_owner: str = Header(default=""), token: str = Query(default="")) -> dict:
        auth(x_hokma_token, token)
        owner(x_hokma_owner)
        g = glue(request)
        try:
            found = await g.link.approve_group_code(body.code.strip())
        except NotLinked:
            raise _fail(409, "not_linked", t("telegram.err.not_linked")) from None
        except AlreadyLinked:
            raise _fail(409, "group_already_linked", t("telegram.err.group_already_linked")) from None
        if found is None:
            raise _fail(400, "invalid_code", t("telegram.err.invalid_group_code"))
        return {"group_linked": True, "avisos": (g.store.load() or {}).get("avisos")}

    @router.delete("/group")
    def unlink_group(request: Request, x_hokma_token: str = Header(default=""),
                     x_hokma_owner: str = Header(default=""), token: str = Query(default="")) -> dict:
        auth(x_hokma_token, token)
        owner(x_hokma_owner)
        glue(request).link.unlink_group()
        return {"group_linked": False}

    return router

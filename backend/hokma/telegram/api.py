"""Settings > Telegram. Every route needs the session token (`X-Hokma-Token` or `?token=`); the ones that change the
link also need `X-Hokma-Owner`, the confirmation of the owner's open tab. Errors are `{"detail": {"code", "message"}}`.

    GET    /api/telegram           -> {linked, connected, user_name, group_linked, avisos, outbox, connect_error}
                                      (connected: the bot is up and its long-poll is alive; the watcher retries it
                                      by itself. avisos: "ok" | "no_permission" | "unreachable" | null when no group.
                                      outbox: {pending, last_error} of the messages Telegram has not accepted)
    POST   /api/telegram/reconnect -> {connected}          (try now; the saved token and the link stay)
    PUT    /api/telegram/token     {token} -> {connected}          (saves the bot token and connects)
    POST   /api/telegram/approve   {code}  -> {linked, user_name}  (the code the bot sent to the chat)
    DELETE /api/telegram           -> {linked: false}              (forgets the token and the link)
    POST   /api/telegram/group/approve {code} -> {group_linked, avisos}  (the code the bot sent with /vincular)
    POST   /api/telegram/group/avisos -> {avisos}                  (try the Avisos topic again)
    DELETE /api/telegram/group     -> {group_linked: false}        (the private link stays)
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel

from hokma.connections.store import StoreUnavailable
from hokma.core import session
from hokma.core.origin import token_matches
from hokma.i18n import t
from hokma.rpc.ws import SESSION_TOKEN
from hokma.telegram.core import AlreadyLinked, InvalidBotToken, NotLinked
from hokma.telegram.core.link import GROUP_PLATFORM, PLATFORM
from hokma.telegram.core.approvals_ui import _redact_telegram_error_text
from hokma.telegram.core.bot_token import is_valid_telegram_bot_token
from hokma.telegram.glue import TOKEN_ENTRY, TelegramGlue


MAX_TOKEN_CHARS, MAX_CODE_CHARS = 100, 16     # checked in the routes: a pydantic 422 would reach the screen raw


class TokenBody(BaseModel):
    token: str


class CodeBody(BaseModel):
    code: str


def _fail(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _is_rejected_token(exc: BaseException) -> bool:
    from telegram.error import InvalidToken
    return isinstance(exc, (InvalidBotToken, InvalidToken))


def _is_network(exc: BaseException) -> bool:
    import httpx
    from telegram.error import NetworkError, TimedOut
    return isinstance(exc, (NetworkError, TimedOut, httpx.ConnectError))


def _log_connect_error(exc: BaseException) -> None:
    """Leave the real reason in engine.log (the token never goes there)."""
    from hokma.engine import errlog
    errlog.log("telegram connect failed: " + _redact_telegram_error_text(f"{type(exc).__name__}: {exc}"),
               scope="telegram")


def _avisos_detail(g, record) -> dict | None:
    """Bug 1170: why the Avisos topic is not ok, with Telegram's code and a translated message (None when it is ok)."""
    if (record or {}).get("avisos") == "ok":
        return None
    info = getattr(g.link, "topic_error", None)
    return {"code": info["code"], "error": info["error"], "message": info["message"]} if info else None


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
                "group_linked": grouped, "avisos": (record or {}).get("avisos") if grouped else None,
                "avisos_detail": _avisos_detail(g, record) if grouped else None,
                "permission_mode": g.permission_mode(),
                "outbox": g.outbox_state(), "connect_error": g.connect_error}

    @router.post("/reconnect")
    async def reconnect(request: Request, x_hokma_token: str = Header(default=""), token: str = Query(default="")) -> dict:
        auth(x_hokma_token, token)
        g = glue(request)
        try:
            started = await g.start()
        except Exception as exc:                       # noqa: BLE001  (the token and the link are kept: only retry)
            _log_connect_error(exc)
            g.connect_error = type(exc).__name__
            if _is_network(exc):
                raise _fail(502, "network", t("telegram.err.network")) from None
            raise _fail(502, "cannot_connect", t("telegram.err.cannot_connect")) from None
        if not started:
            raise _fail(409, "no_token", t("telegram.err.no_token"))
        return {"connected": g.link.connected}

    @router.put("/token")
    async def set_token(body: TokenBody, request: Request, x_hokma_token: str = Header(default=""),
                        x_hokma_owner: str = Header(default=""), token: str = Query(default="")) -> dict:
        auth(x_hokma_token, token)
        owner(x_hokma_owner)
        g = glue(request)
        new_token = body.token.strip()
        if len(new_token) > MAX_TOKEN_CHARS or not is_valid_telegram_bot_token(new_token):      # bug 1151: nothing is saved (or deleted) for a bad shape
            raise _fail(400, "invalid_token", t("telegram.err.invalid_token"))
        try:
            previous = g.keys.get(TOKEN_ENTRY)
            g.keys.set(TOKEN_ENTRY, new_token)
        except StoreUnavailable:
            raise _fail(503, "no_key_store", t("telegram.err.no_key_store")) from None

        async def undo() -> None:
            """A failed attempt never touches the link: stop the connection, put the old token back (if any) and
            bring the bot back up with it (bug 1151: the token that worked is never lost)."""
            await g.link.stop()
            if previous:
                g.keys.set(TOKEN_ENTRY, previous)
                try:
                    await g.start()
                except Exception:                      # noqa: BLE001  (the watcher keeps trying with the saved token)
                    pass
            else:
                g.keys.delete(TOKEN_ENTRY)

        try:
            await g.start()
        except Exception as exc:                       # noqa: BLE001  (every failure becomes a code the screen can show)
            if _is_rejected_token(exc):
                await undo()
                raise _fail(400, "invalid_token", t("telegram.err.invalid_token")) from None
            _log_connect_error(exc)
            await undo()
            if _is_network(exc):
                raise _fail(502, "network", t("telegram.err.network")) from None
            raise _fail(502, "cannot_connect", t("telegram.err.cannot_connect")) from None
        return {"connected": g.link.connected}

    @router.post("/approve")
    def approve(body: CodeBody, request: Request, x_hokma_token: str = Header(default=""),
                x_hokma_owner: str = Header(default=""), token: str = Query(default="")) -> dict:
        auth(x_hokma_token, token)
        owner(x_hokma_owner)
        link = glue(request).link
        code = body.code.strip()
        if len(code) > MAX_CODE_CHARS:
            raise _fail(400, "invalid_code", t("telegram.err.invalid_code"))
        locked = link.pairing_lockout_minutes(PLATFORM)
        if locked and link.linked_chat_id() is None:             # bug 1154: not "get a new code": none is given now
            raise _fail(429, "locked", t("telegram.err.locked", minutes=locked))
        try:
            found = link.approve_pairing_code(code)
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
        code = body.code.strip()
        if len(code) > MAX_CODE_CHARS:
            raise _fail(400, "invalid_code", t("telegram.err.invalid_group_code"))
        locked = g.link.pairing_lockout_minutes(GROUP_PLATFORM)
        if locked and g.link.linked_chat_id() is not None and g.link.linked_group_id() is None:
            raise _fail(429, "locked", t("telegram.err.locked", minutes=locked))
        try:
            found = await g.link.approve_group_code(code)
        except NotLinked:
            raise _fail(409, "not_linked", t("telegram.err.not_linked")) from None
        except AlreadyLinked:
            raise _fail(409, "group_already_linked", t("telegram.err.group_already_linked")) from None
        if found is None:
            raise _fail(400, "invalid_code", t("telegram.err.invalid_group_code"))
        record = g.store.load() or {}
        return {"group_linked": True, "avisos": record.get("avisos"), "avisos_detail": _avisos_detail(g, record)}

    @router.post("/group/avisos")
    async def retry_avisos(request: Request, x_hokma_token: str = Header(default=""),
                           x_hokma_owner: str = Header(default=""), token: str = Query(default="")) -> dict:
        """Bug 1157: try the Avisos topic again (a missing right was granted, the network came back)."""
        auth(x_hokma_token, token)
        owner(x_hokma_owner)
        g = glue(request)
        if g.link.linked_group_id() is None:
            raise _fail(409, "no_group", t("telegram.err.group_not_linked"))
        if not g.link.connected:
            raise _fail(409, "not_connected", t("telegram.err.cannot_connect"))
        await g.link.ensure_avisos_topic(revalidate=True)
        record = g.store.load() or {}
        return {"avisos": record.get("avisos"), "avisos_detail": _avisos_detail(g, record)}

    @router.delete("/group")
    def unlink_group(request: Request, x_hokma_token: str = Header(default=""),
                     x_hokma_owner: str = Header(default=""), token: str = Query(default="")) -> dict:
        auth(x_hokma_token, token)
        owner(x_hokma_owner)
        glue(request).link.unlink_group()
        return {"group_linked": False}

    return router

"""Engines in the panel: connections, detection, tests, models, import from the student's own Claude.

    GET    /api/engines                         {connections[], detect{}, store_available}
    POST   /api/engines/api                     {provider, key?, label?, base_url?, model?}
    POST   /api/engines/cli                     {engine, auth_kind, label, key?}
    POST|GET|DELETE /api/engines/codex/login    device-code sign-in of Codex: {state, url, code}
    POST   /api/engines/{id}/test               {ok, status, code}
    PUT    /api/engines/{id}/default | /label | /tiers
    DELETE /api/engines/{id}
    GET    /api/engines/{id}/models
    GET    /api/engines/claude/import           what could be imported (names and host, never a secret value)
    POST   /api/engines/claude/import           creates the connection
    PUT    /api/conversations/{cid}/engine  {engine_id}

Every route needs the per-boot session token. Errors are {"detail": {"code", "message"}}; a body is read by hand so
no validation message can repeat a key."""
import asyncio
import json

from fastapi import APIRouter, Header, HTTPException, Query, Request

from hokma.core.origin import token_matches
from hokma.db import NotFound, get_database
from hokma.engine.api import get_engine
from hokma.engines import codex_login, secrets, service
from hokma.engines.service import ServiceError
from hokma.i18n import t
from hokma.rpc.ws import SESSION_TOKEN

router = APIRouter(prefix="/api")
MAX_BODY = 4096


def _guard(header: str, query: str) -> None:
    if not token_matches(header or query, SESSION_TOKEN):
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "unauthorized"})


def _fail(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": t(code)})


async def _body(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise _fail(413, "engines.bad_request")
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise _fail(400, "engines.bad_request") from None
    if not isinstance(body, dict):
        raise _fail(400, "engines.bad_request")
    return body


async def _run(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except ServiceError as exc:
        raise _fail(exc.status, exc.code) from None
    except NotFound:
        raise _fail(404, "engines.not_found") from None


@router.get("/engines/texts")
async def texts(x_hokma_token: str = Header(default=""), token: str = Query(default="")) -> dict:
    """The panel texts of the engine screens, in the active language ({items}, {provider}, {engine}, {cli} are left for the panel)."""
    _guard(x_hokma_token, token)
    raw = lambda key, text: text  # noqa: E731
    return {
        "engines.title": t("engines.title"),
        "engines.lead": t("engines.lead"),
        "engines.recommended": t("engines.recommended"),
        "engines.later": t("engines.later"),
        "engines.none_banner": t("engines.none_banner"),
        "engines.import.found": t("engines.import.found"),
        "engines.import.review": t("engines.import.review"),
        "engines.err.region_card": t("engines.err.region_card"),
        "engines.err.connection_removed": t("engines.err.connection_removed"),
        "engines.protect.note": t("engines.protect.note"),
        "engines.bad_request": t("engines.bad_request"),
        "engines.bad_url": t("engines.bad_url"),
        "engines.bad_provider": t("engines.bad_provider"),
        "engines.bad_engine": t("engines.bad_engine"),
        "engines.key_required": t("engines.key_required"),
        "engines.not_found": t("engines.not_found"),
        "engines.store_unavailable": t("engines.store_unavailable"),
        "engines.nothing_to_import": t("engines.nothing_to_import"),
        "tier.fast": t("tier.fast"),
        "tier.balanced": t("tier.balanced"),
        "tier.deep": t("tier.deep"),
    }


@router.get("/engines")
async def listing(x_hokma_token: str = Header(default=""), token: str = Query(default="")) -> dict:
    _guard(x_hokma_token, token)
    return {"connections": await _run(service.list_connections), "detect": await _run(service.detect),
            "store_available": secrets.available()}


@router.post("/engines/api")
async def add_api(request: Request, x_hokma_token: str = Header(default=""), token: str = Query(default="")) -> dict:
    _guard(x_hokma_token, token)
    b = await _body(request)
    return await _run(lambda: service.add_api(str(b.get("provider", "")), b.get("key"), label=b.get("label"),
                                              base_url=b.get("base_url"), model=b.get("model")))


@router.post("/engines/cli")
async def add_cli(request: Request, x_hokma_token: str = Header(default=""), token: str = Query(default="")) -> dict:
    _guard(x_hokma_token, token)
    b = await _body(request)
    return await _run(lambda: service.add_cli(str(b.get("engine", "")), str(b.get("auth_kind", "")),
                                              str(b.get("label", "")), b.get("key")))


@router.get("/engines/claude/import")
async def import_preview(x_hokma_token: str = Header(default=""), token: str = Query(default="")) -> dict:
    _guard(x_hokma_token, token)
    return await _run(service.claude_import_preview)


@router.post("/engines/claude/import")
async def import_apply(x_hokma_token: str = Header(default=""), token: str = Query(default="")) -> dict:
    _guard(x_hokma_token, token)
    return await _run(service.claude_import_apply)


@router.post("/engines/codex/login")
async def codex_login_start(x_hokma_token: str = Header(default=""), token: str = Query(default="")) -> dict:
    _guard(x_hokma_token, token)
    return await asyncio.to_thread(codex_login.start)


@router.get("/engines/codex/login")
async def codex_login_status(x_hokma_token: str = Header(default=""), token: str = Query(default="")) -> dict:
    _guard(x_hokma_token, token)
    return codex_login.status()


@router.delete("/engines/codex/login")
async def codex_login_cancel(x_hokma_token: str = Header(default=""), token: str = Query(default="")) -> dict:
    _guard(x_hokma_token, token)
    codex_login.cancel()
    return {"ok": True}


@router.post("/engines/{engine_id}/test")
async def test(engine_id: int, x_hokma_token: str = Header(default=""), token: str = Query(default="")) -> dict:
    _guard(x_hokma_token, token)
    try:
        return await service.ping(engine_id)
    except NotFound:
        raise _fail(404, "engines.not_found") from None


@router.get("/engines/{engine_id}/models")
async def models(engine_id: int, x_hokma_token: str = Header(default=""), token: str = Query(default="")) -> dict:
    _guard(x_hokma_token, token)
    return {"models": await _run(service.live_models, engine_id)}


@router.put("/engines/{engine_id}/default")
async def make_default(engine_id: int, x_hokma_token: str = Header(default=""),
                       token: str = Query(default="")) -> dict:
    _guard(x_hokma_token, token)
    await _run(service.set_default, engine_id)
    return {"ok": True}


@router.put("/engines/{engine_id}/label")
async def label(engine_id: int, request: Request, x_hokma_token: str = Header(default=""),
                token: str = Query(default="")) -> dict:
    _guard(x_hokma_token, token)
    return await _run(service.rename, engine_id, str((await _body(request)).get("label", "")))


@router.put("/engines/{engine_id}/tiers")
async def tiers(engine_id: int, request: Request, x_hokma_token: str = Header(default=""),
                token: str = Query(default="")) -> dict:
    _guard(x_hokma_token, token)
    given = (await _body(request)).get("tiers")
    return await _run(service.set_tiers, engine_id, given if isinstance(given, dict) else {})


@router.delete("/engines/{engine_id}")
async def remove(engine_id: int, x_hokma_token: str = Header(default=""), token: str = Query(default="")) -> dict:
    _guard(x_hokma_token, token)
    await _run(service.remove, engine_id)
    return {"ok": True}


@router.put("/conversations/{conversation_id}/engine")
async def switch(conversation_id: int, request: Request, x_hokma_token: str = Header(default=""),
                 token: str = Query(default="")) -> dict:
    """Change the engine of a conversation: the next turn starts a new session of that engine with the last 20
    messages as context."""
    _guard(x_hokma_token, token)
    cid = (await _body(request)).get("connection_id")
    if not isinstance(cid, int):
        raise _fail(422, "engines.bad_request")
    store = get_engine().store
    try:
        await asyncio.to_thread(store.set_connection, conversation_id, cid)
    except NotFound:
        raise _fail(404, "engines.not_found") from None
    session = get_engine().sessions.get(str(conversation_id))
    if session is not None:
        await session._drop_process()
        session._session_id, session._session_started = None, False
    with get_database().read() as u:
        engine_label = u.engine_connections.get(cid).label
    return {"ok": True, "notice": t("engines.switch.notice", engine=engine_label)}

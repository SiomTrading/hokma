"""Connections: the only place where data providers are configured. Every route needs the per-boot session token
(`X-Hokma-Token` or `?token=`) and the same Host and Origin guard as the rest of the API, so no agent can reach it.

    GET    /api/connections                  {connections[], groups{asset_class: [id]}, uses{use: [id]}, store_available}
    PUT    /api/connections/{id}/key         {key} or {key_id, secret}  ->  {id, key_state}   (never echoes the key)
    DELETE /api/connections/{id}/key         ->  {id, key_state}
    PUT    /api/connections/{id}/enabled     {enabled}  ->  the card
    PUT    /api/connections/priority         {asset_class (any use: equity, bars, options, future, crypto, forex, news), order[]}
                                             ->  {groups, uses}
    POST   /api/connections/{id}/test        ->  {ok, state, data_state, latency_ms, symbol, first_data, error_code, message}
    POST   /api/connections/{id}/add         key fields or {}  ->  {added, card, test}   (a failed test undoes the key)
    POST   /api/connections/{id}/remove      {}  ->  the card
    POST   /api/connections/schwab/login/start    {}  ->  {auth_url}
    POST   /api/connections/schwab/login/open     {url}  ->  {opened}
    POST   /api/connections/schwab/login/finish   {redirect_url}  ->  {card, test}   (10 per minute)
    GET    /api/connections/schwab/account         ->  the "View account" panel, plus `orders_enabled`

Errors are `{"detail": {"code", "message"}}` with a `connections.*` catalog code. The body of a key request is read by
hand, so no validation message can ever repeat the key.
"""
import asyncio
import json
import logging

from fastapi import APIRouter, Header, HTTPException, Query, Request

from hokma.connections import runtime
from hokma.connections.service import ServiceError
from hokma.core.origin import token_matches
from hokma.i18n import t
from hokma.market.cache import RateLimiter
from hokma.market.messages import CONNECTION_CODES, connection_message
from hokma.schwab.http import NOT_CONNECTED, SchwabError
from hokma.rpc.ws import SESSION_TOKEN

log = logging.getLogger("hokma.connections")
router = APIRouter(prefix="/api/connections")

MAX_BODY_BYTES = 2048
_tests = RateLimiter(limit=20, window=60.0)
_finishes = RateLimiter(limit=10, window=60.0)
MAX_URL_CHARS = 1500


def _fail(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": connection_message(code)})


def _guard(header_token: str, query_token: str) -> None:
    if not token_matches(header_token or query_token, SESSION_TOKEN):
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "unauthorized"})


async def _json(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise _fail(413, "connections.bad_request")
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise _fail(400, "connections.bad_request") from None
    if not isinstance(body, dict):
        raise _fail(400, "connections.bad_request")
    return body


def _service_error(error: ServiceError) -> HTTPException:
    return _fail(error.status, error.code)


def _schwab_error(error: SchwabError) -> HTTPException:
    """A `SchwabError` as `{"detail": {"code", "message"}}`. `connections.*` codes keep their text; a Schwab call that
    found no session says `connections.no_login`; any other `schwab.err.*` keeps its own catalog text."""
    code = "connections.no_login" if error.code == NOT_CONNECTED else error.code
    status = 409 if error.code == NOT_CONNECTED else error.status
    if code in CONNECTION_CODES:
        return _fail(status, code)
    try:
        message = t(code)
    except Exception:                                    # noqa: BLE001  (a code with placeholders or without text)
        message = connection_message("connections.unreachable")
    return HTTPException(status_code=status, detail={"code": code, "message": message})


async def _listing() -> dict:
    hub = await runtime.prepare()
    service = runtime.get_service()
    # In a thread: the Schwab card asks the sign-in module, which may read the system store.
    return {"connections": await asyncio.to_thread(hub.cards), "groups": hub.groups(), "uses": hub.uses(),
            "store_available": await asyncio.to_thread(service.store_available)}


@router.get("", summary="The provider cards")
async def list_connections(x_hokma_token: str = Header(default=""), token: str = Query(default="")) -> dict:
    _guard(x_hokma_token, token)
    return await _listing()


@router.put("/priority", summary="Order of the providers of one asset class")
async def set_priority(request: Request, x_hokma_token: str = Header(default=""),
                       token: str = Query(default="")) -> dict:
    _guard(x_hokma_token, token)
    body = await _json(request)
    order = body.get("order")
    if not isinstance(order, list) or not all(isinstance(x, str) for x in order) or not isinstance(
            body.get("asset_class"), str):
        raise _fail(422, "connections.bad_priority")
    try:
        runtime.get_service().set_priority(body["asset_class"], order)
    except ServiceError as error:
        raise _service_error(error) from None
    hub = await runtime.prepare()
    return {"groups": hub.groups(), "uses": hub.uses()}


@router.put("/{provider_id}/key", summary="Save the key in the system credential store")
async def put_key(provider_id: str, request: Request, x_hokma_token: str = Header(default=""),
                  token: str = Query(default="")) -> dict:
    _guard(x_hokma_token, token)
    body = await _json(request)
    hub = await runtime.prepare()
    service = runtime.get_service()
    try:
        await asyncio.to_thread(service.put_key, provider_id, body)
    except ServiceError as error:
        raise _service_error(error) from None
    hub.forget_error(provider_id)
    await hub.refresh(provider_id, restart=True)
    return {"id": provider_id, "key_state": "saved"}


@router.delete("/{provider_id}/key", summary="Remove the key from the system credential store")
async def delete_key(provider_id: str, x_hokma_token: str = Header(default=""),
                     token: str = Query(default="")) -> dict:
    _guard(x_hokma_token, token)
    hub = await runtime.prepare()
    try:
        await asyncio.to_thread(runtime.get_service().delete_key, provider_id)
    except ServiceError as error:
        raise _service_error(error) from None
    hub.forget_error(provider_id)
    await hub.refresh(provider_id, restart=True)
    return {"id": provider_id, "key_state": "missing"}


@router.put("/{provider_id}/enabled", summary="Switch a provider on or off")
async def put_enabled(provider_id: str, request: Request, x_hokma_token: str = Header(default=""),
                      token: str = Query(default="")) -> dict:
    _guard(x_hokma_token, token)
    body = await _json(request)
    if not isinstance(body.get("enabled"), bool):
        raise _fail(422, "connections.bad_request")
    hub = await runtime.prepare()
    try:
        runtime.get_service().set_enabled(provider_id, body["enabled"])
    except ServiceError as error:
        raise _service_error(error) from None
    hub.forget_error(provider_id)
    await hub.refresh(provider_id)
    return await asyncio.to_thread(hub.card, provider_id)


@router.post("/{provider_id}/test", summary="Open the stream and wait for the first message")
async def test_connection(provider_id: str, x_hokma_token: str = Header(default=""),
                          token: str = Query(default="")) -> dict:
    _guard(x_hokma_token, token)
    if runtime.get_service().catalog.get(provider_id) is None:
        raise _fail(404, "connections.unknown_provider")
    if not _tests.allow():
        raise _fail(429, "connections.rate_limited")
    hub = await runtime.prepare()
    log.info("connection test for %s", provider_id)
    return await hub.test_provider(provider_id)


@router.post("/{provider_id}/add", summary="Save the key, switch the provider on and test it, all or nothing")
async def add_connection(provider_id: str, request: Request, x_hokma_token: str = Header(default=""),
                         token: str = Query(default="")) -> dict:
    _guard(x_hokma_token, token)
    body = await _json(request)
    service = runtime.get_service()
    try:
        service.spec(provider_id)
    except ServiceError as error:
        raise _service_error(error) from None
    if not _tests.allow():
        raise _fail(429, "connections.rate_limited")
    hub = await runtime.prepare()
    loop = asyncio.get_running_loop()

    async def probe() -> dict:
        hub.forget_error(provider_id)
        await hub.refresh(provider_id, restart=True)          # the new key, not the connection the old one opened
        return await hub.test_provider(provider_id)

    def tester() -> dict:
        return asyncio.run_coroutine_threadsafe(probe(), loop).result()

    log.info("add %s", provider_id)
    try:
        result = await asyncio.to_thread(service.add, provider_id, body, tester)
    except ServiceError as error:
        raise _service_error(error) from None
    if not result["added"]:
        hub.forget_error(provider_id)
    await hub.refresh(provider_id, restart=not result["added"])  # a failed add reconnects with the key that stays
    card = await asyncio.to_thread(hub.card, provider_id)
    return {"added": result["added"], "card": card, "test": result["test"]}


@router.post("/{provider_id}/remove", summary="Forget the provider: its key, and for Schwab its session")
async def remove_connection(provider_id: str, request: Request, x_hokma_token: str = Header(default=""),
                            token: str = Query(default="")) -> dict:
    _guard(x_hokma_token, token)
    await _json(request)
    hub = await runtime.prepare()
    try:
        await asyncio.to_thread(runtime.get_service().remove, provider_id)
    except ServiceError as error:
        raise _service_error(error) from None
    hub.forget_error(provider_id)
    await hub.refresh(provider_id, restart=True)
    return await asyncio.to_thread(hub.card, provider_id)


# ------------------------------------------------------------------ Schwab sign-in (5.3) and account (5.7)


def _text(body: dict, name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_URL_CHARS:
        raise _fail(422, "connections.schwab_url")
    return value.strip()


@router.post("/schwab/login/start", summary="The Schwab sign-in address")
async def schwab_login_start(request: Request, x_hokma_token: str = Header(default=""),
                             token: str = Query(default="")) -> dict:
    _guard(x_hokma_token, token)
    await _json(request)
    await runtime.prepare()
    if not runtime.get_service().has_key("schwab"):
        raise _fail(409, "connections.no_key")
    from hokma.schwab import auth

    try:
        started = await asyncio.to_thread(auth.start_login)
    except SchwabError as error:
        raise _schwab_error(error) from None
    return {"auth_url": started["auth_url"]}


@router.post("/schwab/login/open", summary="Open the sign-in address in the browser of this computer")
async def schwab_login_open(request: Request, x_hokma_token: str = Header(default=""),
                            token: str = Query(default="")) -> dict:
    _guard(x_hokma_token, token)
    url = _text(await _json(request), "url")
    from hokma.schwab import auth

    try:
        opened = await asyncio.to_thread(auth.open_login_in_browser, url)
    except SchwabError as error:
        raise _schwab_error(error) from None
    return {"opened": bool(opened)}


@router.post("/schwab/login/finish", summary="Finish the sign-in with the address Schwab sent the browser to")
async def schwab_login_finish(request: Request, x_hokma_token: str = Header(default=""),
                              token: str = Query(default="")) -> dict:
    _guard(x_hokma_token, token)
    if not _finishes.allow():
        raise _fail(429, "connections.rate_limited")
    redirect_url = _text(await _json(request), "redirect_url")
    hub = await runtime.prepare()
    from hokma.schwab import auth

    try:
        finished = await asyncio.to_thread(auth.finish_login, redirect_url)
    except SchwabError as error:
        raise _schwab_error(error) from None
    test = finished.get("test", {})
    if test.get("ok"):
        await asyncio.to_thread(runtime.get_service().mark_added, "schwab")   # idempotent: `finish_login` did it already
    hub.forget_error("schwab")
    await hub.refresh("schwab")
    return {"card": await asyncio.to_thread(hub.card, "schwab"), "test": test}


@router.get("/schwab/account", summary="The View account panel: accounts, balances, positions and open orders")
async def schwab_account(x_hokma_token: str = Header(default=""), token: str = Query(default="")) -> dict:
    _guard(x_hokma_token, token)
    from hokma.broker import service as broker_service
    from hokma.schwab import accounts

    try:
        data = await asyncio.to_thread(accounts.panel_data)
    except SchwabError as error:
        raise _schwab_error(error) from None
    try:
        data["orders_enabled"] = await asyncio.to_thread(broker_service.schwab_orders_enabled)
    except Exception as exc:                              # noqa: BLE001  (the switch is read only: off when it cannot be read)
        log.warning("the orders switch of Schwab could not be read: %s", type(exc).__name__)
        data["orders_enabled"] = False
    return data

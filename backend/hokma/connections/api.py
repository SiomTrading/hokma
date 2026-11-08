"""Connections: the only place where data providers are configured. Every route needs the per-boot session token
(`X-Hokma-Token` or `?token=`) and the same Host and Origin guard as the rest of the API, so no agent can reach it.

    GET    /api/connections                  {connections[], groups{asset_class: [id]}, store_available}
    PUT    /api/connections/{id}/key         {key} or {key_id, secret}  ->  {id, key_state}   (never echoes the key)
    DELETE /api/connections/{id}/key         ->  {id, key_state}
    PUT    /api/connections/{id}/enabled     {enabled}  ->  the card
    PUT    /api/connections/priority         {asset_class, order[]}  ->  {groups}
    POST   /api/connections/{id}/test        ->  {ok, state, data_state, latency_ms, symbol, first_data, error_code, message}

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
from hokma.market.cache import RateLimiter
from hokma.market.messages import connection_message
from hokma.rpc.ws import SESSION_TOKEN

log = logging.getLogger("hokma.connections")
router = APIRouter(prefix="/api/connections")

MAX_BODY_BYTES = 2048
_tests = RateLimiter(limit=20, window=60.0)


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


async def _listing() -> dict:
    hub = await runtime.prepare()
    service = runtime.get_service()
    return {"connections": hub.cards(), "groups": hub.groups(),
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
    return {"groups": hub.groups()}


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
    return hub.card(provider_id)


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

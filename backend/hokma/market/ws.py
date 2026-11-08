"""`WS /ws/market?token=`: the live price channel between the engine and the panel. The Host and Origin guard of the
whole app covers it; the session token is checked here before the socket is accepted.

Panel to engine (JSON text):
    {"op":"subscribe","sub":"c1","symbol":"NQ=F","tf":"5m"}      {"op":"unsubscribe","sub":"c1"}      {"op":"ping"}
`tf` is any interval of `intervals.py` (`30s`, `7m`, `3h`, `2D`, `1W`, or a range `r:10t` / `r:2.5p`); the snapshot
answers with the canonical text in `tf` and the parsed form in `interval`.

Engine to panel: `snapshot`, `bar`, `last`, `status`, `error` (each with `sub`, `seq` growing per subscription,
`epoch`, `source` and `data_state`), `pong` and `connection_status` (no `sub`). See `hub.py` for the fields.
The panel never polls; everything it shows after the snapshot arrives here.
"""
import asyncio
import logging
import json
import re

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from hokma.connections import runtime
from hokma.core.origin import token_matches
from hokma.market.hub import MarketHub, PanelClient
from hokma.market.intervals import IntervalError, parse
from hokma.market.yahoo import valid_symbol
from hokma.rpc.ws import SESSION_TOKEN

log = logging.getLogger("hokma.market")

router = APIRouter()

SUB_ID = re.compile(r"[A-Za-z0-9_\-]{1,32}")
MAX_MESSAGE_CHARS = 4096


@router.websocket("/ws/market")
async def market_socket(ws: WebSocket) -> None:
    if not token_matches(ws.query_params.get("token", ""), SESSION_TOKEN):
        await ws.close(code=4401)
        return
    await ws.accept()
    hub = await runtime.prepare()
    client = PanelClient()
    hub.attach_client(client)
    operations: asyncio.Queue = asyncio.Queue()
    workers = [asyncio.create_task(_pump(ws, client)), asyncio.create_task(_operations(hub, client, operations))]
    try:
        while True:
            raw = await ws.receive_text()
            if len(raw) > MAX_MESSAGE_CHARS:
                client.push(hub.error_message("market.bad_request"))
                continue
            _dispatch(hub, client, operations, raw)
    except WebSocketDisconnect:
        pass
    finally:
        # The release is a task of the hub, started before anything is awaited: if this handler is cancelled while
        # it unwinds (the server is shutting down), the subscriptions are still released.
        hub.detach_client(client)
        for worker in workers:
            worker.cancel()
        try:
            await asyncio.gather(*workers, return_exceptions=True)
        except asyncio.CancelledError:
            pass


def _dispatch(hub: MarketHub, client: PanelClient, operations: asyncio.Queue, raw: str) -> None:
    try:
        message = json.loads(raw)
    except ValueError:
        message = None
    if not isinstance(message, dict):
        client.push(hub.error_message("market.bad_request"))
        return
    op, sub = message.get("op"), message.get("sub")
    if op == "ping":
        client.push({"type": "pong", "epoch": hub.epoch})
    elif op == "subscribe":
        symbol, tf = message.get("symbol"), message.get("tf")
        sub_ok = isinstance(sub, str) and SUB_ID.fullmatch(sub) is not None
        if not sub_ok or not isinstance(symbol, str) or not valid_symbol(symbol) or not isinstance(tf, str):
            client.push(hub.error_message("market.bad_request", sub if sub_ok else None))
            return
        try:
            interval = parse(tf)
        except IntervalError as error:
            client.push(hub.refusal(error.code, sub))
            return
        operations.put_nowait(("subscribe", sub, symbol, interval.canonical))
    elif op == "unsubscribe" and isinstance(sub, str):
        operations.put_nowait(("unsubscribe", sub, None, None))
    else:
        client.push(hub.error_message("market.bad_request"))


async def _operations(hub: MarketHub, client: PanelClient, operations: asyncio.Queue) -> None:
    """Subscribe and unsubscribe run in the order they arrived, off the receive loop, so a slow history load never
    delays a ping."""
    while True:
        op, sub, symbol, tf = await operations.get()
        try:
            if op == "subscribe":
                await hub.subscribe(client, sub, symbol, tf)
            else:
                await hub.unsubscribe(client, sub)
        except Exception:                                      # one bad request must not end the socket
            log.exception("market operation failed")
            client.push(hub.error_message("market.down", sub))


async def _pump(ws: WebSocket, client: PanelClient) -> None:
    while True:
        text = await client.outbox.get()
        if client.overflowed:                                  # a reader too slow to follow is dropped
            await ws.close(code=1013)
            return
        await ws.send_text(text)

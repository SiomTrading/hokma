"""JSON-RPC over WebSocket. Echo only, with a per-boot temporary token."""
import json
import secrets

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()

SESSION_TOKEN = secrets.token_urlsafe(24)


@router.websocket("/rpc")
async def rpc(ws: WebSocket) -> None:
    if not secrets.compare_digest(ws.query_params.get("token", ""), SESSION_TOKEN):
        await ws.close(code=4401)
        return
    await ws.accept()
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_text(json.dumps({
                    "jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "Parse error"}}))
                continue
            await ws.send_text(json.dumps({
                "jsonrpc": "2.0", "id": msg.get("id"), "result": {"echo": msg}}))
    except WebSocketDisconnect:
        pass

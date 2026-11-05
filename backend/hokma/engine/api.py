"""HTTP and WebSocket surface of the chat engine. Both need the per-boot session token."""

from fastapi import APIRouter, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

from hokma.core.origin import token_matches
from hokma.engine import attachments as att
from hokma.engine.engine import ChatEngine, Connection
from hokma.rpc.ws import SESSION_TOKEN

_engine: ChatEngine | None = None
_in_memory = False


def configure(engine: ChatEngine | None = None, **kwargs) -> ChatEngine:
    """Set the engine the routes use: `configure(store=..., projects=...)` builds one."""
    global _engine, _in_memory
    _engine, _in_memory = engine or ChatEngine(session_token=SESSION_TOKEN, **kwargs), False
    return _engine


def database_ready() -> bool:
    from hokma.db import current_version
    try:
        return current_version() > 0
    except Exception:
        return False


def get_engine() -> ChatEngine:
    """The configured engine. If nobody configured one: SQLite-backed once the schema exists (the first run
    creates it), and in RAM before that so the app still answers."""
    global _engine, _in_memory
    if _engine is None or (_in_memory and not _engine.sessions and database_ready()):
        if database_ready():
            from hokma.engine.dbstore import DbAgents, DbChatStore, DbProjects
            _engine = ChatEngine(store=DbChatStore(), projects=DbProjects(), agents=DbAgents(),
                                 session_token=SESSION_TOKEN)
            _in_memory = False
        else:
            _engine, _in_memory = ChatEngine(session_token=SESSION_TOKEN), True
    return _engine


def _token_ok(candidate: str | None) -> bool:
    return token_matches(candidate, SESSION_TOKEN)


def create_router(engine_getter=get_engine) -> APIRouter:
    router = APIRouter()

    @router.websocket("/ws/chat")
    async def ws_chat(ws: WebSocket) -> None:
        if not _token_ok(ws.query_params.get("token")):
            await ws.close(code=4401)
            return
        await ws.accept()
        engine = engine_getter()
        conn = Connection(ws.send_text)
        try:
            while True:
                await engine.handle(conn, await ws.receive_text())
        except WebSocketDisconnect:
            pass
        finally:
            engine.detach(conn)

    @router.post("/api/attachments", status_code=201)
    async def upload_attachment(request: Request, x_hokma_token: str = Header(default=""),
                                token: str = Query(default=""), x_filename: str = Header(default="")):
        if not _token_ok(x_hokma_token or token):
            raise HTTPException(status_code=401, detail="bad token")
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > att.MAX_ATTACHMENT_BYTES + 64 * 1024:
            raise HTTPException(status_code=413, detail="attachment too large")
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > att.MAX_ATTACHMENT_BYTES + 64 * 1024:
                raise HTTPException(status_code=413, detail="attachment too large")
        content_type = request.headers.get("content-type", "")
        parsed = att.parse_multipart(content_type, bytes(body))
        if parsed is not None:
            name, declared_type, data = parsed
        elif content_type.lower().startswith("multipart/"):
            raise HTTPException(status_code=400, detail="no file in the request")
        else:
            name, declared_type, data = x_filename or "file", content_type.split(";")[0], bytes(body)
        if not data:
            raise HTTPException(status_code=400, detail="empty file")
        if len(data) > att.MAX_ATTACHMENT_BYTES:
            raise HTTPException(status_code=413, detail="attachment too large")
        mime = att.sniff_mime(data, declared_type)
        record = engine_getter().store.add_attachment(att.save_blob(data), att.safe_name(name), mime, len(data))
        return JSONResponse({"id": record.id, "name": record.name, "mime": record.mime, "size": record.size,
                             "kind": "image" if att.is_image(record.mime) else "file"}, status_code=201)

    @router.get("/api/attachments/{attachment_id}")
    def read_attachment(attachment_id: str, x_hokma_token: str = Header(default=""), token: str = Query(default="")):
        """The bytes of an image sent in the chat (the thumbnail of a chip). Other kinds are not served."""
        if not _token_ok(x_hokma_token or token):
            raise HTTPException(status_code=401, detail="bad token")
        record = engine_getter().store.get_attachment(attachment_id)
        path = att.attachments_dir() / record.sha256 if record else None
        if record is None or path is None or not att.is_image(record.mime) or not path.is_file():
            raise HTTPException(status_code=404, detail="no such image")
        return Response(path.read_bytes(), media_type=record.mime,
                        headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"})

    async def shutdown() -> None:
        await engine_getter().shutdown()

    router.add_event_handler("shutdown", shutdown)
    return router


router = create_router()

"""WebSocket /ws/terminal: a real shell on a pseudo-terminal, protected with the session token."""
import asyncio
import json

from fastapi import APIRouter, WebSocket

from hokma.core.origin import token_matches
from hokma.rpc.ws import SESSION_TOKEN
from hokma.terminal.pty_backend import PtyUnavailable
from hokma.terminal.session import DEFAULT_COLS, DEFAULT_ROWS, TerminalSession, clamp_size, registry

router = APIRouter()

WS_UNAUTHORIZED = 4401
WS_TOO_MANY = 4429
MAX_FRAME_CHARS = 2 * 1024 * 1024


def _token_ok(candidate: str | None) -> bool:
    return token_matches(candidate, SESSION_TOKEN)


async def _send(ws: WebSocket, payload: dict) -> None:
    await ws.send_text(json.dumps(payload, ensure_ascii=False))


async def _pump_output(ws: WebSocket, session: TerminalSession) -> None:
    while True:
        text = await session.next_output()
        if text is None:
            break
        await _send(ws, {"type": "output", "data": text})
    await _send(ws, {"type": "exit", "code": await session.exit_code()})


async def _read_input(ws: WebSocket, session: TerminalSession) -> None:
    while True:
        message = await ws.receive()
        if message["type"] == "websocket.disconnect":
            return
        raw = message.get("text")
        if raw is None or len(raw) > MAX_FRAME_CHARS:
            continue
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            continue
        kind = frame.get("type") if isinstance(frame, dict) else None
        if kind == "input" and isinstance(frame.get("data"), str):
            session.write(frame["data"])
        elif kind == "resize":
            session.resize(frame.get("cols"), frame.get("rows"))
        elif kind == "ping":
            await _send(ws, {"type": "pong"})


def _detail(exc: BaseException) -> str:
    return (getattr(exc, "detail", None) or f"{type(exc).__name__}: {exc}").strip()[:1500]


def _log_failure(code: str, exc: BaseException) -> None:
    if not hasattr(exc, "detail"):         # TerminalStartError was logged by the session with every attempt
        from hokma.engine import errlog
        errlog.log(f"{code}: {_detail(exc)}", exc, scope="terminal")


@router.websocket("/ws/terminal")
async def ws_terminal(ws: WebSocket) -> None:
    if not _token_ok(ws.query_params.get("token")):
        await ws.close(code=WS_UNAUTHORIZED)
        return
    await ws.accept()
    if registry.full():
        await _send(ws, {"type": "error", "code": "too_many_terminals"})
        await ws.close(code=WS_TOO_MANY)
        return
    cols, rows = clamp_size(ws.query_params.get("cols", DEFAULT_COLS), ws.query_params.get("rows", DEFAULT_ROWS))
    try:
        session = TerminalSession.open(asyncio.get_running_loop(), cols, rows, ws.query_params.get("project_id"))
    except PtyUnavailable as exc:
        _log_failure("pty_unavailable", exc)
        await _send(ws, {"type": "error", "code": "pty_unavailable", "detail": _detail(exc)})
        await ws.close(code=1011)
        return
    except Exception as exc:        # noqa: BLE001  (whatever the cause, the screen gets its text, not a silent close)
        _log_failure("spawn_failed", exc)
        await _send(ws, {"type": "error", "code": "spawn_failed", "detail": _detail(exc)})
        await ws.close(code=1011)
        return
    registry.add(session)
    tasks: list[asyncio.Task] = []
    try:
        await _send(ws, {"type": "ready", "pid": session.pid, "cwd": session.cwd})
        tasks = [asyncio.create_task(_pump_output(ws, session)), asyncio.create_task(_read_input(ws, session))]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except Exception:
        pass
    finally:
        await _finish(ws, session, tasks)


async def _cleanup(session: TerminalSession, tasks: list[asyncio.Task]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    try:
        await session.close()
    finally:
        registry.discard(session)


async def _finish(ws: WebSocket, session: TerminalSession, tasks: list[asyncio.Task]) -> None:
    """Stop the shell for good. The server may cancel this handler (shutdown, dropped connection), so the
    cleanup runs shielded; if even that is cut short the shell is killed synchronously."""
    cleanup = asyncio.ensure_future(_cleanup(session, tasks))
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        session.pty.kill_now()
        registry.discard(session)
        raise
    try:
        await ws.close(code=1000)
    except Exception:
        pass

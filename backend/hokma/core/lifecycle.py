"""How a running engine ends: a restart after an update (exit code 75), a shutdown or a plain stop.

`Lifecycle` lives on `app.state.lifecycle`. A restart first closes every open WebSocket with the reason
`restarting`, so each open tab can show "Restarting Hokma..." and wait for the new engine, and only then asks
the server to stop. The process then ends with code 75 and the supervisor starts the new version.

A shutdown (the "Shut down Hokma" button) closes every WebSocket with the reason `shutting_down`, so each open tab shows
"Hokma is shut down" and not "Hokma could not start", and then does what `hokma stop` does: it asks the supervisor
through the control channel, which stops the engine cleanly. Without a supervisor it stops the server itself.
"""
import asyncio
import sqlite3
from typing import Awaitable, Callable

from starlette.types import ASGIApp, Receive, Scope, Send

from hokma.core import control, session

EXIT_RESTART = 75
WS_RESTARTING_CODE = 1012            # "service restart"
WS_RESTARTING_REASON = "restarting"
WS_SHUTDOWN_CODE = 1001              # "going away"
WS_SHUTDOWN_REASON = "shutting_down"
FLUSH_SECONDS = 0.3
CLOSE_WAIT_SECONDS = 3.0             # a tab that does not answer the close must not hold the shutdown back


def checkpoint_database() -> None:
    """After a stop: fold the SQLite write-ahead log into the database file and empty it, so the file alone is
    complete. A missing file (first run, Postgres owner) or a busy database is not an error: the log is applied
    by SQLite itself the next time the database opens."""
    from hokma.db.paths import db_path

    path = db_path()
    if not path.is_file():
        return
    try:
        conn = sqlite3.connect(str(path), timeout=2.0)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    except sqlite3.Error:
        pass


class Lifecycle:
    def __init__(self) -> None:
        self.server = None                            # the uvicorn.Server, set by run()
        self.exit_code = 0
        self.restarting = False
        self.shutting_down = False
        self._closers: set[Callable[[int, str], Awaitable[None]]] = set()

    @property
    def supervised(self) -> bool:
        return session.supervised()

    @property
    def open_sockets(self) -> int:
        """WebSockets open right now: every tab with a session keeps at least one."""
        return len(self._closers)

    def track(self, closer: Callable[[int, str], Awaitable[None]]) -> None:
        self._closers.add(closer)

    def untrack(self, closer: Callable[[int, str], Awaitable[None]]) -> None:
        self._closers.discard(closer)

    async def _close_sockets(self, code: int, reason: str) -> None:
        """Close every socket at once and wait at most CLOSE_WAIT_SECONDS: a close handshake waits for the browser's
        answer, and a tab that is gone must not delay the others, one after another."""
        async def one(closer: Callable[[int, str], Awaitable[None]]) -> None:
            try:
                await closer(code, reason)
            except Exception:                         # a socket that is already gone is not a problem
                pass

        tasks = [asyncio.ensure_future(one(c)) for c in list(self._closers)]
        if tasks:
            await asyncio.wait(tasks, timeout=CLOSE_WAIT_SECONDS)

    async def restart(self) -> None:
        """Close the sockets with the reason, then stop the server with exit code 75. Only once."""
        if self.restarting:
            return
        self.restarting = True
        self.exit_code = EXIT_RESTART
        await self._close_sockets(WS_RESTARTING_CODE, WS_RESTARTING_REASON)
        await asyncio.sleep(FLUSH_SECONDS)
        self.stop()

    async def shutdown(self) -> None:
        """Close the sockets with the reason `shutting_down`, then end the engine the way `hokma stop` does. Only once."""
        if self.shutting_down or self.restarting:
            return
        self.shutting_down = True
        self.exit_code = 0
        await self._close_sockets(WS_SHUTDOWN_CODE, WS_SHUTDOWN_REASON)
        await asyncio.sleep(FLUSH_SECONDS)
        if self.supervised:
            reply = await asyncio.to_thread(control.request, "stop")
            if reply and reply.get("ok"):
                return
        self.stop()                                   # no supervisor, or its channel did not answer: exit code 0 is an end

    def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True


class WebSocketTracker:
    """Pure ASGI: remembers each accepted WebSocket so a restart or a shutdown can close it with the reason."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        lifecycle = getattr(scope["app"].state, "lifecycle", None) if scope["type"] == "websocket" else None
        if lifecycle is None:
            await self.app(scope, receive, send)
            return

        async def closer(code: int, reason: str) -> None:
            await send({"type": "websocket.close", "code": code, "reason": reason})

        async def tracked_send(message) -> None:
            if message["type"] == "websocket.accept":
                lifecycle.track(closer)
            elif message["type"] == "websocket.close":
                lifecycle.untrack(closer)
            await send(message)

        try:
            await self.app(scope, receive, tracked_send)
        finally:
            lifecycle.untrack(closer)

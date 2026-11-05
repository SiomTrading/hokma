"""Local-only guards shared by the whole app: Host/Origin validation (DNS rebinding and cross-site calls)
and the session token check (constant time, on bytes)."""
import secrets

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

TEST_HOSTS = frozenset({"testserver"})


def hosts_for_port(port: int) -> frozenset[str]:
    return frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})


def origins_for(hosts: frozenset[str]) -> frozenset[str]:
    return frozenset(f"http://{h}" for h in hosts)


def token_matches(candidate: str | None, expected: str) -> bool:
    """Constant-time compare on bytes: a non-ASCII candidate is a plain mismatch, never a TypeError."""
    return secrets.compare_digest((candidate or "").encode("utf-8"), expected.encode("utf-8"))


def request_allowed(headers: dict[str, str], hosts: frozenset[str]) -> bool:
    """Host must be one of `hosts`; Origin must be the matching http origin or absent (curl, fetch from tests)."""
    if headers.get("host", "").lower() not in hosts:
        return False
    origin = headers.get("origin")
    return origin is None or origin.lower() in origins_for(hosts)


class LocalOnlyMiddleware:
    """Pure ASGI, so it covers HTTP and every WebSocket with one rule. The allowed hosts are read from
    `app.state.allowed_hosts` on each request (set by create_app; tests may adjust it)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        if request_allowed(headers, scope["app"].state.allowed_hosts):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await receive()                                   # websocket.connect
            await send({"type": "websocket.close", "code": 1008})
            return
        await PlainTextResponse("forbidden", status_code=403)(scope, receive, send)

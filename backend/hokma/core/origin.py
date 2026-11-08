"""Local-only guards shared by the whole app: Host/Origin validation (DNS rebinding and cross-site calls)
and the session token check (constant time, on bytes). The panel name `hokma.localhost` is accepted only on a connection
whose peer and local address are both the loopback."""
import secrets

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

TEST_HOSTS = frozenset({"testserver"})
LOOPBACK_ADDRESSES = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})
OWN_NAME = "hokma.localhost"      # the fixed name of the panel (see config.browser_host)


def hosts_for_port(port: int, own_host: str | None = None) -> frozenset[str]:
    """The Host values the engine answers to: both loopback names and, when given, the panel name."""
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    if own_host:
        hosts.add(f"{own_host.lower()}:{port}")
    return frozenset(hosts)


def is_own_host(host: str | None) -> bool:
    """True when the Host header names the panel name (any port)."""
    return bool(host) and host.lower().split(":", 1)[0] == OWN_NAME


def is_loopback_connection(scope: Scope) -> bool:
    """True when the peer and the local address of the connection are both the loopback. Behind a proxy or on a
    socket without an address this is False."""
    client, server = scope.get("client"), scope.get("server")
    return (bool(client) and bool(server) and client[0] in LOOPBACK_ADDRESSES
            and server[0] in LOOPBACK_ADDRESSES)


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
        if request_allowed(headers, scope["app"].state.allowed_hosts) and (
                not is_own_host(headers.get("host")) or is_loopback_connection(scope)):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await receive()                                   # websocket.connect
            await send({"type": "websocket.close", "code": 1008})
            return
        await PlainTextResponse("forbidden", status_code=403)(scope, receive, send)

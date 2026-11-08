"""Fake providers and fixtures for the live price tests. Nothing here touches the network: each provider is a local
server that speaks the provider's protocol, and the system credential store is an in-memory backend."""
import asyncio
import json
import threading
import time
from http import HTTPStatus

import keyring
import keyring.backend
from keyring.errors import PasswordDeleteError
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from hokma.market.providers import ProviderCatalog, parse_catalog
from hokma.market.providers import CATALOG_FILE


class MemoryKeyring(keyring.backend.KeyringBackend):
    """A usable credential store that lives in memory."""

    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.items:
            raise PasswordDeleteError("not found")
        del self.items[(service, username)]


class Thread:
    """An event loop on a thread of its own, so a fake server keeps running while the test blocks."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def call(self, coroutine, timeout: float = 5.0):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=2)


def wait_until(predicate, timeout: float = 5.0, step: float = 0.01) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


class FakeWs:
    """A WebSocket server. `handler(ws, fake)` is the provider's side of the conversation; `received` collects what
    the client sent as `(path, json)`."""

    def __init__(self, handler, reject_token: str | None = None) -> None:
        self.handler = handler
        self.reject_token = reject_token                     # Finnhub style: a wrong `?token=` is an HTTP 401
        self.received: list[tuple[str, object]] = []
        self.paths: list[str] = []
        self.clients: set = set()
        self.port = 0
        self._thread: Thread | None = None
        self._server = None

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def start(self, port: int = 0) -> "FakeWs":
        self._thread = Thread()
        self._thread.call(self._start(port))
        return self

    async def _start(self, port: int) -> None:
        async def run(ws):
            self.clients.add(ws)
            self.paths.append(ws.request.path)
            try:
                await self.handler(ws, self)
            except ConnectionClosed:
                pass
            finally:
                self.clients.discard(ws)

        async def process(connection, request):
            if self.reject_token is not None and f"token={self.reject_token}" not in request.path:
                return connection.respond(HTTPStatus.UNAUTHORIZED, "invalid token\n")
            return None

        self._server = await serve(run, "127.0.0.1", port, process_request=process)
        self.port = self._server.sockets[0].getsockname()[1]

    def stop(self) -> None:
        """Stop listening and drop every client, as if the provider went down."""
        if self._thread is None:
            return
        self._thread.call(self._stop())
        self._thread.stop()
        self._thread = None

    async def _stop(self) -> None:
        self._server.close()
        for ws in list(self.clients):
            await ws.close(1011)
        await self._server.wait_closed()

    def restart(self) -> None:
        self.start(self.port)

    def drop_clients(self, code: int = 1011) -> None:
        async def go():
            for ws in list(self.clients):
                await ws.close(code)
        self._thread.call(go())

    def send_all(self, payload) -> None:
        text = payload if isinstance(payload, str) else json.dumps(payload)

        async def go():
            for ws in list(self.clients):
                await ws.send(text)
        self._thread.call(go())

    def messages(self, kind=None) -> list:
        items = [m for _, m in self.received]
        return items if kind is None else [m for m in items if isinstance(m, dict) and kind(m)]


# ---------------------------------------------------------------- provider scripts


async def coinbase_script(ws, fake: FakeWs) -> None:
    async for raw in ws:
        message = json.loads(raw)
        fake.received.append((ws.request.path, message))
        if message.get("type") == "subscribe" and "ticker" in message.get("channels", []):
            await ws.send(json.dumps({"type": "subscriptions", "channels": [{"name": "ticker"}]}))
            for product in message["product_ids"]:
                await ws.send(json.dumps({"type": "ticker", "product_id": product, "price": "80400.5",
                                          "time": "2026-11-05T05:42:53.123456Z", "last_size": "0.01"}))


def alpaca_script(key_id: str, secret: str, symbols_limit: int | None = None):
    async def script(ws, fake: FakeWs) -> None:
        await ws.send(json.dumps([{"T": "success", "msg": "connected"}]))
        async for raw in ws:
            message = json.loads(raw)
            fake.received.append((ws.request.path, message))
            if message.get("action") == "auth":
                if message.get("key") == key_id and message.get("secret") == secret:
                    await ws.send(json.dumps([{"T": "success", "msg": "authenticated"}]))
                else:
                    await ws.send(json.dumps([{"T": "error", "code": 402, "msg": "auth failed"}]))
                    await ws.close(1008)
                    return
            elif message.get("action") == "subscribe":
                await ws.send(json.dumps([{"T": "subscription", "trades": message.get("trades", []), "quotes": [],
                                           "bars": []}]))
    return script


async def finnhub_script(ws, fake: FakeWs) -> None:
    async for raw in ws:
        fake.received.append((ws.request.path, json.loads(raw)))


def massive_script(key: str):
    async def script(ws, fake: FakeWs) -> None:
        await ws.send(json.dumps([{"ev": "status", "status": "connected", "message": "Connected Successfully"}]))
        async for raw in ws:
            message = json.loads(raw)
            fake.received.append((ws.request.path, message))
            if message.get("action") == "auth":
                ok = message.get("params") == key
                await ws.send(json.dumps([{"ev": "status", "status": "auth_success" if ok else "auth_failed",
                                           "message": "authenticated" if ok else "authentication failed"}]))
                if not ok:
                    await ws.close(1008)
                    return
            elif message.get("action") == "subscribe":
                await ws.send(json.dumps([{"ev": "status", "status": "success",
                                           "message": f"subscribed to: {message['params']}"}]))
    return script


def trade_alpaca(symbol: str, price: float, ts: str = "2026-11-05T05:42:54.100Z", size: int = 10) -> list:
    return [{"T": "t", "S": symbol, "p": price, "s": size, "t": ts}]


def trade_coinbase(product: str, price: float, ts: str, size: float = 0.01) -> dict:
    return {"type": "match", "product_id": product, "price": str(price), "size": str(size), "time": ts}


class FakeDatabento:
    """The Databento live gateway: greeting, CRAM challenge, authentication, subscription lines and DBN records."""

    def __init__(self, key: str) -> None:
        self.key = key
        self.lines: list[str] = []
        self.writers: set = set()
        self.port = 0
        self._thread: Thread | None = None
        self._server = None
        self._streams: dict = {}

    def start(self, port: int = 0) -> "FakeDatabento":
        self._thread = Thread()
        self._thread.call(self._start(port))
        return self

    async def _start(self, port: int) -> None:
        from databento.common import cram

        async def handle(reader, writer):
            writer.write(b"lsg_version=0.1\n")
            writer.write(b"cram=abcdef\n")
            await writer.drain()
            auth = (await reader.readline()).decode()
            if not auth.startswith("auth=" + cram.get_challenge_response("abcdef", self.key)):
                writer.write(b"success=0|error=Authentication failed\n")
                await writer.drain()
                writer.close()
                return
            writer.write(b"success=1|session_id=fake\n")
            await writer.drain()
            symbols: list[str] = []
            while True:
                line = await reader.readline()
                if not line:
                    break
                text = line.decode().strip()
                self.lines.append(text)
                if text.startswith("schema=") and "symbols=" in text:
                    symbols += dict(p.partition("=")[::2] for p in text.split("|"))["symbols"].split(",")
                if text.startswith("start_session"):
                    self._begin(writer, symbols)
                    self.writers.add(writer)
                elif text.startswith("schema=") and writer in self.writers:
                    self._mappings(writer, symbols)
            self.writers.discard(writer)

        self._server = await asyncio.start_server(handle, "127.0.0.1", port)
        self.port = self._server.sockets[0].getsockname()[1]

    def _begin(self, writer, symbols: list[str]) -> None:
        import databento_dbn as dbn

        metadata = dbn.Metadata(dataset="GLBX.MDP3", start=0, stype_in=dbn.SType.CONTINUOUS,
                                stype_out=dbn.SType.INSTRUMENT_ID, schema=dbn.Schema.TRADES, symbols=symbols,
                                version=3)
        writer.write(bytes(metadata.encode()))
        self._mappings(writer, symbols)

    def _mappings(self, writer, symbols: list[str]) -> None:
        import databento_dbn as dbn

        for index, symbol in enumerate(symbols):
            record = dbn.SymbolMappingMsg(1, 40 + index, 0, dbn.SType.CONTINUOUS, symbol, dbn.SType.INSTRUMENT_ID,
                                          str(40 + index), 0, 2 ** 63)
            writer.write(bytes(record))

    def push_trade(self, price: float, size: int = 2, instrument_id: int = 40, ts_ms: int | None = None) -> None:
        import databento_dbn as dbn

        stamp = int((ts_ms if ts_ms is not None else time.time() * 1000) * 1_000_000)
        record = dbn.TradeMsg(1, instrument_id, stamp, int(price * 1e9), size, dbn.Action.TRADE if hasattr(
            dbn.Action, "TRADE") else "T", dbn.Side.ASK if hasattr(dbn.Side, "ASK") else "A", 0, stamp)

        async def go():
            for writer in list(self.writers):
                writer.write(bytes(record))
                await writer.drain()
        self._thread.call(go())

    def stop(self) -> None:
        if self._thread is None:
            return

        async def go():
            self._server.close()
            for writer in list(self.writers):
                writer.close()
        self._thread.call(go())
        self._thread.stop()
        self._thread = None


DEAD_URL = "ws://127.0.0.1:9/"                                   # nothing listens there: a refused connection


def catalog_with(urls: dict[str, str], polling_interval: float = 0.2, **overrides) -> ProviderCatalog:
    """The shipped provider catalog with every host pointed at a local fake server (or at a dead port, so a test
    never reaches the real internet). `overrides` is `{provider_id: {field: value}}` applied on top, for example a
    smaller `max_symbols`."""
    raw = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
    for provider_id, item in raw["providers"].items():
        if "url" in item:
            item["url"] = DEAD_URL
        if "host" in item:
            item["host"], item["port"] = "127.0.0.1", 9
    for provider_id, url in urls.items():
        if url.startswith("tcp://"):
            host, _, port = url[6:].partition(":")
            raw["providers"][provider_id]["host"], raw["providers"][provider_id]["port"] = host, int(port)
        else:
            raw["providers"][provider_id]["url"] = url
    for provider_id, fields in overrides.items():
        raw["providers"][provider_id].update(fields)
    raw["polling"]["interval_seconds"] = polling_interval
    return parse_catalog(raw)

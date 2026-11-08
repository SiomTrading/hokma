"""SiomTrading Futures stream: `wss://{host}/v1/stream/futures`, the host fixed by the package in `providers.json`.

The owner's access token goes in the first message (`hello`), never in the URL, a header, a log line or an event.
Server messages: `welcome` (limits, data state, instruments), `subscribed`, `tick`, `bar`, `rollover`, `status`,
`error`, `pong`. Close codes: 1001 and 1011 reconnect with the usual backoff (1, 2, 4 ... 30 s); 1008 and 4403 are
final until the owner pastes another token; 4409 is final too (another device took the stream); 4429 waits for the
`retry_after_ms` of the error before it, never less than 30 s.
"""
import asyncio
import json
from dataclasses import replace
from urllib.parse import urlsplit

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from hokma import __version__
from hokma.market.adapters.ws_base import MAX_MESSAGE_BYTES, OPEN_TIMEOUT, WebSocketSource
from hokma.market.providers import ProviderSpec
from hokma.market.stream import BarUpdate, Rollover, SourceStatus, SymbolError, TerminalError, Tick

CLIENT_NAME = f"hokma/{__version__}"
PROTOCOL = 1
PING_SECONDS = 15.0
MIN_RATE_LIMIT_WAIT = 30.0
LOOPBACK = ("127.0.0.1", "localhost", "::1")

# `error.code` that refuses one symbol, as a market catalog code.
SYMBOL_ERRORS = {
    "symbol.unknown": "market.unknown_symbol",
    "symbol.not_entitled": "market.not_entitled",
    "limit.symbols": "market.limit",
}
# Close code that ends the connection for good, as the `SourceStatus.code` the hub maps to a catalog text.
FINAL_CLOSES = {1008: "token_invalid", 4403: "access_revoked", 4409: "other_device"}


class SiomTradingFuturesSource(WebSocketSource):
    ping_seconds = PING_SECONDS
    min_rate_limit_wait = MIN_RATE_LIMIT_WAIT

    def __init__(self, spec: ProviderSpec, token: str, url: str | None = None, **kwargs) -> None:
        super().__init__(spec, url, **kwargs)
        self.__token = token
        parts = urlsplit(self._url_base)
        if parts.scheme != "wss" and not (parts.scheme == "ws" and parts.hostname in LOOPBACK):
            raise ValueError("the futures stream needs a secure address")       # the token never travels in clear
        self._contracts: dict[str, str] = {}
        self._tick_sizes: dict[str, float | None] = {}        # what `welcome` announced, per root
        self._pending: list[str] = []                        # asked, not yet acknowledged
        self._retry_after_ms = 0
        self._ping_task: asyncio.Task | None = None

    # ---- connection ----

    async def _session(self) -> None:
        code: int | None = None
        try:
            async with connect(self._url(), open_timeout=OPEN_TIMEOUT, max_size=MAX_MESSAGE_BYTES,
                               close_timeout=2, ping_interval=20, ping_timeout=20) as ws:
                self._ws = ws
                try:
                    await self._handshake(ws)
                    self._ping_task = asyncio.create_task(self._ping_loop(ws))
                    async for raw in ws:
                        try:
                            message = json.loads(raw)
                        except ValueError:
                            continue
                        self._on_message(message)
                except ConnectionClosed as error:
                    code = error.rcvd.code if error.rcvd else None
                finally:
                    self._ws = None
                    task, self._ping_task = self._ping_task, None
                    if task is not None:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                if code is None:
                    code = ws.close_code
        except InvalidStatus as error:
            if error.response.status_code in (401, 403):
                raise TerminalError("token_invalid") from None
            raise
        self._on_close(code)

    def _on_close(self, code: int | None) -> None:
        if code in FINAL_CLOSES:
            raise TerminalError(FINAL_CLOSES[code])
        if code == 4429:
            self._retry_delay = max(self.min_rate_limit_wait, self._retry_after_ms / 1000)
        self._retry_after_ms = 0

    async def _handshake(self, ws) -> None:
        self._pending.clear()
        await ws.send(json.dumps({"type": "hello", "token": self.__token, "client": CLIENT_NAME,
                                  "protocol": PROTOCOL}, separators=(",", ":")))
        while True:
            message = await self._recv(ws)
            kind = message.get("type") if isinstance(message, dict) else None
            if kind == "welcome":
                self._on_welcome(message)
                break
            if kind == "error":
                self._on_error(message)
        self._mark_ready()
        if self._symbols:
            await self._send_subscribe(sorted(self._symbols))

    def _on_welcome(self, message: dict) -> None:
        state = message.get("data_state")
        if state in ("live", "delayed"):
            self.data_state = state
        limit = message.get("max_symbols")
        delay = message.get("delay_seconds")
        self.capabilities = replace(
            self.capabilities,
            max_symbols=int(limit) if isinstance(limit, int) and limit > 0 else self.capabilities.max_symbols,
            delay_seconds=int(delay) if isinstance(delay, (int, float)) else self.capabilities.delay_seconds)
        for item in message.get("instruments") or []:
            if isinstance(item, dict) and item.get("root") and item.get("contract"):
                self._contracts[str(item["root"])] = str(item["contract"])
                tick = item.get("tick_size")
                self._tick_sizes[str(item["root"])] = float(tick) if isinstance(tick, (int, float)) and tick > 0 else None

    def announced(self) -> dict[str, float | None]:
        """The futures roots the server announced in `welcome`, with their tick size when it said one."""
        return dict(self._tick_sizes)

    async def _ping_loop(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(self.ping_seconds)
                await ws.send('{"type":"ping"}')
        except ConnectionClosed:
            return

    # ---- subscriptions ----

    async def _send(self, payload) -> None:
        try:
            await super()._send(payload)
        except ConnectionClosed:
            pass                     # the connection is ending; the wanted symbols are replayed on the next one

    async def _send_subscribe(self, symbols: list[str]) -> None:
        self._pending += [s for s in symbols if s not in self._pending]
        await self._send({"type": "subscribe", "symbols": symbols})

    async def _send_unsubscribe(self, symbols: list[str]) -> None:
        self._pending = [s for s in self._pending if s not in symbols]
        await self._send({"type": "unsubscribe", "symbols": symbols})

    # ---- messages ----

    def _on_message(self, message) -> None:
        if not isinstance(message, dict):
            return
        kind = message.get("type")
        try:
            if kind == "tick":
                self.emit(Tick(message["symbol"], int(message["ts_ms"]), float(message["price"]),
                               float(message["size"]) if message.get("size") is not None else None))
            elif kind == "bar":
                self.emit(BarUpdate(message["symbol"], int(message["start_ms"]), float(message["o"]),
                                    float(message["h"]), float(message["l"]), float(message["c"]),
                                    float(message.get("v") or 0), bool(message.get("final")),
                                    str(message.get("tf") or "1m")))
            elif kind == "subscribed":
                symbol = str(message["symbol"])
                if symbol in self._pending:
                    self._pending.remove(symbol)
                if message.get("contract"):
                    self._contracts[symbol] = str(message["contract"])
                self.emit(SourceStatus(self.data_state, "subscribed"))
            elif kind == "rollover":
                symbol = str(message["symbol"])
                self._contracts[symbol] = str(message["to_contract"])
                self.emit(Rollover(symbol, str(message["from_contract"]), str(message["to_contract"]),
                                   int(message["at_ms"])))
            elif kind == "status":
                self._on_status(message)
            elif kind == "error":
                self._on_error(message)
        except (KeyError, ValueError, TypeError):
            return

    def _on_status(self, message: dict) -> None:
        state = str(message.get("state") or "")
        code = str(message.get("code") or "")
        if state in ("live", "delayed"):
            self.data_state = state
            self.emit(SourceStatus(state, code or "status"))
        elif state == "reconnecting":
            self.emit(SourceStatus("reconnecting", code or "network"))
        elif state == "closed":
            self.emit(SourceStatus("closed", code or "closed"))

    def _on_error(self, message: dict) -> None:
        code = str(message.get("code") or "")
        if code in ("auth.invalid", "auth.expired"):
            raise TerminalError("token_invalid")
        if code == "limit.rate":
            retry = message.get("retry_after_ms")
            self._retry_after_ms = int(retry) if isinstance(retry, (int, float)) and retry > 0 else 0
            return
        catalog_code = SYMBOL_ERRORS.get(code)
        if catalog_code is None:
            return
        symbol = self._error_symbol(message)
        if symbol is not None:
            if symbol in self._pending:
                self._pending.remove(symbol)
            self.emit(SymbolError(symbol, catalog_code, code))

    def _error_symbol(self, message: dict) -> str | None:
        """The symbol an error is about: its own `symbol` field, the detail when it names one, or the oldest
        subscription the server has not acknowledged."""
        named = message.get("symbol")
        if isinstance(named, str) and named:
            return named
        detail = message.get("detail")
        if isinstance(detail, str) and detail in self._symbols:
            return detail
        return self._pending[0] if self._pending else None

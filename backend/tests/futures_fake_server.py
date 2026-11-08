"""A fake SiomTrading Futures server: the whole contract of `/v1/stream/futures` (section 6.2 of the 0.0.6 scope)
on a local port, with faults a test can inject. Nothing here touches the network.

Server side of the conversation: `hello` within `hello_timeout` seconds (else close 1002), a token check (invalid
or expired: an `error` then close 1008, revoked: close 4403), `welcome`, then `subscribe` / `unsubscribe` / `ping`.
A test pushes data and faults with the methods below; they are safe to call from the test thread.
"""
import asyncio
import json
import time

from live_support import FakeWs

PATH = "/v1/stream/futures"
VALID_TOKEN = "sf-token-0123456789abcdef"
INSTRUMENTS = {
    "NQ": {"contract": "NQZ6", "tick_size": 0.25},
    "ES": {"contract": "ESZ6", "tick_size": 0.25},
    "GC": {"contract": "GCZ6", "tick_size": 0.1},
}


class FuturesFakeServer:
    def __init__(self, tokens=(VALID_TOKEN,), data_state: str = "live", delay_seconds: int = 0, max_symbols: int = 20,
                 hello_timeout: float = 10.0) -> None:
        self.tokens = set(tokens)
        self.revoked: set[str] = set()
        self.expired: set[str] = set()
        self.denied: set[str] = set()                       # symbols outside the token's access
        self.data_state, self.delay_seconds, self.max_symbols = data_state, delay_seconds, max_symbols
        self.hello_timeout = hello_timeout
        self.contracts = {root: item["contract"] for root, item in INSTRUMENTS.items()}
        self.close_next: list[tuple[int, str]] = []         # closes the next connections right after `welcome`
        self.attempts: list[float] = []                     # monotonic time of every connection
        self.headers: list[dict] = []
        self.subscribed: dict[str, set[str]] = {}           # per connection id
        self._fake = FakeWs(self._handler)
        self._ids = 0

    # ---- life ----

    @property
    def url(self) -> str:
        return self._fake.url + PATH

    @property
    def paths(self) -> list[str]:
        return self._fake.paths

    def start(self) -> "FuturesFakeServer":
        self._fake.start()
        return self

    def stop(self) -> None:
        self._fake.stop()

    def messages(self, kind: str | None = None) -> list[dict]:
        items = [m for _, m in self._fake.received if isinstance(m, dict)]
        return items if kind is None else [m for m in items if m.get("type") == kind]

    def symbols(self) -> set[str]:
        """What the live connections are subscribed to."""
        return set().union(*self.subscribed.values()) if self.subscribed else set()

    # ---- what a test injects ----

    def push(self, payload: dict) -> None:
        self._fake.send_all(payload)

    def tick(self, symbol: str, price: float, ts_ms: int, size: float = 1) -> None:
        self.push({"type": "tick", "symbol": symbol, "contract": self.contracts.get(symbol, symbol), "ts_ms": ts_ms,
                   "price": price, "size": size})

    def bar(self, symbol: str, start_ms: int, o: float, h: float, l: float, c: float, v: float = 1,
            final: bool = False) -> None:
        self.push({"type": "bar", "symbol": symbol, "contract": self.contracts.get(symbol, symbol), "tf": "1m",
                   "start_ms": start_ms, "o": o, "h": h, "l": l, "c": c, "v": v, "final": final})

    def status(self, state: str, code: str = "") -> None:
        self.push({"type": "status", "state": state, "code": code})

    def error(self, code: str, detail: str = "", **extra) -> None:
        self.push({"type": "error", "code": code, "detail": detail, **extra})

    def rollover(self, symbol: str, to_contract: str, at_ms: int) -> None:
        previous, self.contracts[symbol] = self.contracts[symbol], to_contract
        self.push({"type": "rollover", "symbol": symbol, "from_contract": previous, "to_contract": to_contract,
                   "at_ms": at_ms})

    def _fire(self, coroutine) -> None:
        """Run on the server's loop without waiting: a close waits for the client, which may be the caller's loop."""
        asyncio.run_coroutine_threadsafe(coroutine, self._fake._thread.loop)

    def close_clients(self, code: int = 1011, reason: str = "") -> None:
        async def go():
            for ws in list(self._fake.clients):
                await ws.close(code, reason)
        self._fire(go())

    def revoke(self, token: str) -> None:
        """The access was withdrawn: live connections close with 4403 and new ones are refused the same way."""
        self.revoked.add(token)
        self.close_clients(4403, "Access revoked")

    def other_device(self) -> None:
        self.close_clients(4409, "Connected from another device")

    def rate_limit(self, retry_after_ms: int) -> None:
        async def go():
            for ws in list(self._fake.clients):
                await ws.send(json.dumps({"type": "error", "code": "limit.rate", "detail": "too many messages",
                                          "retry_after_ms": retry_after_ms}))
                await ws.close(4429, "Rate limit")
        self._fire(go())

    # ---- the server's side of one connection ----

    async def _refuse(self, ws, code: int, reason: str, error: str | None = None) -> None:
        if error is not None:
            await ws.send(json.dumps({"type": "error", "code": error, "detail": reason}))
        await ws.close(code, reason)

    async def _handler(self, ws, fake: FakeWs) -> None:
        self.attempts.append(time.monotonic())
        self.headers.append(dict(ws.request.headers))
        if ws.request.path != PATH:
            await ws.close(1008, "unknown path")
            return
        self._ids += 1
        mine = str(self._ids)
        try:
            try:
                first = json.loads(await asyncio.wait_for(ws.recv(), self.hello_timeout))
            except (asyncio.TimeoutError, ValueError):
                await ws.close(1002, "hello required")
                return
            fake.received.append((ws.request.path, first))
            if not isinstance(first, dict) or first.get("type") != "hello" or first.get("protocol") != 1:
                await ws.close(1002, "hello required")
                return
            token = first.get("token")
            if token in self.revoked:
                await self._refuse(ws, 4403, "Access revoked")
                return
            if token in self.expired:
                await self._refuse(ws, 1008, "Token expired", "auth.expired")
                return
            if token not in self.tokens:
                await self._refuse(ws, 1008, "Token not valid", "auth.invalid")
                return
            await ws.send(json.dumps({
                "type": "welcome", "protocol": 1, "max_symbols": self.max_symbols, "data_state": self.data_state,
                "delay_seconds": self.delay_seconds,
                "instruments": [{"root": root, "contract": self.contracts[root], "tick_size": item["tick_size"],
                                 "session_tz": "America/Chicago", "expires_at": None}
                                for root, item in INSTRUMENTS.items()]}))
            if self.close_next:
                code, reason = self.close_next.pop(0)
                await ws.close(code, reason)
                return
            self.subscribed[mine] = set()
            async for raw in ws:
                message = json.loads(raw)
                fake.received.append((ws.request.path, message))
                await self._answer(ws, mine, message)
        finally:
            self.subscribed.pop(mine, None)

    async def _answer(self, ws, mine: str, message: dict) -> None:
        kind = message.get("type")
        if kind == "ping":
            await ws.send(json.dumps({"type": "pong"}))
        elif kind == "subscribe":
            for symbol in message.get("symbols", []):
                root = symbol[:2] if symbol not in INSTRUMENTS else symbol
                if root not in INSTRUMENTS:
                    await ws.send(json.dumps({"type": "error", "code": "symbol.unknown", "detail": symbol,
                                              "symbol": symbol}))
                elif symbol in self.denied or root in self.denied:
                    await ws.send(json.dumps({"type": "error", "code": "symbol.not_entitled", "detail": symbol,
                                              "symbol": symbol}))
                elif symbol not in self.subscribed[mine] and len(self.subscribed[mine]) >= self.max_symbols:
                    await ws.send(json.dumps({"type": "error", "code": "limit.symbols", "detail": symbol,
                                              "symbol": symbol}))
                else:
                    self.subscribed[mine].add(symbol)
                    await ws.send(json.dumps({"type": "subscribed", "symbol": symbol,
                                              "contract": self.contracts.get(root, symbol)}))
        elif kind == "unsubscribe":
            self.subscribed[mine].difference_update(message.get("symbols", []))

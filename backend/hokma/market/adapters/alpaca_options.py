"""The Alpaca option quotes by stream (SPEC-016 5.2, A2): `wss://stream.data.alpaca.markets/v1beta1/indicative`, MessagePack only (JSON is refused with
error 412), channels `trades` and `quotes` (no greeks, no bars), 200 quotes on the free plan (error 405 past that). Only `quotes` are asked: bid and ask of
the contracts on screen. The `indicative` feed is derived from OPRA: its quotes are not OPRA quotes (said by the owner's message when Alpaca is connected).

Its own connection, apart from the stock stream (the stock one is JSON). The fields of a quote are those of the official page (Iris, 21-nov): `T`, `S`
symbol, `t` RFC-3339 text with nanoseconds (not a number), `bx` and `bp` and `bs` bid exchange, price and size, `ax` and `ap` and `as` ask, `c` condition. Not
documented there, assumed by analogy with stocks: that the channels are named `quotes` and `trades`, and whether trades count in the 200.
"""
import asyncio
import logging
import random

from websockets.asyncio.client import connect

from hokma.core import tls
from hokma.market.adapters.ws_base import MAX_MESSAGE_BYTES, OPEN_TIMEOUT, parse_iso_ms
from hokma.market.msgpack_lite import MsgpackError, packb, unpackb
from hokma.market.stream import BACKOFF_SECONDS, LimitReached, OptionQuote

log = logging.getLogger("hokma.market")
SYMBOL_LIMIT_CODE = 405
MAX_QUOTES = 200                      # the free plan (Basic): 200 quotes on one WebSocket (official, about-market-data-api)
AUTH_CODES = (401, 402, 403, 404, 412)


class AlpacaOptionsFeed:
    def __init__(self, url: str, credentials: dict, emit, backoff: tuple[float, ...] | None = None) -> None:
        self._url = url
        self.__credentials = {"key": credentials["key_id"], "secret": credentials["secret"]}
        self._emit = emit
        self._backoff = backoff if backoff is not None else BACKOFF_SECONDS
        self._want: set[str] = set()
        self._sent: set[str] = set()
        self._ws = None
        self._runner: asyncio.Task | None = None
        self._closing = False
        self.terminal = False                       # the account has no access to this feed: it is not asked again

    def __repr__(self) -> str:
        return "<AlpacaOptionsFeed>"                # nothing it holds is shown

    async def start(self) -> None:
        if self._runner is None or self._runner.done():
            self._closing = False
            self._runner = asyncio.create_task(self._run(), name="alpaca-options")

    async def stop(self) -> None:
        self._closing = True
        runner, self._runner = self._runner, None
        if runner is not None:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)

    async def want(self, keys: list[str]) -> None:
        """A star is never asked (error 413 for option quotes). Past the 200 quotes of the plan the contract is not asked at all: it is told as limited."""
        keys = [k for k in keys if k and k != "*"]
        room = max(0, MAX_QUOTES - len(self._want))
        fresh = [k for k in keys if k not in self._want]
        accepted, over = fresh[:room], fresh[room:]
        self._want.update(accepted)
        if over:
            self._emit(LimitReached("options", tuple(over)))
        await self.start()
        await self._reconcile()

    async def drop(self, keys: list[str]) -> None:
        self._want.difference_update(keys)
        await self._reconcile()

    async def _reconcile(self) -> None:
        ws = self._ws
        if ws is None:
            return
        add, gone = sorted(self._want - self._sent), sorted(self._sent - self._want)
        if add:
            await ws.send(packb({"action": "subscribe", "quotes": add}))
            self._sent.update(add)
        if gone:
            await ws.send(packb({"action": "unsubscribe", "quotes": gone}))
            self._sent.difference_update(gone)

    async def _run(self) -> None:
        attempt = 0
        while not self._closing and not self.terminal:
            ready = False
            try:
                ready = await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as error:               # a drop: only its class is logged, never its text (it can hold a URL or a key)
                log.info("alpaca options stream dropped: %s", type(error).__name__)
            self._ws, self._sent = None, set()
            if self._closing or self.terminal:
                return
            attempt = 0 if ready else attempt + 1
            await asyncio.sleep(self._backoff[min(attempt, len(self._backoff) - 1)] * (0.75 + random.random() / 2))

    async def _session(self) -> bool:
        ssl_context = tls.outbound_context() if self._url.startswith("wss://") else None
        async with connect(self._url, open_timeout=OPEN_TIMEOUT, max_size=MAX_MESSAGE_BYTES, close_timeout=2, ping_interval=20,
                           ping_timeout=20, ssl=ssl_context) as ws:
            await asyncio.wait_for(ws.recv(), OPEN_TIMEOUT)                              # [{"T":"success","msg":"connected"}]
            await ws.send(packb({"action": "auth", **self.__credentials}))
            for item in self._decode(await asyncio.wait_for(ws.recv(), OPEN_TIMEOUT)):
                if item.get("T") == "error":
                    self.terminal = int(item.get("code", 0)) in AUTH_CODES
                    return False
            self._ws = ws
            await self._reconcile()
            async for raw in ws:
                for item in self._decode(raw):
                    self._on_item(item)
            return True

    @staticmethod
    def _decode(raw) -> list[dict]:
        try:
            data = unpackb(raw if isinstance(raw, (bytes, bytearray)) else str(raw).encode())
        except MsgpackError as error:
            log.warning("alpaca options stream: a message was dropped (%s, %d bytes)", error, len(raw) if hasattr(raw, "__len__") else 0)   # no content, only its size
            return []
        return [i for i in (data if isinstance(data, list) else [data]) if isinstance(i, dict)]

    def _on_item(self, item: dict) -> None:
        kind = item.get("T")
        try:
            if kind == "q":
                bid, ask = item.get("bp"), item.get("ap")
                self._emit(OptionQuote(item["S"], parse_iso_ms(item["t"]), _num(bid), _num(ask), None, None, _num(item.get("bs")), _num(item.get("as"))))      # t: RFC-3339 text
            elif kind == "error" and int(item.get("code", 0)) == SYMBOL_LIMIT_CODE:
                over = tuple(sorted(self._sent))                                          # the limit answered: what was asked last did not all enter
                self._emit(LimitReached("options", over[-20:]))
        except (KeyError, ValueError, TypeError):
            return


def _num(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

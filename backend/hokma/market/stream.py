"""The contract of a live market source. Every provider is an adapter that fulfils it; the hub does not know which.

    adapter.connect()                    start the connection in the background; progress arrives as SourceStatus
    adapter.subscribe(symbols)           never blocks; applied now or when the connection is ready, and again after
                                         every reconnection
    adapter.unsubscribe(symbols)
    adapter.events()                     async iterator of MarketEvent
    adapter.close()

Symbols on this surface are the PROVIDER symbols (`to_provider` in `symbols.py`); the hub maps them back.
A secret handed to an adapter stays inside it: it is never part of an event, a repr or a log line.
"""
import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from hokma.market.models import SymbolHit, Series
from hokma.market.errors import MarketError

BACKOFF_SECONDS = (1, 2, 4, 8, 16, 30)
QUEUE_LIMIT = 20000
log = logging.getLogger("hokma.market")


@dataclass(frozen=True)
class SourceCapabilities:
    streaming: bool
    max_symbols: int
    asset_classes: tuple[str, ...]
    delay_seconds: int | None = 0
    ticks: bool = True              # the source reports trade by trade; False: no seconds or range bars from it (SPEC-016 3.3)
    clock_aligned: bool = False     # it sends every session as it comes: the hub folds the intervals it does not give on the clock
    provider_days: bool = False     # it gives the day candle itself (Quote fields of the day): days and weeks are never folded from minutes
    live_options: bool = False      # it streams option contracts (`subscribe_options`): the chain moves cell by cell


@dataclass(frozen=True)
class Quote:
    """Level 1 of a symbol (SPEC-016 3.2), whole: the source's last value of every field, never a partial message.
    What the source does not give is None; nothing is calculated here except `mark` when the source has none and has
    bid and ask (then `mark_calculated`). `regular_last` is the last price of the regular session, as the source gives it.
    `session`: `regular` when the source says the last trade was a regular one, `extended` when it says it was not, None
    when it says nothing. Pre and post are told apart only by the hours of the provider (`market_hours`), never here."""
    symbol: str
    ts_ms: int
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    mark: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    last_size: float | None = None
    volume: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    prev_close: float | None = None
    regular_last: float | None = None
    session: str | None = None
    status: str | None = None
    delayed: bool = False
    mark_calculated: bool = False
    regular_ts_ms: int | None = None          # time of the last regular-session trade: the day the open, high and low belong to


@dataclass(frozen=True)
class OptionQuote:
    """Level 1 of an option contract (SPEC-016 4.3), whole: the last value of every field the source sent. `symbol` is the key of the source
    as it came; the greeks, the IV and the theoretical value are the source's, never calculated here."""
    symbol: str
    ts_ms: int
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    mark: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    volume: float | None = None
    open_interest: float | None = None
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    rho: float | None = None
    theoretical: float | None = None
    underlying_price: float | None = None
    status: str | None = None
    delayed: bool = False


@dataclass(frozen=True)
class LimitReached:
    """The source's limit of symbols answered a subscription: these `symbols` did not enter and are not asked again (SPEC-016 4.4)."""
    kind: str                     # "options" | "equity" | ...
    symbols: tuple[str, ...]


@dataclass(frozen=True)
class Tick:
    symbol: str
    ts_ms: int
    price: float
    size: float | None = None


@dataclass(frozen=True)
class BarUpdate:
    symbol: str
    start_ms: int
    o: float
    h: float
    l: float
    c: float
    v: float
    final: bool = False
    tf: str = "1m"


@dataclass(frozen=True)
class SourceStatus:
    """state: live | delayed (connected, with that data state) | reconnecting | down (terminal: needs the owner).
    `code` is a stable short word (`connected`, `subscribed`, `network`, `auth_failed`, ...)."""
    state: str
    code: str = ""
    detail: str = ""


@dataclass(frozen=True)
class SymbolError:
    """The provider refused one symbol. `code` is a market catalog code, for example `market.unknown_symbol`."""
    symbol: str
    code: str
    detail: str = ""


@dataclass(frozen=True)
class Rollover:
    """The contract behind a futures root changed. The hub sends the panel a new snapshot."""
    symbol: str
    from_contract: str
    to_contract: str
    at_ms: int


MarketEvent = Tick | Quote | OptionQuote | LimitReached | BarUpdate | SourceStatus | SymbolError | Rollover


class LiveMarketSource(Protocol):
    name: str
    capabilities: SourceCapabilities

    async def connect(self) -> None: ...
    async def subscribe(self, symbols: list[str]) -> None: ...
    async def unsubscribe(self, symbols: list[str]) -> None: ...
    def events(self) -> AsyncIterator[MarketEvent]: ...
    async def close(self) -> None: ...


class TerminalError(Exception):
    """Raised inside an adapter session when retrying cannot help (bad key, access revoked). `code` becomes the
    `SourceStatus(down, code)`; the hub maps it to a `connections.*` catalog code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code, self.detail = code, detail


class UnsupportedHistory:
    """Live adapters stream only. `MarketDataSource` asks for `search` and `history`; the bars of a snapshot come
    from the history source the hub holds."""

    def search(self, query: str) -> list[SymbolHit]:
        return []

    def history(self, symbol: str, timeframe: str) -> Series:
        raise MarketError("unsupported", 501)


class LiveSource(UnsupportedHistory):
    """Common base: the event queue, the desired symbol set and the run loop with its backoff.

    A subclass implements `_session()`: connect, authenticate, replay `self._symbols`, read until the connection
    ends. It raises TerminalError for a refusal that retrying cannot fix and any other exception for a drop."""

    name = ""
    provider_id = ""
    capabilities = SourceCapabilities(True, 0, (), 0)
    data_state = "live"

    def __init__(self, backoff: tuple[float, ...] | None = None) -> None:
        self._backoff = backoff if backoff is not None else BACKOFF_SECONDS
        self._queue: asyncio.Queue[MarketEvent] | None = None
        self._symbols: set[str] = set()
        self._runner: asyncio.Task | None = None
        self._closing = False
        self._wake: asyncio.Event | None = None
        self._ready = False
        self._retry_delay: float | None = None       # a session may ask for a wait of its own before the next try

    def __repr__(self) -> str:                       # never show anything a subclass holds
        return f"<{type(self).__name__} {self.provider_id}>"

    # ---- contract ----

    async def connect(self) -> None:
        if self._runner is not None and not self._runner.done():
            return
        self._closing = False
        self._queue = self._queue or asyncio.Queue(QUEUE_LIMIT)
        self._wake = asyncio.Event()
        self._runner = asyncio.create_task(self._run(), name=f"market-{self.provider_id}")

    async def subscribe(self, symbols: list[str]) -> None:
        fresh = [s for s in symbols if s not in self._symbols]
        self._symbols.update(symbols)
        if fresh and self._ready:
            await self._send_subscribe(fresh)

    async def unsubscribe(self, symbols: list[str]) -> None:
        gone = [s for s in symbols if s in self._symbols]
        self._symbols.difference_update(symbols)
        if gone and self._ready:
            await self._send_unsubscribe(gone)

    async def events(self) -> AsyncIterator[MarketEvent]:
        self._queue = self._queue or asyncio.Queue(QUEUE_LIMIT)
        while True:
            event = await self._queue.get()
            if event is _STOP:
                return
            yield event

    async def close(self) -> None:
        self._closing = True
        self._ready = False
        runner, self._runner = self._runner, None
        if runner is not None:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        await self._after_close()
        if self._queue is not None:
            self._queue.put_nowait(_STOP) if not self._queue.full() else None

    # ---- for subclasses ----

    def emit(self, event: MarketEvent) -> None:
        if self._queue is None:
            return
        if self._queue.full():                        # keep the newest; a status is never the one dropped
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self._queue.put_nowait(event)

    async def _session(self) -> None:
        raise NotImplementedError

    async def _send_subscribe(self, symbols: list[str]) -> None: ...

    async def _send_unsubscribe(self, symbols: list[str]) -> None: ...

    async def _after_close(self) -> None: ...

    def _mark_ready(self, code: str = "connected") -> None:
        self._ready = True
        self.emit(SourceStatus(self.data_state, code))

    # ---- run loop ----

    async def _run(self) -> None:
        attempt = 0
        while not self._closing:
            try:
                await self._session()
                code = "closed"
            except asyncio.CancelledError:
                raise
            except TerminalError as error:
                self._ready = False
                self.emit(SourceStatus("down", error.code, error.detail))
                return
            except Exception as error:                # a drop: classify without ever echoing the message
                code = classify_error(error)
            started_ready = self._ready
            self._ready = False
            if self._closing:
                return
            attempt = 0 if started_ready else attempt + 1
            self.emit(SourceStatus("reconnecting", code))
            delay, self._retry_delay = self._retry_delay, None
            await asyncio.sleep(delay if delay is not None else self._backoff[min(attempt, len(self._backoff) - 1)])


_STOP: MarketEvent = SourceStatus("down", "stop")


def classify_error(error: BaseException) -> str:
    """A short code for a connection failure. Never includes the exception text (it can carry a URL with a key)."""
    status = getattr(getattr(error, "response", None), "status_code", None)
    if status in (401, 403):
        return "auth_failed"
    if status == 429:
        return "rate_limited"
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return "timeout"
    if isinstance(error, OSError):
        return "network"
    return "dropped"

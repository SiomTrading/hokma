"""Databento live feed through the official `databento` client (a binary TCP protocol, not a WebSocket).

The client runs on threads of its own and calls back from them; every event crosses to the event loop with
`call_soon_threadsafe`. The client is created by `live_factory`, so a test points it at a local fake gateway and
the code never imports the vendor client at module load (the interface below is all the rest of Hokma sees).
A session per dataset: futures on `GLBX.MDP3`, equities on `DBEQ.BASIC`.
"""
import asyncio
import logging
from collections.abc import Callable

from hokma.market.providers import ProviderSpec
from hokma.market.stream import (LiveSource, SourceCapabilities, SourceStatus, Tick, TerminalError,
                                 classify_error)
from hokma.market.symbols import databento_dataset

log = logging.getLogger("hokma.market")
SCHEMA = "trades"


def default_live_factory(key: str, gateway: str | None, port: int | None):
    import databento

    if gateway:
        return databento.Live(key=key, gateway=gateway, port=port or 13000)
    return databento.Live(key=key)


class _Session:
    def __init__(self, dataset: str, live) -> None:
        self.dataset, self.live = dataset, live
        self.ids: dict[int, str] = {}
        self.started = False
        self.subscribed: set[str] = set()


class DatabentoSource(LiveSource):
    def __init__(self, spec: ProviderSpec, key: str, gateway: str | None = None, port: int | None = None,
                 live_factory: Callable = default_live_factory, backoff: tuple[float, ...] | None = None,
                 poll_seconds: float = 2.0) -> None:
        super().__init__(backoff)
        self.provider_id, self.name, self.data_state = spec.id, spec.name, spec.data_state
        self.capabilities = SourceCapabilities(True, spec.max_symbols, spec.asset_classes, spec.delay_seconds)
        self.__key = key
        self._gateway, self._port = gateway or spec.host, port or spec.port
        self._factory = live_factory
        self._poll = poll_seconds
        self._sessions: dict[str, _Session] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock: asyncio.Lock | None = None
        self._worker: asyncio.Task | None = None
        self._dirty: asyncio.Event | None = None
        self._announced = False

    # ---- contract: nothing here blocks on the network ----

    async def connect(self) -> None:
        if self._worker is not None and not self._worker.done():
            return
        self._closing = False
        self._loop = asyncio.get_running_loop()
        self._queue = self._queue or asyncio.Queue(20000)
        self._dirty = asyncio.Event()
        self._worker = asyncio.create_task(self._work(), name="market-databento")

    async def subscribe(self, symbols: list[str]) -> None:
        self._symbols.update(symbols)
        if self._dirty is not None:
            self._dirty.set()

    async def unsubscribe(self, symbols: list[str]) -> None:
        self._symbols.difference_update(symbols)
        if self._dirty is not None:
            self._dirty.set()

    async def close(self) -> None:
        self._closing = True
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        await asyncio.to_thread(self._terminate_all)
        if self._queue is not None and not self._queue.full():
            from hokma.market.stream import _STOP
            self._queue.put_nowait(_STOP)

    # ---- worker: reconcile the desired symbols with the vendor sessions ----

    async def _work(self) -> None:
        attempt = 0
        while not self._closing:
            try:
                await self._reconcile()
                attempt = 0
                await self._watch()
            except asyncio.CancelledError:
                raise
            except TerminalError as error:
                self.emit(SourceStatus("down", error.code, error.detail))
                return
            except Exception as error:
                code = classify_vendor_error(error)
                if code == "auth_failed":
                    self.emit(SourceStatus("down", code))
                    await asyncio.to_thread(self._terminate_all)
                    return
                self.emit(SourceStatus("reconnecting", code))
                await asyncio.to_thread(self._terminate_all)
                self._sessions.clear()
                self._announced = False
                await asyncio.sleep(self._backoff[min(attempt, len(self._backoff) - 1)])
                attempt += 1

    async def _watch(self) -> None:
        """Wait for a change of symbols or a dropped session; either returns so the worker reconciles again."""
        assert self._dirty is not None
        while not self._closing:
            try:
                await asyncio.wait_for(self._dirty.wait(), self._poll)
                self._dirty.clear()
                return
            except asyncio.TimeoutError:
                if any(s.started and not s.live.is_connected() for s in self._sessions.values()):
                    raise OSError("connection lost")

    async def _reconcile(self) -> None:
        wanted: dict[str, set[str]] = {}
        for symbol in self._symbols:
            wanted.setdefault(dataset_of(symbol), set()).add(symbol)
        for dataset, symbols in wanted.items():
            session = self._sessions.get(dataset)
            if session is None:
                session = self._sessions[dataset] = await asyncio.to_thread(self._open, dataset)
            fresh, gone = symbols - session.subscribed, session.subscribed - symbols
            if fresh:
                await asyncio.to_thread(self._subscribe, session, sorted(fresh))
                session.subscribed |= fresh
            if gone:
                await asyncio.to_thread(self._unsubscribe, session, sorted(gone))
                session.subscribed -= gone
            if not session.started and session.subscribed:
                await asyncio.to_thread(session.live.start)
                session.started = True
            if fresh and not self._announced:
                self._announced = True
                self.emit(SourceStatus(self.data_state, "connected"))
            if fresh:
                self.emit(SourceStatus(self.data_state, "subscribed"))
        for dataset in [d for d in self._sessions if d not in wanted]:
            await asyncio.to_thread(self._close_session, self._sessions.pop(dataset))

    # ---- vendor client calls (run in threads) ----

    def _open(self, dataset: str) -> _Session:
        live = self._factory(self.__key, self._gateway, self._port)
        session = _Session(dataset, live)
        live.add_callback(lambda record: self._on_record(session, record), lambda error: self._on_error(error))
        return session

    def _subscribe(self, session: _Session, symbols: list[str]) -> None:
        stype = "continuous" if session.dataset == databento_dataset("future") else "raw_symbol"
        session.live.subscribe(dataset=session.dataset, schema=SCHEMA, symbols=symbols, stype_in=stype)

    def _unsubscribe(self, session: _Session, symbols: list[str]) -> None:
        stype = "continuous" if session.dataset == databento_dataset("future") else "raw_symbol"
        session.live.unsubscribe(schema=SCHEMA, symbols=symbols, stype_in=stype)

    def _close_session(self, session: _Session) -> None:
        try:
            session.live.terminate()
        except Exception:
            pass

    def _terminate_all(self) -> None:
        for session in list(self._sessions.values()):
            self._close_session(session)

    def _on_record(self, session: _Session, record) -> None:
        name = type(record).__name__
        if name == "SymbolMappingMsg":
            session.ids[record.instrument_id] = record.stype_in_symbol
        elif name == "TradeMsg":
            symbol = session.ids.get(record.instrument_id)
            if symbol is not None:
                self._cross(Tick(symbol, record.ts_event // 1_000_000, float(record.pretty_price), float(record.size)))

    def _on_error(self, error: Exception) -> None:
        log.warning("databento session error: %s", type(error).__name__)

    def _cross(self, event) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self.emit, event)


def dataset_of(symbol: str) -> str:
    """Continuous futures symbols (`NQ.c.0`) live on the futures dataset; plain tickers on the equities one."""
    return databento_dataset("future") if ".c." in symbol else databento_dataset("equity")


def classify_vendor_error(error: Exception) -> str:
    """A short code; `auth_failed` is the one that stops retrying."""
    text = f"{type(error).__name__} {error}".lower()
    if "authenticat" in text or "api key" in text or "invalid key" in text:
        return "auth_failed"
    return classify_error(error)

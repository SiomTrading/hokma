"""The fallback when no provider serves a symbol: it wraps a source without streaming (the Yahoo one) and asks for the
latest 1m bars from the backend at the provider's pace. Its data is always `delayed`. The panel never polls: it
receives these bars over `/ws/market` like any other."""
import asyncio
from collections.abc import Callable

from hokma.market.errors import UNKNOWN_SYMBOL, MarketError
from hokma.market.providers import PollingSpec
from hokma.market.source import MarketDataSource
from hokma.market.stream import BarUpdate, LiveSource, SourceCapabilities, SourceStatus

PROVIDER_ID = "yahoo_polling"


class PollingLiveSource(LiveSource):
    provider_id = PROVIDER_ID
    data_state = "delayed"

    def __init__(self, source: Callable[[], MarketDataSource], spec: PollingSpec,
                 backoff: tuple[float, ...] | None = None) -> None:
        super().__init__(backoff)
        self.name = spec.name
        self.capabilities = SourceCapabilities(False, 1_000_000, (), None)
        self._source = source
        self._interval = spec.interval_seconds
        self._poke: asyncio.Event | None = None

    async def subscribe(self, symbols: list[str]) -> None:
        await super().subscribe(symbols)
        if self._poke is not None:
            self._poke.set()

    async def _session(self) -> None:
        self._poke = asyncio.Event()
        self._mark_ready("polling")
        while True:
            await self._poll_once()
            try:
                await asyncio.wait_for(self._poke.wait(), self._interval)
            except asyncio.TimeoutError:
                pass
            self._poke.clear()

    async def _poll_once(self) -> None:
        failures = 0
        symbols = sorted(self._symbols)
        for symbol in symbols:
            try:
                series = await asyncio.to_thread(self._source().history, symbol, "1m")
            except MarketError as error:
                if error.code != UNKNOWN_SYMBOL:              # no 1m bars for this symbol: the snapshot stands
                    failures += 1
                continue
            last = series.candles[-2:]
            for index, candle in enumerate(last):
                self.emit(BarUpdate(symbol, candle.t * 1000, candle.o, candle.h, candle.l, candle.c, candle.v,
                                    final=index < len(last) - 1))
        if failures and failures == len(symbols):
            raise OSError("history unavailable")                        # shows as reconnecting, then retries
        if failures == 0:
            self.emit(SourceStatus("delayed", "polled"))

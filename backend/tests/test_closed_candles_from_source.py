"""SPEC-016 T8 (3.3): the candle that Hokma built from trades (Coinbase, Finnhub) is replaced by the closed candle of the source, once per closed candle."""
import asyncio
import json

from hokma.market.adapters import ADAPTERS, register_adapter
from hokma.market.hub import MarketHub, PanelClient
from hokma.market.models import Candle, Series, SessionPeriod
from hokma.market.providers import get_catalog
from hokma.market.stream import LiveSource, SourceCapabilities, SourceStatus, Tick
from test_hub_streaming import message, run
from test_source_selection import Config

BASE = 1_793_800_800                                  # a multiple of 60


class TradeSource(LiveSource):
    provider_id, name = "coinbase", "Coinbase"

    def __init__(self) -> None:
        super().__init__()
        self.capabilities = SourceCapabilities(True, 100, ("crypto",), 0)

    async def connect(self) -> None:
        import asyncio as _a
        self._queue = self._queue or _a.Queue(1000)
        self.emit(SourceStatus("live", "connected"))

    async def subscribe(self, symbols):
        pass

    async def unsubscribe(self, symbols):
        pass

    async def close(self) -> None:
        self._closing = True


class History:
    name = "Coinbase"

    def __init__(self) -> None:
        self.final = False
        self.dropped: list[str] = []

    def drop_cached(self, symbol):
        self.dropped.append(symbol)

    def search(self, query):
        return []

    def history(self, symbol, base):
        candles = [Candle(BASE - 60, 99, 100.5, 98, 99.8, 6)] if self.final else [Candle(BASE - 60, 99, 100, 98, 99.5, 5)]   # the closed candle, with its numbers
        return Series(symbol, symbol, "crypto", "Coinbase", "USD", "UTC", 0, candles, None, BASE, SessionPeriod(BASE - 86400, BASE + 86400), BASE, source="Coinbase")


def test_when_a_candle_closes_the_hub_reads_the_closed_one_of_the_source_once_and_replaces_it():
    saved = ADAPTERS["coinbase"]
    made = {}
    register_adapter("coinbase", lambda spec, secret, url: made.setdefault("s", TradeSource()))
    history = History()

    async def go():
        config = Config()
        config.enabled, config.keys = {"coinbase": True}, set()
        hub = MarketHub(get_catalog(), config, lambda: history, clock=lambda: BASE + 70)
        client = PanelClient()
        hub.attach_client(client)
        await hub.subscribe(client, "a", "BTC-USD", "1m")
        await message(client, lambda m: m["type"] == "snapshot")
        source = made["s"]
        source.emit(Tick("BTC-USD", (BASE - 30) * 1000, 99.7, 1))             # a trade of the previous minute
        source.emit(Tick("BTC-USD", (BASE + 5) * 1000, 100.2, 1))             # the first trade of the next one closes the previous candle
        history.final = True                                                    # the source now has the closed candle with its own numbers
        snap = await message(client, lambda m: m["type"] == "snapshot" and m["seq"] > 2, timeout=6.0)
        assert history.dropped == ["BTC-USD"]                                   # the memory was dropped: the read is a real one
        closed = next(b for b in snap["bars"] if b["t"] == BASE - 60)
        assert (closed["c"], closed["h"], closed["v"]) == (99.8, 100.5, 6)        # the source's numbers, not the ones built from trades
        assert hub.reconcile_diffs >= 0
        await hub.close()
    try:
        run(go())
    finally:
        register_adapter("coinbase", saved)

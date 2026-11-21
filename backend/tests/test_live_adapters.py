"""The provider adapters, one by one, against fake servers that speak each provider's protocol."""
import asyncio
import json

from hokma.market.adapters import AlpacaSource, CoinbaseSource, FinnhubSource, PollingLiveSource
from hokma.market.stream import SourceStatus, Tick
from live_support import FakeWs, alpaca_script, catalog_with, coinbase_script, finnhub_script
from test_live_market import KEY_ID, SECRET

BACKOFF = (0.05, 0.05, 0.1)


def spec_of(provider_id: str, url: str | None = None):
    return catalog_with({provider_id: url} if url else {}).providers[provider_id]


class Run:
    """Run an adapter on an event loop for the length of one test and collect what it emits."""

    def __init__(self, source) -> None:
        self.source, self.events = source, []

    async def __aenter__(self):
        await self.source.connect()
        self.reader = asyncio.create_task(self._read())
        return self

    async def _read(self) -> None:
        async for event in self.source.events():
            self.events.append(event)

    async def __aexit__(self, *exc) -> None:
        await self.source.close()
        self.reader.cancel()
        await asyncio.gather(self.reader, return_exceptions=True)

    async def until(self, predicate, timeout: float = 5.0):
        end = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < end:
            for event in list(self.events):
                if predicate(event):
                    return event
            await asyncio.sleep(0.01)
        raise AssertionError(f"event not seen; got {self.events}")


def test_coinbase_subscribes_at_once_and_maps_matches_and_tickers_to_ticks():
    fake = FakeWs(coinbase_script).start()
    try:
        async def go():
            source = CoinbaseSource(spec_of("coinbase", fake.url), backoff=BACKOFF)
            async with Run(source) as run:
                await source.subscribe(["ETH-USD"])
                first = await run.until(lambda e: isinstance(e, SourceStatus) and e.code == "connected")
                assert first.state == "live"
                ticker = await run.until(lambda e: isinstance(e, Tick) and e.symbol == "ETH-USD")
                assert ticker.price == 80400.5 and ticker.size is None
                fake.send_all({"type": "match", "product_id": "ETH-USD", "price": "3200.5", "size": "1.25",
                               "time": "2026-11-05T05:43:00.250000Z"})
                match = await run.until(lambda e: isinstance(e, Tick) and e.price == 3200.5)
                assert match.size == 1.25 and match.ts_ms % 1000 == 250
        asyncio.run(go())
        first = fake.messages()[0]
        assert first["type"] == "subscribe" and first["channels"] == ["heartbeat"]   # inside the 5 second window
        assert any(m.get("channels") == ["ticker", "matches"] and m["product_ids"] == ["ETH-USD"]
                   for m in fake.messages())
    finally:
        fake.stop()


def test_coinbase_subscribes_again_after_a_reconnection():
    fake = FakeWs(coinbase_script).start()
    try:
        async def go():
            source = CoinbaseSource(spec_of("coinbase", fake.url), backoff=BACKOFF)
            async with Run(source) as run:
                await source.subscribe(["BTC-USD"])
                await run.until(lambda e: isinstance(e, Tick))
                await asyncio.to_thread(fake.drop_clients)
                await run.until(lambda e: isinstance(e, SourceStatus) and e.state == "reconnecting")
                run.events.clear()
                await run.until(lambda e: isinstance(e, Tick) and e.symbol == "BTC-USD")
        asyncio.run(go())
        assert len([m for m in fake.messages() if m.get("channels") == ["ticker", "matches"]]) == 2
    finally:
        fake.stop()


def test_alpaca_authenticates_by_message_and_maps_trades():
    fake = FakeWs(alpaca_script(KEY_ID, SECRET)).start()
    try:
        async def go():
            source = AlpacaSource(spec_of("alpaca", fake.url), {"key_id": KEY_ID, "secret": SECRET}, backoff=BACKOFF)
            async with Run(source) as run:
                await source.subscribe(["AAPL", "MSFT"])
                await run.until(lambda e: isinstance(e, SourceStatus) and e.code == "connected")
                await run.until(lambda e: isinstance(e, SourceStatus) and e.code == "subscribed")
                fake.send_all([{"T": "t", "S": "MSFT", "p": 410.25, "s": 7, "t": "2026-11-05T14:30:01.123456789Z"}])
                trade = await run.until(lambda e: isinstance(e, Tick))
                assert (trade.symbol, trade.price, trade.size) == ("MSFT", 410.25, 7.0)
        asyncio.run(go())
        assert fake.messages()[0] == {"action": "auth", "key": KEY_ID, "secret": SECRET}
        assert any(m.get("action") == "subscribe" and sorted(m["trades"]) == ["AAPL", "MSFT"] for m in fake.messages())
        assert "key=" not in "".join(fake.paths) and SECRET not in "".join(fake.paths)
    finally:
        fake.stop()


def test_alpaca_with_a_bad_key_stops_for_good_and_does_not_retry():
    fake = FakeWs(alpaca_script(KEY_ID, "other-secret-value-1")).start()
    try:
        async def go():
            source = AlpacaSource(spec_of("alpaca", fake.url), {"key_id": KEY_ID, "secret": SECRET}, backoff=BACKOFF)
            async with Run(source) as run:
                status = await run.until(lambda e: isinstance(e, SourceStatus) and e.state == "down")
                assert status.code == "auth_failed"
                await asyncio.sleep(0.4)
                assert len([m for m in fake.messages() if m.get("action") == "auth"]) == 1
        asyncio.run(go())
    finally:
        fake.stop()


def test_alpaca_connection_limit_is_a_terminal_status_of_its_own():
    async def script(ws, fake):
        await ws.send(json.dumps([{"T": "success", "msg": "connected"}]))
        await ws.recv()
        await ws.send(json.dumps([{"T": "error", "code": 406, "msg": "connection limit exceeded"}]))
    fake = FakeWs(script).start()
    try:
        async def go():
            source = AlpacaSource(spec_of("alpaca", fake.url), {"key_id": KEY_ID, "secret": SECRET}, backoff=BACKOFF)
            async with Run(source) as run:
                status = await run.until(lambda e: isinstance(e, SourceStatus) and e.state == "down")
                assert status.code == "connection_limit"
        asyncio.run(go())
    finally:
        fake.stop()


def test_finnhub_carries_the_key_in_the_connection_url_only_and_subscribes_per_symbol():
    fake = FakeWs(finnhub_script, reject_token="finnhub-key-value-13579").start()
    try:
        async def go():
            source = FinnhubSource(spec_of("finnhub", fake.url), "finnhub-key-value-13579", backoff=BACKOFF)
            async with Run(source) as run:
                await source.subscribe(["AAPL", "OANDA:EUR_USD"])
                status = await run.until(lambda e: isinstance(e, SourceStatus) and e.code == "connected")
                assert status.state == "live"                           # from providers.json
                assert "finnhub-key" not in repr(source) and "finnhub-key" not in repr(status)
                await wait_until_async(lambda: len(fake.messages()) == 2)
                fake.send_all({"type": "ping"})
                fake.send_all({"type": "trade", "data": [{"p": 1.0832, "s": "OANDA:EUR_USD", "t": 1793857400000,
                                                          "v": 0}, {"bad": True}]})
                trade = await run.until(lambda e: isinstance(e, Tick))
                assert (trade.symbol, trade.price, trade.ts_ms) == ("OANDA:EUR_USD", 1.0832, 1793857400000)
                await source.unsubscribe(["AAPL"])
                await wait_until_async(lambda: any(m.get("type") == "unsubscribe" for m in fake.messages()))
        asyncio.run(go())
        assert fake.paths and all("token=finnhub-key-value-13579" in p for p in fake.paths)
        assert sorted(m["symbol"] for m in fake.messages() if m["type"] == "subscribe") == ["AAPL", "OANDA:EUR_USD"]
    finally:
        fake.stop()


async def wait_until_async(predicate, timeout: float = 5.0) -> None:
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


def test_finnhub_with_a_refused_handshake_is_terminal_and_never_logs_the_key(caplog):
    fake = FakeWs(finnhub_script, reject_token="the-right-one-12345").start()
    caplog.set_level("DEBUG")
    try:
        async def go():
            source = FinnhubSource(spec_of("finnhub", fake.url), "the-wrong-one-99999", backoff=BACKOFF)
            async with Run(source) as run:
                status = await run.until(lambda e: isinstance(e, SourceStatus) and e.state == "down")
                assert status.code == "auth_failed" and "wrong-one" not in repr(status)
        asyncio.run(go())
        assert "the-wrong-one-99999" not in caplog.text
    finally:
        fake.stop()


# ------------------------------------------------------------------ base behaviour


def test_an_adapter_never_shows_its_secret_in_its_representation():
    source = AlpacaSource(spec_of("alpaca"), {"key_id": KEY_ID, "secret": SECRET})
    finn = FinnhubSource(spec_of("finnhub"), "finnhub-key-value-13579")
    for adapter in (source, finn, PollingLiveSource(lambda: None, catalog_with({}).polling)):
        text = repr(adapter) + str(adapter)
        assert SECRET not in text and KEY_ID not in text and "finnhub-key" not in text and "k" * 29 not in text


def test_when_the_queue_is_full_the_newest_events_are_kept():
    async def go():
        source = CoinbaseSource(spec_of("coinbase"))
        source._queue = asyncio.Queue(3)
        for price in range(10):
            source.emit(Tick("BTC-USD", price, float(price)))
        return [source._queue.get_nowait().price for _ in range(3)]
    assert asyncio.run(go()) == [7.0, 8.0, 9.0]


def test_the_polling_source_declares_that_it_does_not_stream_and_is_always_delayed():
    source = PollingLiveSource(lambda: None, catalog_with({}).polling)
    assert source.capabilities.streaming is False and source.data_state == "delayed"
    assert source.name == "Yahoo Finance"

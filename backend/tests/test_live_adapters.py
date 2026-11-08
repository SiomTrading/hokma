"""The provider adapters, one by one, against fake servers that speak each provider's protocol, and the premium
adapters through the panel channel (M12)."""
import asyncio
import json

from hokma.market.adapters import (AlpacaSource, CoinbaseSource, DatabentoSource, FinnhubSource, MassiveFuturesSource,
                                   PollingLiveSource)
from hokma.market.stream import BarUpdate, SourceStatus, Tick
from live_fixtures import AUTH, Collector, live  # noqa: F401  (fixture)
from live_support import (FakeDatabento, FakeWs, alpaca_script, catalog_with, coinbase_script, finnhub_script,
                          massive_script, wait_until)
from market_support import RECORDED_AT
from test_live_market import KEY_ID, SECRET, subscribe

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


def test_massive_maps_trades_and_finished_minute_bars():
    fake = FakeWs(massive_script("massive-key-24680")).start()
    try:
        async def go():
            source = MassiveFuturesSource(spec_of("massive", fake.url), "massive-key-24680", backoff=BACKOFF)
            async with Run(source) as run:
                await source.subscribe(["NQZ6"])
                await run.until(lambda e: isinstance(e, SourceStatus) and e.code == "connected")
                await run.until(lambda e: isinstance(e, SourceStatus) and e.code == "subscribed")
                fake.send_all([{"ev": "T", "sym": "NQZ6", "p": 28200.25, "s": 3, "t": 1793857400123},
                               {"ev": "AM", "sym": "NQZ6", "o": 1, "h": 3, "l": 0.5, "c": 2, "v": 40,
                                "s": 1793857380000, "e": 1793857440000}])
                tick = await run.until(lambda e: isinstance(e, Tick))
                bar = await run.until(lambda e: isinstance(e, BarUpdate))
                assert (tick.symbol, tick.price, tick.size, tick.ts_ms) == ("NQZ6", 28200.25, 3.0, 1793857400123)
                assert (bar.start_ms, bar.o, bar.h, bar.l, bar.c, bar.v, bar.final) == (
                    1793857380000, 1, 3, 0.5, 2, 40, True)
        asyncio.run(go())
        subscribe_message = next(m for m in fake.messages() if m.get("action") == "subscribe")
        assert subscribe_message["params"] == "T.NQZ6,AM.NQZ6"
        assert "massive-key-24680" not in "".join(fake.paths)
    finally:
        fake.stop()


def test_massive_with_a_bad_key_is_terminal():
    fake = FakeWs(massive_script("the-real-key-111")).start()
    try:
        async def go():
            source = MassiveFuturesSource(spec_of("massive", fake.url), "some-other-key-222", backoff=BACKOFF)
            async with Run(source) as run:
                assert (await run.until(lambda e: isinstance(e, SourceStatus) and e.state == "down")).code == "auth_failed"
        asyncio.run(go())
    finally:
        fake.stop()


# ------------------------------------------------------------------ Databento over a fake TCP gateway


def test_databento_client_talks_to_a_fake_gateway_and_yields_ticks():
    gateway = FakeDatabento("db-" + "k" * 29).start()
    try:
        async def go():
            spec = spec_of("databento", f"tcp://127.0.0.1:{gateway.port}")
            source = DatabentoSource(spec, "db-" + "k" * 29, backoff=BACKOFF, poll_seconds=0.2)
            async with Run(source) as run:
                await source.subscribe(["NQ.c.0"])
                await run.until(lambda e: isinstance(e, SourceStatus) and e.code == "connected", timeout=10)
                await asyncio.sleep(0.3)
                gateway.push_trade(28201.75, 4, ts_ms=1793857400500)
                tick = await run.until(lambda e: isinstance(e, Tick), timeout=10)
                assert (tick.symbol, tick.price, tick.size, tick.ts_ms) == ("NQ.c.0", 28201.75, 4.0, 1793857400500)
        asyncio.run(go())
        assert any(line.startswith("schema=trades|stype_in=continuous|symbols=NQ.c.0") for line in gateway.lines)
    finally:
        gateway.stop()


def test_databento_with_a_wrong_key_stops_for_good():
    gateway = FakeDatabento("db-" + "k" * 29).start()
    try:
        async def go():
            spec = spec_of("databento", f"tcp://127.0.0.1:{gateway.port}")
            source = DatabentoSource(spec, "db-" + "x" * 29, backoff=BACKOFF, poll_seconds=0.2)
            async with Run(source) as run:
                await source.subscribe(["NQ.c.0"])
                status = await run.until(lambda e: isinstance(e, SourceStatus) and e.state == "down", timeout=10)
                assert status.code == "auth_failed" and "x" * 29 not in repr(status)
        asyncio.run(go())
    finally:
        gateway.stop()


def test_databento_reconnects_after_the_gateway_drops_and_streams_again():
    gateway = FakeDatabento("db-" + "k" * 29).start()
    try:
        async def go():
            spec = spec_of("databento", f"tcp://127.0.0.1:{gateway.port}")
            source = DatabentoSource(spec, "db-" + "k" * 29, backoff=BACKOFF, poll_seconds=0.2)
            async with Run(source) as run:
                await source.subscribe(["NQ.c.0"])
                await run.until(lambda e: isinstance(e, SourceStatus) and e.code == "connected", timeout=10)
                port = gateway.port
                gateway.stop()
                await run.until(lambda e: isinstance(e, SourceStatus) and e.state == "reconnecting", timeout=10)
                gateway.start(port)
                run.events.clear()
                await run.until(lambda e: isinstance(e, SourceStatus) and e.state == "live", timeout=15)
                await asyncio.sleep(0.3)
                gateway.push_trade(28300.5, 1, ts_ms=1793857500000)
                tick = await run.until(lambda e: isinstance(e, Tick), timeout=10)
                assert tick.price == 28300.5
        asyncio.run(go())
    finally:
        gateway.stop()


# ------------------------------------------------------------------ M12: premium adapters reach the panel


def test_m12_databento_and_massive_deliver_ticks_to_the_panel_over_the_same_socket(live):
    gateway = FakeDatabento("db-" + "k" * 29).start()
    massive = FakeWs(massive_script("massive-key-24680")).start()
    try:
        live.use({"databento": f"tcp://127.0.0.1:{gateway.port}", "massive": massive.url})
        with live.app() as client, live.connect(client) as ws:
            c = Collector(ws)
            assert client.put("/api/connections/databento/key", json={"key": "db-" + "k" * 29},
                              headers=AUTH).status_code == 200
            snapshot = subscribe(c, "d", "NQ=F", "5m")
            assert (snapshot["source"], snapshot["data_state"], snapshot["fallback"]) == ("Databento", "live", False)
            assert wait_until(lambda: any(line.startswith("start_session") for line in gateway.lines), 10)
            for i in range(30):                                       # the session starts after the first request
                gateway.push_trade(28250.25 + i, 2, ts_ms=int(RECORDED_AT * 1000) + 3000 + i * 50)
                if any(m["type"] == "bar" for m in c.all):
                    break
                import time as _time
                _time.sleep(0.1)
            bar = c.find(lambda m: m["type"] == "bar" and m["sub"] == "d", timeout=10)
            assert bar["source"] == "Databento" and bar["data_state"] == "live" and bar["bar"]["c"] >= 28250.25
            # the same symbol served by Massive when it comes first in the order
            client.put("/api/connections/massive/key", json={"key": "massive-key-24680"}, headers=AUTH)
            client.put("/api/connections/priority", headers=AUTH,
                       json={"asset_class": "future", "order": ["massive", "databento", "siomtrading_futures"]})
            c.send({"op": "unsubscribe", "sub": "d"})
            assert wait_until(lambda: client.get("/api/connections", headers=AUTH).json()["connections"][4][
                "symbols_in_use"] == 0)
            second = subscribe(c, "m", "NQ=F", "5m")
            assert (second["source"], second["data_state"]) == ("Massive", "live")
            assert wait_until(lambda: any(m.get("action") == "subscribe" and m["params"] == "T.NQZ6,AM.NQZ6"
                                          for m in massive.messages()))
            massive.send_all([{"ev": "T", "sym": "NQZ6", "p": 28333.5, "s": 1,
                               "t": int(RECORDED_AT * 1000) + 2000}])
            got = c.find(lambda m: m["type"] == "bar" and m["sub"] == "m", timeout=10)
            assert got["source"] == "Massive" and got["bar"]["c"] == 28333.5
    finally:
        gateway.stop()
        massive.stop()


# ------------------------------------------------------------------ base behaviour


def test_an_adapter_never_shows_its_secret_in_its_representation():
    source = AlpacaSource(spec_of("alpaca"), {"key_id": KEY_ID, "secret": SECRET})
    finn = FinnhubSource(spec_of("finnhub"), "finnhub-key-value-13579")
    db = DatabentoSource(spec_of("databento"), "db-" + "k" * 29)
    for adapter in (source, finn, db, PollingLiveSource(lambda: None, catalog_with({}).polling)):
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

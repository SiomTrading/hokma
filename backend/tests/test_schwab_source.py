"""SPEC-016 T3: Schwab as a streaming source (adapters/schwab.py) over the fake Streamer. No network, no polling."""
import asyncio

import pytest

from hokma.market.adapters import ADAPTERS, SchwabSource
from hokma.market.adapters import schwab as feed
from hokma.market.providers import get_catalog
from hokma.market.stream import BarUpdate, Quote, SourceStatus, Tick
from hokma.schwab import stream
from schwab_support import FakeSchwab
from streamer_fake_server import StreamerFakeServer
from test_live_adapters import Run
from test_schwab_stream import FAST, BACKOFF, preference

QQQ = "QQQ"
MINUTE = 1_700_000_040_000                                   # a made-up minute, a multiple of 60000


@pytest.fixture
def server():
    fake = StreamerFakeServer().start()
    stream.allow_local_for_tests(True)
    yield fake
    stream.allow_local_for_tests(False)
    fake.stop()


def source_for(server: StreamerFakeServer) -> tuple[SchwabSource, FakeSchwab]:
    rest = FakeSchwab()
    rest.on("GET", "/trader/v1/userPreference", preference(server.url))
    rest.__enter__()
    return SchwabSource(get_catalog().get("schwab"), backoff=BACKOFF, config=FAST), rest


def test_every_registered_adapter_streams():
    """CA11: a provider that is not a stream is not a live source. The registry fails when one says otherwise."""
    secrets = {"alpaca": '{"key_id": "k", "secret": "s"}', "finnhub": "key", "siomtrading_futures": "token"}
    for provider_id, factory in ADAPTERS.items():
        spec = get_catalog().get(provider_id)
        if spec is None:
            continue
        assert factory(spec, secrets.get(provider_id, ""), None).capabilities.streaming is True, provider_id


def test_schwab_says_it_has_no_trade_by_trade_feed_and_does_not_poll():
    spec = get_catalog().get("schwab")
    source = SchwabSource(spec)
    assert source.capabilities.streaming is True and source.capabilities.ticks is False
    assert not hasattr(source, "_poll_once") and not hasattr(feed, "POLL_SECONDS")
    assert "secret" not in repr(source).lower()


def test_the_quote_is_the_whole_state_of_the_symbol_and_never_a_tick(server):
    async def go():
        source, rest = source_for(server)
        try:
            async with Run(source) as run:
                await source.subscribe([QQQ])
                await run.until(lambda e: isinstance(e, SourceStatus) and e.code == "subscribed")
                server.data("LEVELONE_EQUITIES", [{"key": QQQ, "1": 500.1, "2": 500.2, "3": 500.15, "4": 3, "5": 4, "8": 1000,
                                                  "10": 501.0, "11": 499.0, "12": 498.0, "17": 499.5, "29": 500.0, "28": True,
                                                  "33": 500.15, "35": 1_700_000_000_500, "32": "Normal"}])
                first = await run.until(lambda e: isinstance(e, Quote))
                server.data("LEVELONE_EQUITIES", [{"key": QQQ, "3": 500.3}])
                second = await run.until(lambda e: isinstance(e, Quote) and e.last == 500.3)
                assert first.bid == 500.1 and first.ask == 500.2 and first.last == 500.15 and first.mark == 500.15
                assert first.volume == 1000 and first.high == 501.0 and first.low == 499.0 and first.prev_close == 498.0
                assert first.open == 499.5 and first.regular_last == 500.0 and first.session == "regular"
                assert first.status == "Normal" and first.ts_ms == 1_700_000_000_500 and first.delayed is False
                assert second.bid == 500.1 and second.volume == 1000          # the fields that did not change are still there
                assert not [e for e in run.events if isinstance(e, Tick)]
        finally:
            rest.__exit__()
    asyncio.run(go())
    assert server.commands("LEVELONE_EQUITIES", "ADD")[0]["keys"] == QQQ and server.commands("CHART_EQUITY", "ADD")


def test_a_quote_without_mark_gets_the_mean_of_bid_and_ask_marked_as_calculated(server):
    async def go():
        source, rest = source_for(server)
        try:
            async with Run(source) as run:
                await source.subscribe([QQQ])
                await run.until(lambda e: isinstance(e, SourceStatus) and e.code == "subscribed")
                server.data("LEVELONE_EQUITIES", [{"key": QQQ, "1": 10.0, "2": 10.2}])
                quote = await run.until(lambda e: isinstance(e, Quote))
                assert quote.mark == 10.1 and quote.mark_calculated is True and quote.session is None
        finally:
            rest.__exit__()
    asyncio.run(go())


def test_delayed_true_makes_the_source_delayed_and_false_makes_it_live_again(server):
    async def go():
        source, rest = source_for(server)
        try:
            async with Run(source) as run:
                await source.subscribe([QQQ])
                await run.until(lambda e: isinstance(e, SourceStatus) and e.code == "subscribed")
                server.data("LEVELONE_EQUITIES", [{"key": QQQ, "3": 1.0}], delayed=True)
                await run.until(lambda e: isinstance(e, SourceStatus) and e.state == "delayed" and e.code == "data")
                assert (await run.until(lambda e: isinstance(e, Quote) and e.last == 1.0)).delayed is True
                server.data("LEVELONE_EQUITIES", [{"key": QQQ, "3": 2.0}], delayed=False)
                await run.until(lambda e: isinstance(e, SourceStatus) and e.state == "live" and e.code == "data")
        finally:
            rest.__exit__()
    asyncio.run(go())


def test_chart_equity_is_the_candle_of_a_minute_and_it_closes_when_the_next_minute_comes(server):
    def chart(start, o, h, l, c, v):
        server.data("CHART_EQUITY", [{"key": QQQ, "1": o, "2": h, "3": l, "4": c, "5": v, "6": 1, "7": start, "8": 1}])

    async def go():
        source, rest = source_for(server)
        try:
            async with Run(source) as run:
                await source.subscribe([QQQ])
                await run.until(lambda e: isinstance(e, SourceStatus) and e.code == "subscribed")
                chart(MINUTE, 10.0, 10.5, 9.9, 10.2, 100)
                chart(MINUTE, 10.0, 10.6, 9.9, 10.4, 150)
                chart(MINUTE + 60_000, 10.4, 10.5, 10.3, 10.4, 20)
                await run.until(lambda e: isinstance(e, BarUpdate) and e.start_ms == MINUTE + 60_000)
                bars = [e for e in run.events if isinstance(e, BarUpdate)]
                final = [b for b in bars if b.final]
                assert [(b.start_ms, b.o, b.h, b.l, b.c, b.v) for b in final] == [(MINUTE, 10.0, 10.6, 9.9, 10.4, 150)]
                assert bars[0].final is False and bars[-1].final is False
        finally:
            rest.__exit__()
    asyncio.run(go())


def test_unsubscribing_drops_both_services(server):
    async def go():
        source, rest = source_for(server)
        try:
            async with Run(source) as run:
                await source.subscribe([QQQ, "SPY"])
                await run.until(lambda e: isinstance(e, SourceStatus) and e.code == "subscribed")
                await source.unsubscribe(["SPY"])
                for _ in range(200):
                    if server.subscribed.get("CHART_EQUITY") == {QQQ} and server.subscribed.get("LEVELONE_EQUITIES") == {QQQ}:
                        return
                    await asyncio.sleep(0.01)
                raise AssertionError(server.subscribed)
        finally:
            rest.__exit__()
    asyncio.run(go())


def test_a_refused_host_ends_the_source_with_its_code_and_nothing_leaves(server):
    async def go():
        rest = FakeSchwab()
        rest.on("GET", "/trader/v1/userPreference", preference("wss://evil.example/ws"))
        rest.__enter__()
        source = SchwabSource(get_catalog().get("schwab"), backoff=BACKOFF, config=FAST)
        try:
            async with Run(source) as run:
                await source.subscribe([QQQ])
                down = await run.until(lambda e: isinstance(e, SourceStatus) and e.state == "down")
                assert down.code == "connections.schwab_denied"
        finally:
            rest.__exit__()
    asyncio.run(go())
    assert server.attempts == 0 and server.logins == []

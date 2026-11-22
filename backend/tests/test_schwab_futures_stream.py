"""SPEC-016 T22 (3.10): futures by the Schwab Streamer: the services of futures, the contract symbol, the rollover by field 34, the account without
futures, and the single source (Schwab when connected, SiomTrading Futures when not). Fakes only."""
import asyncio
from datetime import date

import pytest

from hokma.market import symbols
from hokma.market.adapters import ADAPTERS, SchwabSource
from hokma.market.providers import get_catalog
from hokma.market.stream import BarUpdate, Quote, Rollover, SourceStatus, SymbolError
from hokma.schwab import stream
from schwab_support import FakeSchwab
from streamer_fake_server import StreamerFakeServer
from test_live_adapters import Run
from test_schwab_stream import BACKOFF, FAST, preference

MINUTE = 1_700_000_040_000


@pytest.fixture
def server():
    fake = StreamerFakeServer().start()
    stream.allow_local_for_tests(True)
    yield fake
    stream.allow_local_for_tests(False)
    fake.stop()


def source_for(server):
    rest = FakeSchwab()
    rest.on("GET", "/trader/v1/userPreference", preference(server.url))
    rest.__enter__()
    return SchwabSource(get_catalog().get("schwab"), backoff=BACKOFF, config=FAST), rest


def test_the_mapping_of_the_symbol_of_hokma_to_the_one_of_schwab():
    assert symbols.to_provider("schwab", "NQ=F", "future", date(2026, 11, 21)) == "/NQ"
    assert symbols.schwab_future("NQ", 12, 2026) == "/NQZ26" and symbols.schwab_future("ES", 3, 2027) == "/ESH27"
    assert symbols.parse_schwab_future("/NQZ26") == ("NQ", 12, 2026)
    assert symbols.parse_schwab_future("/NQ") is None and symbols.parse_schwab_future("QQQ") is None
    assert symbols.to_provider("schwab", "QQQ", "equity") == "QQQ"
    with pytest.raises(ValueError):
        symbols.schwab_future("NQ", 13, 2026)


def test_a_futures_symbol_asks_for_the_futures_services_and_never_the_equity_ones(server):
    async def go():
        source, rest = source_for(server)
        try:
            async with Run(source) as run:
                await source.subscribe(["/NQZ26", "QQQ"])
                await run.until(lambda e: isinstance(e, SourceStatus) and e.code == "subscribed")
                for _ in range(200):
                    if server.subscribed.get("CHART_FUTURES") and server.subscribed.get("LEVELONE_EQUITIES"):
                        break
                    await asyncio.sleep(0.01)
                assert server.subscribed["LEVELONE_FUTURES"] == {"/NQZ26"} and server.subscribed["CHART_FUTURES"] == {"/NQZ26"}
                assert server.subscribed["LEVELONE_EQUITIES"] == {"QQQ"} and server.subscribed["CHART_EQUITY"] == {"QQQ"}
                server.data("LEVELONE_FUTURES", [{"key": "/NQZ26", "1": 28300.0, "2": 28300.5, "3": 28300.25, "8": 100, "18": 28200.0, "34": "/NQZ26"}])
                quote = await run.until(lambda e: isinstance(e, Quote) and e.symbol == "/NQZ26")
                assert (quote.bid, quote.ask, quote.last, quote.open) == (28300.0, 28300.5, 28300.25, 28200.0)
                server.data("CHART_FUTURES", [{"key": "/NQZ26", "1": MINUTE, "2": 10.0, "3": 12.0, "4": 9.0, "5": 11.0, "6": 7}])
                bar = await run.until(lambda e: isinstance(e, BarUpdate) and e.symbol == "/NQZ26")
                assert (bar.start_ms, bar.o, bar.h, bar.l, bar.c, bar.v) == (MINUTE, 10.0, 12.0, 9.0, 11.0, 7.0)
        finally:
            rest.__exit__()
    asyncio.run(go())


def test_the_front_contract_that_schwab_names_in_field_34_makes_a_rollover(server):
    async def go():
        source, rest = source_for(server)
        try:
            async with Run(source) as run:
                await source.subscribe(["/NQ"])
                await run.until(lambda e: isinstance(e, SourceStatus) and e.code == "subscribed")
                server.data("LEVELONE_FUTURES", [{"key": "/NQ", "3": 1.0, "34": "/NQZ26"}])
                await run.until(lambda e: isinstance(e, Quote))
                assert not [e for e in run.events if isinstance(e, Rollover)]          # the first one tells where we are, not a change
                server.data("LEVELONE_FUTURES", [{"key": "/NQ", "3": 2.0, "34": "/NQH27"}])
                roll = await run.until(lambda e: isinstance(e, Rollover))
                assert (roll.symbol, roll.from_contract, roll.to_contract) == ("/NQ", "/NQZ26", "/NQH27")
        finally:
            rest.__exit__()
    asyncio.run(go())


def test_an_account_without_futures_loses_only_the_futures_symbol_and_the_stocks_go_on(server):
    server.unavailable.update({"LEVELONE_FUTURES", "CHART_FUTURES"})

    async def go():
        source, rest = source_for(server)
        try:
            async with Run(source) as run:
                await source.subscribe(["/NQ", "QQQ"])
                refused = await run.until(lambda e: isinstance(e, SymbolError))
                assert (refused.symbol, refused.code) == ("/NQ", "market.futures_not_enabled")
                assert not [e for e in run.events if isinstance(e, SourceStatus) and e.state == "down"]
                server.data("LEVELONE_EQUITIES", [{"key": "QQQ", "3": 500.0}])
                await run.until(lambda e: isinstance(e, Quote) and e.symbol == "QQQ")
        finally:
            rest.__exit__()
    asyncio.run(go())

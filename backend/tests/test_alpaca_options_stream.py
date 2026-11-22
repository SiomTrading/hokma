"""SPEC-016 T7 (5.2, A2): the Alpaca option quotes by msgpack stream, against a fake that speaks it. The fields of a quote are [ASSUMED]."""
import asyncio

import pytest

from hokma.market.adapters import AlpacaSource
from hokma.market.adapters.alpaca_options import AlpacaOptionsFeed
from hokma.market.msgpack_lite import packb, unpackb
from hokma.market.stream import LimitReached, OptionQuote
from live_support import FakeWs
from test_live_adapters import BACKOFF, spec_of

KEY, SECRET = "AKTESTKEYID12345", "alpaca-secret-67890"
CONTRACT = "QQQ261218C00500000"


def script(fail_auth: int | None = None, quote_limit: bool = False):
    async def handler(ws, fake: FakeWs) -> None:
        await ws.send(packb([{"T": "success", "msg": "connected"}]))
        auth = unpackb(await ws.recv())
        fake.received.append(("auth", auth))
        if fail_auth or auth.get("key") != KEY or auth.get("secret") != SECRET:
            await ws.send(packb([{"T": "error", "code": fail_auth or 402, "msg": "auth failed"}]))
            return
        await ws.send(packb([{"T": "success", "msg": "authenticated"}]))
        async for raw in ws:
            message = unpackb(raw)                                     # a JSON text would not unpack: the stream is msgpack only
            fake.received.append(("op", message))
            if quote_limit and message.get("action") == "subscribe":
                await ws.send(packb([{"T": "error", "code": 405, "msg": "symbol limit exceeded"}]))
    return handler


async def until(predicate, timeout=3.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("not reached")


def test_it_authenticates_in_msgpack_asks_only_quotes_and_emits_bid_and_ask_of_each_contract():
    fake = FakeWs(script()).start()
    events: list = []

    async def go():
        feed = AlpacaOptionsFeed(fake.url, {"key_id": KEY, "secret": SECRET}, events.append, backoff=BACKOFF)
        try:
            await feed.want([CONTRACT])
            await until(lambda: any(k == "op" for k, _ in fake.received))
            async def push():
                for ws in list(fake.clients):
                    await ws.send(packb([{"T": "q", "S": CONTRACT, "bp": 1.25, "bs": 3, "ap": 1.3, "as": 4, "t": "2026-11-20T15:00:00.250Z"}]))
            fake._thread.call(push())
            await until(lambda: any(isinstance(e, OptionQuote) for e in events))
        finally:
            await feed.stop()
    try:
        asyncio.run(go())
    finally:
        fake.stop()
    ops = [m for k, m in fake.received if k == "op"]
    assert ops[0] == {"action": "subscribe", "quotes": [CONTRACT]}       # quotes only: no trades, no wildcard
    quote = next(e for e in events if isinstance(e, OptionQuote))
    assert (quote.symbol, quote.bid, quote.ask, quote.bid_size, quote.ask_size) == (CONTRACT, 1.25, 1.3, 3.0, 4.0)
    assert quote.delta is None and quote.iv is None                     # the stream has no greeks: nothing is made up


def test_a_star_is_never_asked_and_past_200_quotes_the_contract_is_told_as_limited_and_not_sent():
    events: list = []
    feed = AlpacaOptionsFeed("ws://127.0.0.1:9", {"key_id": KEY, "secret": SECRET}, events.append, backoff=BACKOFF)

    async def go():
        feed.start = lambda: asyncio.sleep(0)                      # no connection here: only what would be asked
        await feed.want(["*", ""] + [f"QQQ261218C{i:08d}" for i in range(205)])
    asyncio.run(go())
    assert len(feed._want) == 200 and "*" not in feed._want
    assert [e.kind for e in events] == ["options"] and len(events[0].symbols) == 5
    assert events[0].symbols[0] == "QQQ261218C00000200"


def test_a_message_that_cannot_be_read_is_dropped_and_logged_without_its_content(caplog):
    import logging

    caplog.set_level(logging.WARNING)
    assert AlpacaOptionsFeed._decode(b"\xc1\x00") == []
    record = next(r for r in caplog.records if "dropped" in r.getMessage())
    assert "2 bytes" in record.getMessage() and "\\xc1" not in record.getMessage()


def test_the_time_of_a_quote_is_rfc3339_text_with_nanoseconds():
    events: list = []
    feed = AlpacaOptionsFeed("ws://127.0.0.1:9", {"key_id": KEY, "secret": SECRET}, events.append)
    feed._on_item({"T": "q", "S": CONTRACT, "t": "2026-11-20T15:00:00.123456789Z", "bx": "C", "bp": 1.0, "bs": 1, "ax": "C", "ap": 1.1, "as": 2, "c": "A"})
    assert events[0].ts_ms == 1_795_186_800_123


def test_a_refused_account_stops_asking_and_the_limit_of_symbols_is_told():
    fake = FakeWs(script(fail_auth=402)).start()

    async def refused():
        feed = AlpacaOptionsFeed(fake.url, {"key_id": KEY, "secret": SECRET}, lambda e: None, backoff=BACKOFF)
        await feed.want([CONTRACT])
        await until(lambda: feed.terminal)
        await feed.stop()
    try:
        asyncio.run(refused())
    finally:
        fake.stop()
    limited = FakeWs(script(quote_limit=True)).start()
    events: list = []

    async def over():
        feed = AlpacaOptionsFeed(limited.url, {"key_id": KEY, "secret": SECRET}, events.append, backoff=BACKOFF)
        try:
            await feed.want([CONTRACT])
            await until(lambda: any(isinstance(e, LimitReached) for e in events))
        finally:
            await feed.stop()
    try:
        asyncio.run(over())
    finally:
        limited.stop()
    assert next(e for e in events if isinstance(e, LimitReached)).kind == "options"


def test_the_alpaca_source_says_it_streams_options_and_gives_its_keys_without_spaces():
    source = AlpacaSource(spec_of("alpaca"), {"key_id": KEY, "secret": SECRET}, backoff=BACKOFF)
    assert source.capabilities.live_options is True and source.capabilities.streaming is True
    assert "alpaca-secret" not in repr(source) and "alpaca-secret" not in repr(AlpacaOptionsFeed("wss://x", {"key_id": KEY, "secret": SECRET}, print))

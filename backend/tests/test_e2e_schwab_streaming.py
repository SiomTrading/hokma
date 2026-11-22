"""SPEC-016 T13: the whole path with fakes at the edges only: the REAL hub, the REAL SchwabSource, the REAL Streamer session and the REAL history
chain, against a fake Streamer and a fake Schwab REST. A chart is opened, the price moves by itself, the connection drops and comes back, and nothing
is ever asked of `/quotes`. Dates: Wednesday 2024-11-06 (New York is UTC-5)."""
import asyncio
import json
import threading
from datetime import datetime, timezone

import pytest

from hokma.market import stream as market_stream
from hokma.market.history import ConnectedSource
from hokma.market.hub import MarketHub, PanelClient
from hokma.market.providers import get_catalog
from hokma.schwab import stream
from schwab_support import FakeSchwab
from streamer_fake_server import StreamerFakeServer
from test_hub_streaming import run
from test_schwab_market import NOW, OPEN_930, SCHWAB_BARS, Config, Yahoo, bar, no_other_provider
from test_schwab_stream import preference

HISTORY, HOURS = "/marketdata/v1/pricehistory", "/marketdata/v1/markets/equity"
PRE = OPEN_930 - 5400                                          # 08:00 New York: pre-market


async def seen(client, store: list, predicate, timeout: float = 6.0) -> dict:
    """Everything the panel received is kept in `store`: a message that arrived while waiting for another is not lost."""
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        while not client.outbox.empty():
            store.append(json.loads(client.outbox.get_nowait()))
        found = next((m for m in store if predicate(m)), None)
        if found is not None:
            return found
        await asyncio.sleep(0.02)
    raise AssertionError(f"not seen; got {[(m['type'], m.get('state')) for m in store]}")


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_LANG", "en")
    monkeypatch.setattr(stream, "BACKOFF_SECONDS", (0.05, 0.05))
    monkeypatch.setattr(market_stream, "BACKOFF_SECONDS", (0.05, 0.05))
    server = StreamerFakeServer().start()
    stream.allow_local_for_tests(True)
    rest = FakeSchwab()
    gate = threading.Event()                                # while it is clear, the session cannot get its preferences: the outage lasts until the test says
    gate.set()
    body = preference(server.url)
    rest.on("GET", "/trader/v1/userPreference", lambda call: (gate.wait(15), body)[1])
    five = [bar(PRE, 10, 10.2, 9.9, 10.1, 5), bar(OPEN_930, 10.1, 10.6, 10.0, 10.4, 100), bar(OPEN_930 + 300, 10.4, 10.8, 10.3, 10.7, 80)]
    rest.on("GET", HISTORY, lambda call: {**SCHWAB_BARS, "candles": five if call.query.get("frequencyType") == "minute" else [bar(OPEN_930 - 14 * 3600 + 5 * 3600, 9.5, 10.9, 9.4, 10.0, 1000)]})
    day = "2024-11-06"
    rest.on("GET", HOURS, {"equity": {"EQ": {"date": day, "marketType": "EQUITY", "isOpen": True, "sessionHours": {
        "preMarket": [{"start": f"{day}T07:00:00-05:00", "end": f"{day}T09:30:00-05:00"}],
        "regularMarket": [{"start": f"{day}T09:30:00-05:00", "end": f"{day}T16:00:00-05:00"}],
        "postMarket": [{"start": f"{day}T16:00:00-05:00", "end": f"{day}T20:00:00-05:00"}]}}}})
    rest.__enter__()
    chain = ConnectedSource(Yahoo(), Config(), get_catalog(), tmp_path / "cache", fetch=no_other_provider, clock=lambda: NOW)
    hub = MarketHub(get_catalog(), Config(), lambda: chain, clock=lambda: NOW)
    hub.watch.show_after, hub.watch.notify_after = 0.05, 0.1
    hub.watch.set_notifier(lambda text: None)
    yield hub, server, rest, gate
    gate.set()
    rest.__exit__()
    stream.allow_local_for_tests(False)
    server.stop()


def test_a_chart_by_streaming_from_the_snapshot_to_a_reconnection_without_asking_quotes(world):
    hub, server, rest, gate = world

    async def go():
        client = PanelClient()
        store: list = []
        hub.attach_client(client)
        await hub.subscribe(client, "a", "QQQ", "5m")
        snap = await seen(client, store, lambda m: m["type"] == "snapshot", timeout=8.0)
        assert snap["source"] == "Schwab" and snap["fallback"] is False and snap["history"]["source"] == "Schwab"
        assert [b["t"] for b in snap["bars"]][:2] == [PRE, OPEN_930]                          # pre-market is a candle like any other
        assert [s["name"] for s in snap["sessions"]] == ["preMarket", "regularMarket", "postMarket"] and snap["market_open"] is True
        for _ in range(300):
            if server.subscribed.get("LEVELONE_EQUITIES") == {"QQQ"} and server.subscribed.get("CHART_EQUITY") == {"QQQ"}:
                break
            await asyncio.sleep(0.02)
        server.data("LEVELONE_EQUITIES", [{"key": "QQQ", "1": 10.7, "2": 10.8, "3": 10.75, "12": 10.0, "33": 10.75, "35": (NOW) * 1000}])
        last = await seen(client, store, lambda m: m["type"] in ("last", "bar") and m.get("price") == 10.75)
        assert last["quote"]["bid"] == 10.7 and last["quote"]["ask"] == 10.8 and round(last["change"], 2) == 0.75
        server.data("CHART_EQUITY", [{"key": "QQQ", "1": 10.7, "2": 11.0, "3": 10.6, "4": 10.9, "5": 40, "6": 1, "7": (OPEN_930 + 600) * 1000}])
        bar_message = await seen(client, store, lambda m: m["type"] == "bar" and m["bar"]["t"] == OPEN_930 + 600)
        assert bar_message["bar"]["h"] == 11.0 and bar_message["final"] is False
        # the connection drops: the symbol stays, the band comes, the session logs in again and replays everything
        # The outage is held open until the band has been seen: the band has a debounce (the loss must last), and a reconnection that is faster than it
        # is, rightly, no alert at all. A fixed wait would make the test depend on the speed of the machine.
        gate.clear()
        server.subscribed.clear()
        server.close_clients(1011)
        await seen(client, store, lambda m: m["type"] == "broker_alert" and m["state"] == "lost", timeout=6.0)
        gate.set()                                                                       # the network is back: the session logs in again
        again = await seen(client, store, lambda m: m["type"] == "snapshot" and m["seq"] > snap["seq"], timeout=8.0)
        assert again["source"] == "Schwab"
        await seen(client, store, lambda m: m["type"] == "broker_alert" and m["state"] == "recovered", timeout=6.0)
        for _ in range(300):
            if server.subscribed.get("LEVELONE_EQUITIES") == {"QQQ"}:
                break
            await asyncio.sleep(0.02)
        assert server.subscribed.get("LEVELONE_EQUITIES") == {"QQQ"} and server.log.count("ADMIN LOGIN") == 2
        await hub.close()
    run(go())
    assert not [c for c in rest.calls if c.path.endswith("/quotes")]                  # CA9: not one periodic call to /quotes

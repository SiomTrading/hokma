"""Block e: the SiomTrading Futures adapter against a fake server that speaks the whole contract (6.2).

Adapter level first (messages, close codes, waits), then the panel level F1 to F7 through `/ws/market` and
`/api/connections`. Nothing touches the network."""
import asyncio
import json
import logging
import os
import time

import pytest

from hokma import __version__
from hokma.core.config import hokma_data_dir
from hokma.engine.session import SECRET_ENV
from hokma.market.adapters import ADAPTERS, SiomTradingFuturesSource
from hokma.market.stream import BarUpdate, Rollover, SourceStatus, SymbolError, Tick
from futures_fake_server import VALID_TOKEN, FuturesFakeServer
from live_fixtures import AUTH, Collector, live  # noqa: F401  (fixture)
from live_support import wait_until
from market_support import RECORDED_AT, fixture_text
from test_live_adapters import BACKOFF, Run, spec_of
from test_live_market import subscribe

T0 = int(RECORDED_AT * 1000)


@pytest.fixture
def server():
    fake = FuturesFakeServer().start()
    yield fake
    fake.stop()


def source_for(fake: FuturesFakeServer, token: str = VALID_TOKEN) -> SiomTradingFuturesSource:
    return SiomTradingFuturesSource(spec_of("siomtrading_futures", fake.url), token, backoff=BACKOFF)


def card(client) -> dict:
    return next(c for c in client.get("/api/connections", headers=AUTH).json()["connections"]
                if c["id"] == "siomtrading_futures")


def save_token(client, token: str = VALID_TOKEN) -> None:
    assert client.put("/api/connections/siomtrading_futures/key", json={"key": token},
                      headers=AUTH).status_code == 200


def connection_state(c: Collector, state: str, after: int = 0) -> dict:
    return c.find(lambda m: m["type"] == "connection_status" and m["provider"] == "siomtrading_futures"
                  and m["state"] == state, after=after)


# ------------------------------------------------------------------ adapter level


def test_the_adapter_says_hello_first_with_the_token_in_the_message_and_not_in_the_address(server):
    async def go():
        source = source_for(server)
        async with Run(source) as run:
            await source.subscribe(["NQ", "NQZ6"])
            welcome = await run.until(lambda e: isinstance(e, SourceStatus) and e.code == "connected")
            assert welcome.state == "live"
            await run.until(lambda e: isinstance(e, SourceStatus) and e.code == "subscribed")
    asyncio.run(go())
    first = server.messages()[0]
    assert first == {"type": "hello", "token": VALID_TOKEN, "client": f"hokma/{__version__}", "protocol": 1}
    assert server.paths == ["/v1/stream/futures"] and VALID_TOKEN not in json.dumps(server.headers)
    assert server.messages("subscribe")[0]["symbols"] == ["NQ", "NQZ6"]       # a root and an exact contract


def test_it_maps_ticks_bars_status_and_the_refusal_of_one_symbol(server):
    server.max_symbols = 1

    async def go():
        source = source_for(server)
        async with Run(source) as run:
            await source.subscribe(["NQ"])
            await run.until(lambda e: isinstance(e, SourceStatus) and e.code == "subscribed")
            assert source.capabilities.max_symbols == 1                      # the limit the server announced
            server.tick("NQ", 28200.25, T0 + 1000, 3)
            server.bar("NQ", T0, 1, 3, 0.5, 2, 40, final=True)
            server.status("delayed", "feed_lag")
            await source.subscribe(["ES", "ZZ"])
            tick = await run.until(lambda e: isinstance(e, Tick))
            bar = await run.until(lambda e: isinstance(e, BarUpdate))
            assert (tick.symbol, tick.ts_ms, tick.price, tick.size) == ("NQ", T0 + 1000, 28200.25, 3.0)
            assert (bar.start_ms, bar.o, bar.h, bar.l, bar.c, bar.v, bar.final, bar.tf) == (T0, 1, 3, 0.5, 2, 40, True, "1m")
            delayed = await run.until(lambda e: isinstance(e, SourceStatus) and e.code == "feed_lag")
            assert delayed.state == "delayed" and source.data_state == "delayed"
            limit = await run.until(lambda e: isinstance(e, SymbolError) and e.symbol == "ES")
            assert limit.code == "market.limit"
            unknown = await run.until(lambda e: isinstance(e, SymbolError) and e.symbol == "ZZ")
            assert unknown.code == "market.unknown_symbol"
    asyncio.run(go())


def test_the_ping_goes_out_on_its_interval_and_a_pong_is_harmless(server, monkeypatch):
    monkeypatch.setattr(SiomTradingFuturesSource, "ping_seconds", 0.1)

    async def go():
        async with Run(source_for(server)) as run:
            await run.until(lambda e: isinstance(e, SourceStatus) and e.code == "connected")
            await asyncio.sleep(0.5)
            assert not [e for e in run.events if isinstance(e, SymbolError)]
    asyncio.run(go())
    assert len(server.messages("ping")) >= 3


def test_the_default_ping_interval_is_fifteen_seconds():
    assert SiomTradingFuturesSource.ping_seconds == 15.0


def test_a_rollover_is_passed_on_and_the_source_keeps_the_symbol(server):
    async def go():
        source = source_for(server)
        async with Run(source) as run:
            await source.subscribe(["NQ"])
            await run.until(lambda e: isinstance(e, SourceStatus) and e.code == "subscribed")
            server.rollover("NQ", "NQH7", T0)
            event = await run.until(lambda e: isinstance(e, Rollover))
            assert (event.symbol, event.from_contract, event.to_contract, event.at_ms) == ("NQ", "NQZ6", "NQH7", T0)
    asyncio.run(go())


@pytest.mark.parametrize("code", [1001, 1011])
def test_1001_and_1011_reconnect_and_replay_the_subscriptions(server, code):
    async def go():
        source = source_for(server)
        async with Run(source) as run:
            await source.subscribe(["NQ"])
            await run.until(lambda e: isinstance(e, SourceStatus) and e.code == "subscribed")
            server.close_clients(code)
            down = await run.until(lambda e: isinstance(e, SourceStatus) and e.state == "reconnecting")
            end = asyncio.get_running_loop().time() + 5
            while not any(isinstance(e, SourceStatus) and e.code == "subscribed"
                          for e in run.events[run.events.index(down) + 1:]):
                assert asyncio.get_running_loop().time() < end, run.events
                await asyncio.sleep(0.01)
    asyncio.run(go())
    assert len(server.attempts) == 2 and len(server.messages("subscribe")) == 2


@pytest.mark.parametrize("code,reason,expected", [(1008, "", "token_invalid"), (4403, "", "access_revoked"),
                                                  (4409, "", "other_device")])
def test_final_closes_stop_the_source_for_good(server, code, reason, expected):
    server.close_next = [(code, reason)]

    async def go():
        source = source_for(server)
        async with Run(source) as run:
            down = await run.until(lambda e: isinstance(e, SourceStatus) and e.state == "down")
            assert down.code == expected
            await asyncio.sleep(0.5)                                         # backoff here is 0.05 s: it would loop
    asyncio.run(go())
    assert len(server.attempts) == 1


@pytest.mark.parametrize("retry_ms,wait", [(100, 0.6), (1200, 1.2)])
def test_4429_waits_for_retry_after_and_never_less_than_the_minimum(server, monkeypatch, retry_ms, wait):
    monkeypatch.setattr(SiomTradingFuturesSource, "min_rate_limit_wait", 0.6)

    async def go():
        source = source_for(server)
        async with Run(source) as run:
            await run.until(lambda e: isinstance(e, SourceStatus) and e.code == "connected")
            server.rate_limit(retry_ms)
            await run.until(lambda e: isinstance(e, SourceStatus) and e.state == "reconnecting")
            await asyncio.sleep(wait + 0.8)
    asyncio.run(go())
    assert len(server.attempts) == 2
    assert server.attempts[1] - server.attempts[0] >= wait - 0.05


def test_the_adapter_refuses_an_address_that_would_carry_the_token_in_clear():
    spec = spec_of("siomtrading_futures")
    with pytest.raises(ValueError):
        SiomTradingFuturesSource(spec, "token", "ws://stream.example.com/v1/stream/futures")
    SiomTradingFuturesSource(spec, "token", "wss://stream.example.com/v1/stream/futures")
    assert "token" not in repr(SiomTradingFuturesSource(spec, "token-secret-9", "wss://x.example.com/p"))


def test_the_shipped_card_is_available_and_first_for_futures(live):
    assert ADAPTERS["siomtrading_futures"] is not None
    with live.app() as client:
        assert card(client)["available"] is True
        assert card(client)["priority"] == {"future": 0}
        assert card(client)["max_symbols"] == 20


# ------------------------------------------------------------------ panel level, F1 to F7


@pytest.mark.parametrize("state", ["live", "delayed"])
def test_f1_a_healthy_server_connects_and_nq_carries_the_data_state_of_welcome(live, server, state):
    server.data_state = state
    live.use({"siomtrading_futures": server.url})
    with live.app() as client, live.connect(client) as ws:
        save_token(client)
        c = Collector(ws)
        subscribe(c, "n", "NQ=F", "5m")
        snapshot = c.find(lambda m: m["type"] == "snapshot" and m["data_state"] == state, timeout=8)
        assert snapshot["source"] == "SiomTrading Futures" and snapshot["fallback"] is False
        assert wait_until(lambda: card(client)["state"] == "connected")
        assert card(client)["data_state"] == state and card(client)["symbols_in_use"] == 1
        assert connection_state(c, "connected")["data_state"] == state
        assert wait_until(lambda: server.symbols() == {"NQ"})
        for i in range(4):
            server.tick("NQ", 28300.0 + i, T0 + 2000 + i * 400)
            time.sleep(0.3)
        bars = [m for m in c.all if m["type"] == "bar" and m["sub"] == "n"]
        assert wait_until(lambda: len([m for m in c.all if m["type"] == "bar"]) >= 2)
        bars = [m for m in c.all if m["type"] == "bar" and m["sub"] == "n"]
        assert all(m["source"] == "SiomTrading Futures" and m["data_state"] == state for m in bars)
        assert bars[-1]["bar"]["c"] >= 28301.0
        seqs = [m["seq"] for m in c.all if m.get("sub") == "n"]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
        assert c.of("bar", "n")[-1]["seq"] > snapshot["seq"]
    hello = server.messages("hello")[0]
    assert hello["client"] == f"hokma/{__version__}" and hello["protocol"] == 1 and hello["token"] == VALID_TOKEN


@pytest.mark.parametrize("mode,code,text", [("invalid", "connections.token_invalid", "Token not valid"),
                                           ("revoked", "connections.access_revoked", "Access revoked"),
                                           ("device", "connections.other_device", "Connected from another device")])
def test_f2_a_refused_token_is_an_error_card_and_one_attempt(live, server, mode, code, text):
    if mode == "revoked":
        server.revoked.add(VALID_TOKEN)
    if mode == "device":
        server.close_next = [(4409, "Connected from another device")]
    live.use({"siomtrading_futures": server.url})
    live.fetcher.overrides["NQ=F"] = (200, fixture_text("nqf_5m.json"))
    with live.app() as client, live.connect(client) as ws:
        save_token(client, "wrong-token-123456" if mode == "invalid" else VALID_TOKEN)
        c = Collector(ws)
        subscribe(c, "n", "NQ=F", "5m")
        assert connection_state(c, "error")["error_code"] == code
        time.sleep(1.5)                                    # a loop at the test backoff would make dozens of tries
        assert len(server.attempts) == 1
        assert card(client)["state"] == "error" and card(client)["error_message"] == text
        moved = c.find(lambda m: m["type"] == "snapshot" and m["fallback"], timeout=6)
        assert moved["data_state"] == "delayed"
        if mode == "invalid":                              # a new token is the only way back
            save_token(client)
            assert wait_until(lambda: card(client)["state"] == "connected", 8)
            assert len(server.attempts) == 2


def test_f2_a_token_revoked_while_connected_stops_without_retrying(live, server):
    live.use({"siomtrading_futures": server.url})
    with live.app() as client, live.connect(client) as ws:
        save_token(client)
        c = Collector(ws)
        subscribe(c, "n", "NQ=F", "5m")
        assert wait_until(lambda: card(client)["state"] == "connected")
        server.revoke(VALID_TOKEN)
        assert connection_state(c, "error")["error_code"] == "connections.access_revoked"
        time.sleep(1.0)
        assert len(server.attempts) == 1


def test_f3_a_1011_drop_comes_back_without_duplicate_bars(live, server):
    live.use({"siomtrading_futures": server.url})
    with live.app() as client, live.connect(client) as ws:
        save_token(client)
        c = Collector(ws)
        first = subscribe(c, "n", "NQ=F", "5m")
        assert wait_until(lambda: card(client)["state"] == "connected")
        server.tick("NQ", 28300.0, T0 + 2000)
        before = c.find(lambda m: m["type"] == "bar", timeout=5)
        mark = len(c.all)
        server.close_clients(1011)
        connection_state(c, "reconnecting", after=mark)
        connection_state(c, "connected", after=mark)
        again = c.find(lambda m: m["type"] == "snapshot" and m["seq"] > first["seq"], after=mark, timeout=6)
        times = [bar["t"] for bar in again["bars"]]
        assert times == sorted(times) and len(times) == len(set(times))
        assert wait_until(lambda: len(server.messages("subscribe")) == 2)
        server.tick("NQ", 28305.0, T0 + 2500)
        after = c.find(lambda m: m["type"] == "bar" and m["bar"]["c"] == 28305.0, after=mark, timeout=5)
        assert after["bar"]["t"] == before["bar"]["t"] == times[-1]          # it updates the candle, no second one
        assert [m["bar"]["t"] for m in c.all[mark:] if m["type"] == "bar"].count(times[-1]) >= 1
        assert len(set(times)) == len(times)
    assert len(server.attempts) == 2


def test_f4_a_rollover_sends_a_new_snapshot_and_the_stream_goes_on(live, server):
    live.use({"siomtrading_futures": server.url})
    with live.app() as client, live.connect(client) as ws:
        save_token(client)
        c = Collector(ws)
        subscribe(c, "n", "NQ=F", "5m")
        assert wait_until(lambda: server.symbols() == {"NQ"})
        server.tick("NQ", 28300.0, T0 + 2000)
        before = c.find(lambda m: m["type"] == "bar", timeout=5)
        mark = len(c.all)
        server.rollover("NQ", "NQH7", T0 + 2200)
        again = c.find(lambda m: m["type"] == "snapshot" and m["seq"] > before["seq"], after=mark, timeout=6)
        assert again["sub"] == "n" and again["bars"] and again["source"] == "SiomTrading Futures"
        server.tick("NQ", 28411.0, T0 + 2600)
        assert c.find(lambda m: m["type"] == "bar" and m["bar"]["c"] == 28411.0 and m["seq"] > again["seq"], after=mark)


def test_f5_a_symbol_outside_the_access_is_an_error_and_the_others_continue(live, server):
    server.denied = {"GC"}
    live.use({"siomtrading_futures": server.url})
    live.fetcher.overrides["GC=F"] = (200, fixture_text("nqf_5m.json"))
    with live.app() as client, live.connect(client) as ws:
        save_token(client)
        c = Collector(ws)
        assert subscribe(c, "n", "NQ=F", "5m")["type"] == "snapshot"
        subscribe(c, "g", "GC=F", "5m")
        refused = c.find(lambda m: m["type"] == "error" and m.get("sub") == "g")
        assert refused["code"] == "market.not_entitled" and "Connections" in refused["message"]
        server.tick("NQ", 28310.0, T0 + 2000)
        assert c.find(lambda m: m["type"] == "bar" and m["sub"] == "n")["bar"]["c"] == 28310.0
        assert card(client)["state"] == "connected"


def test_f6_the_token_is_nowhere_but_the_system_store(live, server, caplog, capfd):
    caplog.set_level(logging.DEBUG)
    logging.getLogger().setLevel(logging.DEBUG)
    bad = "sf-bad-token-9876543210"
    live.use({"siomtrading_futures": server.url})
    with live.app() as client, live.connect(client) as ws:
        http_texts = []
        c = Collector(ws)
        http_texts.append(client.put("/api/connections/siomtrading_futures/key", json={"key": bad},
                                     headers=AUTH).text)
        subscribe(c, "n", "NQ=F", "5m")
        connection_state(c, "error")                       # the bad token went through the whole path
        save_token(client)
        assert wait_until(lambda: card(client)["state"] == "connected", 8)
        server.tick("NQ", 28300.0, T0 + 2000)
        c.find(lambda m: m["type"] == "bar")
        for path in ("/api/connections", ):
            response = client.get(path, headers=AUTH)
            http_texts.append(response.text + json.dumps(dict(response.headers)))
        http_texts.append(client.post("/api/connections/siomtrading_futures/test", headers=AUTH).text)
        http_texts.append(client.delete("/api/connections/siomtrading_futures/key", headers=AUTH).text)
        time.sleep(0.5)
        socket_text = json.dumps([{k: v for k, v in m.items() if k != "_at"} for m in c.all])
        secrets = (VALID_TOKEN, bad)
        everything = {"websocket": socket_text, "http": "\n".join(http_texts), "logs": caplog.text,
                      "output": "".join(capfd.readouterr())}
        for where, text in everything.items():
            for secret in secrets:
                assert secret not in text, f"token found in {where}"
        for path in hokma_data_dir().rglob("*"):
            if path.is_file():
                assert not any(s.encode() in path.read_bytes() for s in secrets), f"token found in {path}"
        assert not [v for v in os.environ.values() if any(s in v for s in secrets)]
        claude_env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
        assert not [v for v in claude_env.values() if any(s in v for s in secrets)]
    assert server.paths and all(p == "/v1/stream/futures" for p in server.paths)           # no query string at all
    assert not any(s in json.dumps(server.headers) for s in (VALID_TOKEN, bad))
    assert {m["token"] for m in server.messages("hello")} == {bad, VALID_TOKEN}          # it travels only in `hello`


def test_f7_without_a_token_nq_falls_back_to_the_delayed_source_and_nothing_connects(live, server):
    live.use({"siomtrading_futures": server.url})
    with live.app() as client, live.connect(client) as ws:
        c = Collector(ws)
        snapshot = subscribe(c, "n", "NQ=F", "5m")
        assert (snapshot["type"], snapshot["fallback"], snapshot["data_state"]) == ("snapshot", True, "delayed")
        assert snapshot["asset_class"] == "future" and snapshot["source"] == "Yahoo Finance"
        assert card(client)["key_state"] == "missing" and card(client)["state"] == "not_connected"
        time.sleep(0.3)
    assert server.attempts == []

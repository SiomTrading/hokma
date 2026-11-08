"""A provider that plugs into the hub with `register_adapter` (this is how the SiomTrading futures adapter arrives):
a source that reports its own data state, refuses one symbol, rolls a contract, drops and returns, and is refused for
good. The source here lives in the test process; the hub sees it like any other adapter."""
import asyncio

from hokma.market.adapters import ADAPTERS
from hokma.market.stream import LiveSource, Rollover, SourceCapabilities, SourceStatus, SymbolError, Tick
from live_fixtures import AUTH, Collector, live  # noqa: F401  (fixture)
from live_support import wait_until
from market_support import RECORDED_AT, fixture_text
from test_live_market import subscribe


class PluggedFutures(LiveSource):
    provider_id = "siomtrading_futures"
    name = "SiomTrading Futures"
    denied: set[str] = set()
    instances: list["PluggedFutures"] = []

    def __init__(self, spec) -> None:
        super().__init__()
        self.capabilities = SourceCapabilities(True, spec.max_symbols, spec.asset_classes, 0)
        self.data_state = "delayed"                               # what a server `welcome` would say
        self.subscribed: list[str] = []
        PluggedFutures.instances.append(self)

    async def _session(self) -> None:
        self._mark_ready()
        await asyncio.Event().wait()

    async def _send_subscribe(self, symbols: list[str]) -> None:
        self.subscribed += symbols
        for symbol in symbols:
            if symbol in self.denied:
                self.emit(SymbolError(symbol, "market.not_entitled", "symbol.not_entitled"))


def plug(monkeypatch, denied=()):
    PluggedFutures.instances = []
    PluggedFutures.denied = set(denied)
    monkeypatch.setitem(ADAPTERS, "siomtrading_futures", lambda spec, secret, url: PluggedFutures(spec))


def call(client, function):
    client.portal.call(function)


def test_a_registered_adapter_serves_futures_with_the_data_state_it_declares(live, monkeypatch):
    plug(monkeypatch)
    with live.app() as client, live.connect(client) as ws:
        client.put("/api/connections/siomtrading_futures/key", json={"key": "token-value-1234"}, headers=AUTH)
        c = Collector(ws)
        snapshot = subscribe(c, "n", "NQ=F", "5m")
        assert (snapshot["source"], snapshot["fallback"], snapshot["data_state"]) == (
            "SiomTrading Futures", False, "delayed")
        source = PluggedFutures.instances[-1]
        assert wait_until(lambda: source.subscribed == ["NQ"])
        call(client, lambda: source.emit(Tick("NQ", int(RECORDED_AT * 1000) + 2000, 28300.5, 1)))
        bar = c.find(lambda m: m["type"] == "bar", timeout=5)
        assert bar["source"] == "SiomTrading Futures" and bar["data_state"] == "delayed" and bar["bar"]["c"] == 28300.5
        call(client, lambda: source.emit(SourceStatus("live", "welcome")))
        c.find(lambda m: m["type"] == "snapshot" and m["data_state"] == "live")          # the snapshot is relabelled
        call(client, lambda: source.emit(Tick("NQ", int(RECORDED_AT * 1000) + 2500, 28301.5, 1)))
        live_bar = c.find(lambda m: m["type"] == "bar" and m["data_state"] == "live", timeout=5)
        assert live_bar["bar"]["c"] == 28301.5


def test_f5_a_symbol_that_is_not_in_the_access_is_an_error_and_the_others_continue(live, monkeypatch):
    plug(monkeypatch, denied={"GC"})
    live.fetcher.overrides["GC=F"] = (200, fixture_text("nqf_5m.json"))
    with live.app() as client, live.connect(client) as ws:
        client.put("/api/connections/siomtrading_futures/key", json={"key": "token-value-1234"}, headers=AUTH)
        c = Collector(ws)
        assert subscribe(c, "n", "NQ=F", "5m")["type"] == "snapshot"
        subscribe(c, "g", "GC=F", "5m")
        refused = c.find(lambda m: m["type"] == "error" and m.get("sub") == "g")
        assert refused["code"] == "market.not_entitled" and refused["message"]
        source = PluggedFutures.instances[-1]
        call(client, lambda: source.emit(Tick("NQ", int(RECORDED_AT * 1000) + 2000, 28310.0, 1)))
        assert c.find(lambda m: m["type"] == "bar" and m["sub"] == "n")["bar"]["c"] == 28310.0


def test_f4_a_rollover_sends_a_new_snapshot(live, monkeypatch):
    plug(monkeypatch)
    with live.app() as client, live.connect(client) as ws:
        client.put("/api/connections/siomtrading_futures/key", json={"key": "token-value-1234"}, headers=AUTH)
        c = Collector(ws)
        first = subscribe(c, "n", "NQ=F", "5m")
        source = PluggedFutures.instances[-1]
        call(client, lambda: source.emit(Rollover("NQ", "NQZ6", "NQH7", int(RECORDED_AT * 1000))))
        again = c.find(lambda m: m["type"] == "snapshot" and m["seq"] > first["seq"])
        assert again["sub"] == "n" and again["bars"]


def test_f3_and_f2_a_drop_comes_back_and_a_refusal_moves_the_symbols_to_the_fallback(live, monkeypatch):
    plug(monkeypatch)
    with live.app() as client, live.connect(client) as ws:
        client.put("/api/connections/siomtrading_futures/key", json={"key": "token-value-1234"}, headers=AUTH)
        c = Collector(ws)
        first = subscribe(c, "n", "NQ=F", "5m")
        source = PluggedFutures.instances[-1]
        call(client, lambda: source.emit(SourceStatus("reconnecting", "network")))
        assert c.find(lambda m: m["type"] == "status" and m["state"] == "reconnecting")
        call(client, lambda: source.emit(SourceStatus("delayed", "connected")))
        assert c.find(lambda m: m["type"] == "snapshot" and m["seq"] > first["seq"])
        mark = len(c.all)
        call(client, lambda: source.emit(SourceStatus("down", "token_invalid")))
        card = c.find(lambda m: m["type"] == "connection_status" and m["provider"] == "siomtrading_futures"
                      and m["state"] == "error", after=mark)
        assert card["error_code"] == "connections.token_invalid"
        moved = c.find(lambda m: m["type"] == "snapshot" and m["fallback"], after=mark, timeout=6)
        assert moved["data_state"] == "delayed" and moved["asset_class"] == "future"
        listed = client.get("/api/connections", headers=AUTH).json()["connections"][3]
        assert listed["error_code"] == "connections.token_invalid" and listed["error_message"] == "Token not valid"

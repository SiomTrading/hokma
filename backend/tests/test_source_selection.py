"""SPEC-016 T17: one source of data per user (3.6) and no silent fallback (3.7). The brokers are fakes that emit a Quote; Alpaca is a fake
free source. No network."""
import asyncio

import pytest

from hokma.market.adapters import ADAPTERS, register_adapter
from hokma.market.hub import MarketHub, PanelClient
from hokma.market.providers import get_catalog
from hokma.market.stream import LiveSource, Quote, SourceCapabilities, SourceStatus
from test_hub_streaming import History, daily_history, five_history, message, run


class FakeProvider(LiveSource):
    def __init__(self, provider_id: str, name: str) -> None:
        super().__init__()
        self.provider_id, self.name = provider_id, name
        self.capabilities = SourceCapabilities(True, 100, ("equity",), 0, ticks=False)
        self.subscribed: list[str] = []

    async def connect(self) -> None:
        import asyncio as _a
        self._queue = self._queue or _a.Queue(1000)
        self.emit(SourceStatus("live", "connected"))

    async def subscribe(self, symbols):
        self.subscribed += list(symbols)

    async def unsubscribe(self, symbols):
        pass

    async def close(self) -> None:
        self._closing = True


class Config:
    loaded = True

    def __init__(self) -> None:
        self.enabled = {"schwab": True, "alpaca": True}
        self.keys = {"schwab", "alpaca"}
        self.signed = True
        self.chosen: str | None = None

    def order(self, use):
        return list(get_catalog().default_priority[use])

    def is_enabled(self, provider_id):
        return self.enabled.get(provider_id, False)

    def has_key(self, provider_id):
        return provider_id in self.keys

    def signed_in(self, provider_id):
        return self.signed

    def read_secret(self, provider_id):
        return "x"

    def data_source(self):
        return self.chosen


@pytest.fixture
def providers():
    made: dict = {}
    saved = {pid: ADAPTERS[pid] for pid in ("schwab", "alpaca")}
    for pid, name in (("schwab", "Schwab"), ("alpaca", "Alpaca Trading")):
        register_adapter(pid, lambda spec, secret, url, pid=pid, name=name: made.setdefault(pid, FakeProvider(pid, name)))
    yield made
    for pid, factory in saved.items():
        register_adapter(pid, factory)


def hub_of(config: Config) -> MarketHub:
    return MarketHub(get_catalog(), config, lambda: History(daily_history(), five_history()))


async def chart(hub: MarketHub, client: PanelClient, sub: str, symbol: str = "QQQ") -> dict:
    await hub.subscribe(client, sub, symbol, "5m")
    return await message(client, lambda m: m.get("sub") == sub and m["type"] in ("snapshot", "error"))


def test_with_a_broker_connected_the_data_is_only_the_broker_and_alpaca_gets_nothing(providers):
    async def go():
        hub = hub_of(Config())
        client = PanelClient()
        hub.attach_client(client)
        snap = await chart(hub, client, "a")
        assert snap["source"] == "Schwab" and "alpaca" not in providers
        assert hub.preview_route("QQQ", "equity")["provider"] == "schwab"
        await hub.close()
    run(go())


def test_without_a_broker_alpaca_is_the_source_labelled_alpaca_trading(providers):
    config = Config()
    config.keys = {"alpaca"}
    config.signed = False

    async def go():
        hub = hub_of(config)
        client = PanelClient()
        hub.attach_client(client)
        snap = await chart(hub, client, "a")
        assert snap["source"] == "Alpaca Trading" and "schwab" not in providers
        await hub.close()
    run(go())


def test_the_roles_come_from_the_providers_file_and_no_name_is_in_the_route():
    catalog = get_catalog()
    assert {pid: spec.role for pid, spec in catalog.providers.items()} == {
        "coinbase": None, "alpaca": "free", "finnhub": None, "siomtrading_futures": "free", "schwab": "broker", "ibkr": "broker"}
    assert catalog.providers["alpaca"].name == "Alpaca Trading"


def test_when_the_broker_goes_down_the_symbols_stay_stopped_with_its_name_and_nothing_else_answers(providers):
    async def go():
        hub = hub_of(Config())
        client = PanelClient()
        hub.attach_client(client)
        await chart(hub, client, "a")
        providers["schwab"].emit(SourceStatus("down", "connections.schwab_expired"))
        status = await message(client, lambda m: m["type"] == "status" and m["state"] == "down")
        assert status["source"] == "Schwab" and status["code"] == "connections.schwab_expired"
        assert "alpaca" not in providers                                # no fallback, no other source
        late = PanelClient()
        hub.attach_client(late)
        refused = await chart(hub, late, "b", "SPY")
        assert refused["type"] == "error" and refused["code"] == "market.down"
        await hub.close()
    run(go())


def test_connecting_a_broker_moves_open_charts_from_alpaca_to_the_broker_and_removing_it_moves_them_back(providers):
    config = Config()
    config.keys = {"alpaca"}
    config.signed = False

    async def go():
        hub = hub_of(config)
        client = PanelClient()
        hub.attach_client(client)
        first = await chart(hub, client, "a")
        assert first["source"] == "Alpaca Trading"
        config.keys, config.signed = {"alpaca", "schwab"}, True                    # the user connects Schwab
        await hub.refresh("schwab")
        moved = await message(client, lambda m: m["type"] == "snapshot" and m["source"] == "Schwab")
        assert moved["sub"] == "a"
        config.keys, config.signed = {"alpaca"}, False                              # and removes it
        await hub.refresh("schwab")
        back = await message(client, lambda m: m["type"] == "snapshot" and m["source"] == "Alpaca Trading")
        assert back["sub"] == "a"
        await hub.close()
    run(go())


def test_the_chosen_broker_is_the_only_one_when_two_are_connected(providers):
    config = Config()
    hub = hub_of(config)
    assert hub._active_broker("equity") == "schwab"
    config.chosen = "schwab"
    assert hub._active_broker("equity") == "schwab"
    assert hub._eligible("alpaca") is False

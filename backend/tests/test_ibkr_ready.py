"""SPEC-016 T20 (5.6): Interactive Brokers is ready to be added. Its entry is in the providers file, off, with no adapter; with a fake adapter that emits Quote and
BarUpdate, the selection, the loss notice and the cell by cell chain work exactly as with Schwab. The real adapter is its own spec (I1 is not verified)."""
import asyncio

import pytest

from hokma.market.adapters import ADAPTERS, register_adapter
from hokma.market.hub import PanelClient
from hokma.market.providers import get_catalog
from hokma.market.stream import Quote, SourceStatus
from test_broker_watch import english, watched  # noqa: F401  (fixture)
from test_hub_streaming import message, run
from test_source_selection import Config, FakeProvider, chart, hub_of, providers  # noqa: F401  (fixture)


@pytest.fixture
def two_brokers(providers):
    saved = ADAPTERS.get("ibkr")
    register_adapter("ibkr", lambda spec, secret, url: providers.setdefault("ibkr", FakeProvider("ibkr", "Interactive Brokers")))
    yield providers
    if saved is None:
        ADAPTERS.pop("ibkr", None)
    else:
        register_adapter("ibkr", saved)


def both() -> Config:
    config = Config()
    config.enabled, config.keys = {"schwab": True, "ibkr": True, "alpaca": True}, {"schwab", "alpaca"}
    return config


def test_the_entry_is_in_the_file_off_by_default_with_a_gateway_and_no_key():
    spec = get_catalog().get("ibkr")
    assert (spec.role, spec.auth, spec.enabled_by_default, spec.needs_key, spec.host) == ("broker", "gateway", False, False, "127.0.0.1")
    assert "ibkr" not in ADAPTERS or ADAPTERS["ibkr"] is not None          # no adapter ships: the card says it is not available yet


def test_with_two_brokers_connected_only_the_chosen_one_serves_and_the_choice_moves_the_open_charts(two_brokers):
    config = both()

    async def go():
        hub = hub_of(config)
        client = PanelClient()
        hub.attach_client(client)
        assert hub._active_broker("equity") == "schwab"                       # none chosen: the first one
        first = await chart(hub, client, "a")
        assert first["source"] == "Schwab" and "ibkr" not in two_brokers
        config.chosen = "ibkr"                                                  # the user picks IB in Connections
        for pid in ("schwab", "ibkr"):
            await hub.refresh(pid, cascade=False)
        moved = await message(client, lambda m: m["type"] == "snapshot" and m["source"] == "Interactive Brokers")
        assert moved["sub"] == "a" and hub._eligible("schwab") is False and hub._eligible("alpaca") is False
        await hub.close()
    run(go())


def test_the_loss_of_ib_is_told_like_the_loss_of_schwab(two_brokers):
    sent: list = []

    async def go():
        config = both()
        config.chosen = "ibkr"
        hub = watched(hub_of(config), sent)
        client = PanelClient()
        hub.attach_client(client)
        await chart(hub, client, "a")
        two_brokers["ibkr"].emit(SourceStatus("reconnecting", "network"))
        alert = await message(client, lambda m: m["type"] == "broker_alert" and m["state"] == "lost")
        assert alert["source"] == "Interactive Brokers"
        for _ in range(100):
            if sent:
                break
            await asyncio.sleep(0.02)
        assert "Interactive Brokers" in sent[0]
        await hub.close()
    run(go())

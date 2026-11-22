"""SPEC-016 T9 (5.5, CA22): Yahoo stays what it was in 1.0.9 for a user without a broker, and with a broker the hub never sends it a symbol
the broker covers. Yahoo is never presented as live data."""
import asyncio

import pytest

from hokma.market.adapters import ADAPTERS, register_adapter
from hokma.market.adapters.polling import PollingLiveSource
from hokma.market.hub import MarketHub, PanelClient
from hokma.market.providers import get_catalog
from test_hub_streaming import History, daily_history, five_history, message, run
from test_source_selection import Config, FakeProvider, chart, hub_of, providers  # noqa: F401  (fixture)


def test_without_a_broker_or_a_free_key_the_symbol_is_the_delayed_yahoo_fallback_as_in_109(providers):
    config = Config()
    config.keys, config.signed, config.enabled = set(), False, {}

    async def go():
        hub = hub_of(config)
        client = PanelClient()
        hub.attach_client(client)
        snap = await chart(hub, client, "a")
        assert snap["type"] == "snapshot" and snap["fallback"] is True and snap["data_state"] == "delayed"
        assert snap["source"] == get_catalog().polling.name == "Yahoo Finance"
        assert isinstance(hub._fallback.source, PollingLiveSource) and hub._fallback.source.data_state == "delayed"
        assert providers == {}                                       # nothing else was opened for it
        await hub.close()
    run(go())


def test_with_a_broker_connected_yahoo_gets_no_symbol_of_the_broker(providers):
    async def go():
        hub = hub_of(Config())
        client = PanelClient()
        hub.attach_client(client)
        snap = await chart(hub, client, "a")
        assert snap["source"] == "Schwab" and snap["fallback"] is False
        assert hub._fallback.feeds == {} and hub._fallback.source is None
        await hub.close()
    run(go())


def test_the_yahoo_modules_keep_their_1_0_9_contract():
    """The polling interval and the delay table of the packaged file are the ones of 1.0.9: nobody retuned the delayed fallback."""
    polling = get_catalog().polling
    assert polling.interval_seconds == 20 and polling.delay_seconds == {"future": 600, "equity": 900, "forex": 0, "crypto": 0, "index": 900}
    assert PollingLiveSource.provider_id == "yahoo_polling" and PollingLiveSource.data_state == "delayed"

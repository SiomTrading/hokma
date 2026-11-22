"""SPEC-016 3.10: futures come from Schwab when it is connected and from SiomTrading Futures when it is not, never both and with no silent move."""
from hokma.market.hub import PanelClient
from test_hub_streaming import message, run
from test_source_selection import Config, FakeProvider, hub_of, providers  # noqa: F401  (fixture)
from hokma.market.adapters import ADAPTERS, register_adapter


def test_with_schwab_connected_the_futures_route_to_schwab_and_siomtrading_futures_is_off(providers):
    hub = hub_of(Config())
    assert hub._active_broker("future") == "schwab"
    assert hub._eligible("siomtrading_futures") is False
    candidates = hub._candidates("NQ=F", "future", frozenset())
    assert [(runtime.id, upstream) for runtime, upstream in candidates] == [("schwab", "/NQ")]


def test_without_schwab_the_futures_route_to_siomtrading_futures():
    config = Config()
    config.keys, config.signed, config.enabled = {"siomtrading_futures"}, False, {"siomtrading_futures": True}
    saved = ADAPTERS["siomtrading_futures"]
    register_adapter("siomtrading_futures", lambda spec, secret, url: FakeProvider("siomtrading_futures", "SiomTrading Futures"))
    try:
        hub = hub_of(config)
        assert hub._active_broker("future") is None and hub._eligible("siomtrading_futures") is True
        assert [(r.id, u) for r, u in hub._candidates("NQ=F", "future", frozenset())] == [("siomtrading_futures", "NQ")]
    finally:
        register_adapter("siomtrading_futures", saved)

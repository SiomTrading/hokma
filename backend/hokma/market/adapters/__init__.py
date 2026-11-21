"""The adapters and the registry that builds one from a provider spec and the owner's saved key.

`register_adapter(provider_id, factory)` is how a provider plugs in (the SiomTrading futures adapter is listed in
`ADAPTERS` below; tests plug fakes in the same way). A factory is `factory(spec, secret, url)` returning a source,
where `secret` is the saved key as a string (None when the provider needs none) and `url` is an override for tests.
"""
import json
from collections.abc import Callable

from hokma.market.adapters.alpaca import AlpacaSource
from hokma.market.adapters.coinbase import CoinbaseSource
from hokma.market.adapters.finnhub import FinnhubSource
from hokma.market.adapters.polling import PollingLiveSource
from hokma.market.adapters.siomtrading_futures import SiomTradingFuturesSource
from hokma.market.providers import ProviderSpec
from hokma.market.stream import LiveSource

AdapterFactory = Callable[[ProviderSpec, str | None, str | None], LiveSource]

ADAPTERS: dict[str, AdapterFactory] = {
    "coinbase": lambda spec, secret, url: CoinbaseSource(spec, url),
    "alpaca": lambda spec, secret, url: AlpacaSource(spec, json.loads(secret or "{}"), url),
    "finnhub": lambda spec, secret, url: FinnhubSource(spec, secret or "", url),
    "siomtrading_futures": lambda spec, secret, url: SiomTradingFuturesSource(spec, secret or "", url),
}


def register_adapter(provider_id: str, factory: AdapterFactory) -> None:
    ADAPTERS[provider_id] = factory


__all__ = ["ADAPTERS", "AdapterFactory", "AlpacaSource", "CoinbaseSource", "FinnhubSource", "PollingLiveSource",
           "SiomTradingFuturesSource", "register_adapter"]

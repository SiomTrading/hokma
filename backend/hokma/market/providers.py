"""The catalog of data providers: who they are, which asset classes they cover, whether they need a key, their
limits and the `data_state` they declare. It lives in `providers.json` next to this file so a value (for example
whether the free Finnhub plan is live) changes without code. The same file carries the host that the package fixes
for the SiomTrading futures stream.

`set_catalog` swaps the catalog (tests point the hosts at local fake servers); nothing reads the environment.
"""
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

CATALOG_FILE = Path(__file__).with_name("providers.json")
ASSET_CLASSES = ("equity", "crypto", "forex", "future")
DATA_STATES = ("live", "delayed")


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    name: str
    asset_classes: tuple[str, ...]
    needs_key: bool
    key_fields: tuple[str, ...]
    enabled_by_default: bool
    data_state: str
    delay_seconds: int | None
    max_symbols: int
    premium: bool
    test_symbol: str
    url: str | None = None
    host: str | None = None
    port: int | None = None


@dataclass(frozen=True)
class PollingSpec:
    name: str
    interval_seconds: float
    delay_seconds: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderCatalog:
    providers: dict[str, ProviderSpec]
    default_priority: dict[str, tuple[str, ...]]
    polling: PollingSpec

    def get(self, provider_id: str) -> ProviderSpec | None:
        return self.providers.get(provider_id)


def parse_catalog(raw: dict) -> ProviderCatalog:
    providers: dict[str, ProviderSpec] = {}
    for provider_id, item in raw["providers"].items():
        state = item["data_state"]
        if state not in DATA_STATES:
            raise ValueError(f"{provider_id}: data_state must be one of {DATA_STATES}")
        classes = tuple(item["asset_classes"])
        if not classes or any(c not in ASSET_CLASSES for c in classes):
            raise ValueError(f"{provider_id}: unknown asset class")
        providers[provider_id] = ProviderSpec(
            id=provider_id, name=item["name"], asset_classes=classes, needs_key=bool(item["needs_key"]),
            key_fields=tuple(item.get("key_fields", ())), enabled_by_default=bool(item["enabled_by_default"]),
            data_state=state, delay_seconds=item.get("delay_seconds"), max_symbols=int(item["max_symbols"]),
            premium=bool(item.get("premium", False)), test_symbol=item["test_symbol"], url=item.get("url"),
            host=item.get("host"), port=item.get("port"),
        )
    priority: dict[str, tuple[str, ...]] = {}
    for asset_class in ASSET_CLASSES:
        order = [p for p in raw["default_priority"].get(asset_class, []) if p in providers]
        # A provider that covers the class but is missing from the default order goes last.
        order += [p.id for p in providers.values() if asset_class in p.asset_classes and p.id not in order]
        priority[asset_class] = tuple(order)
    poll = raw["polling"]
    return ProviderCatalog(providers, priority, PollingSpec(
        poll["name"], float(poll["interval_seconds"]), {k: int(v) for k, v in poll["delay_seconds"].items()}))


_lock = threading.Lock()
_catalog: ProviderCatalog | None = None


def get_catalog() -> ProviderCatalog:
    global _catalog
    with _lock:
        if _catalog is None:
            _catalog = parse_catalog(json.loads(CATALOG_FILE.read_text(encoding="utf-8")))
        return _catalog


def set_catalog(catalog: ProviderCatalog | None) -> None:
    global _catalog
    with _lock:
        _catalog = catalog

"""What the owner decides in Connections: the key of each provider, which ones are on and in which order.

Keys go to the system credential store and nowhere else (`store.py`). This object never returns a key and never
logs one; it only remembers, in memory, whether each provider has one saved."""
import json
import re

from hokma.connections.store import ConnectionSettings, KeyStore, StoreUnavailable
from hokma.market.providers import ASSET_CLASSES, ProviderCatalog, ProviderSpec

KEY_VALUE = re.compile(r"[\x21-\x7e]{4,256}")           # printable ASCII, no spaces, no control characters


class ServiceError(Exception):
    """`code` is a `connections.*` catalog code; `status` the HTTP status that goes with it."""

    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code, self.status = code, status


class ConnectionService:
    def __init__(self, catalog: ProviderCatalog, store: KeyStore | None = None,
                 settings: ConnectionSettings | None = None) -> None:
        self.catalog = catalog
        self.store = store or KeyStore()
        self.settings = settings or ConnectionSettings()
        self._saved: dict[str, bool] = {}
        self.loaded = False

    # ---- what the hub asks (ConnectionConfig) ----

    def is_enabled(self, provider_id: str) -> bool:
        spec = self.catalog.get(provider_id)
        return self.settings.enabled(provider_id, spec.enabled_by_default if spec else False)

    def has_key(self, provider_id: str) -> bool:
        return self._saved.get(provider_id, False)

    def order(self, asset_class: str) -> list[str]:
        return self.settings.order(asset_class, self.catalog.default_priority.get(asset_class, ()))

    def read_secret(self, provider_id: str) -> str | None:
        """Blocking: asks the system store. Called from a thread, only when a connection is being opened."""
        return self.store.get(provider_id)

    # ---- state ----

    def load(self) -> None:
        """Blocking: learn once which providers have a saved key. Never reads or keeps the values."""
        self._saved = {pid: self.store.has(pid) for pid, spec in self.catalog.providers.items() if spec.needs_key}
        self.loaded = True

    def store_available(self) -> bool:
        return self.store.available()

    # ---- changes (blocking: run them in a thread) ----

    def spec(self, provider_id: str) -> ProviderSpec:
        spec = self.catalog.get(provider_id)
        if spec is None:
            raise ServiceError("connections.unknown_provider", 404)
        return spec

    def put_key(self, provider_id: str, fields: dict) -> None:
        spec = self.spec(provider_id)
        if not spec.needs_key:
            raise ServiceError("connections.bad_key", 400)
        values = {}
        for name in spec.key_fields:
            value = fields.get(name)
            if not isinstance(value, str) or not KEY_VALUE.fullmatch(value.strip()):
                raise ServiceError("connections.bad_key", 422)
            values[name] = value.strip()
        stored = values["key"] if spec.key_fields == ("key",) else json.dumps(values, separators=(",", ":"))
        try:
            self.store.set(provider_id, stored)
        except StoreUnavailable:
            raise ServiceError("connections.store_unavailable", 503) from None
        self._saved[provider_id] = True

    def delete_key(self, provider_id: str) -> None:
        spec = self.spec(provider_id)
        if not spec.needs_key:
            raise ServiceError("connections.bad_key", 400)
        try:
            self.store.delete(provider_id)
        except StoreUnavailable:
            raise ServiceError("connections.store_unavailable", 503) from None
        self._saved[provider_id] = False

    def set_enabled(self, provider_id: str, value: bool) -> None:
        self.spec(provider_id)
        self.settings.set_enabled(provider_id, value)

    def set_priority(self, asset_class: str, order: list[str]) -> None:
        if asset_class not in ASSET_CLASSES:
            raise ServiceError("connections.bad_priority", 422)
        expected = set(self.catalog.default_priority[asset_class])
        if len(order) != len(set(order)) or set(order) != expected:
            raise ServiceError("connections.bad_priority", 422)
        self.settings.set_order(asset_class, order)

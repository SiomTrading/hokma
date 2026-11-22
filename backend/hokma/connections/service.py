"""What the owner decides in Connections: the key of each provider, which ones are on and in which order.

Keys go to the system credential store and nowhere else (`store.py`). This object never returns a key and never
logs one; it only remembers, in memory, whether each provider has one saved."""
import json
import re

import logging
from collections.abc import Callable

from hokma.connections.store import RETIRED_PROVIDERS, SETTINGS_VERSION, ConnectionSettings, KeyStore, StoreUnavailable
from hokma.market.providers import USES, ProviderCatalog, ProviderSpec

log = logging.getLogger("hokma.connections")
KEY_VALUE = re.compile(r"[\x21-\x7e]{4,256}")           # printable ASCII, no spaces, no control characters
SESSION_SUFFIX = ".session"                              # the OAuth session of a provider: `hokma-market/<id>.session`


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

    def data_source(self) -> str | None:
        """The broker the user chose as the source of live data, or None (the hub then takes the first one connected)."""
        return self.settings.data_source()

    def set_data_source(self, provider_id: str) -> None:
        spec = self.spec(provider_id)
        if spec.role != "broker" or not self.is_added(provider_id):
            raise ServiceError("connections.bad_data_source", 422)
        self.settings.set_data_source(provider_id)

    def read_secret(self, provider_id: str) -> str | None:
        """Blocking: asks the system store. Called from a thread, only when a connection is being opened."""
        return self.store.get(provider_id)

    # ---- state ----

    def load(self) -> None:
        """Blocking: learn once which providers have a saved key. Never reads or keeps the values."""
        self._retire_providers()
        self._saved = {pid: self.store.has(pid) for pid, spec in self.catalog.providers.items() if spec.needs_key}
        self.loaded = True

    def _retire_providers(self) -> None:
        """Once, for a settings file older than version 2 (or none): remove the saved key of every provider Hokma no
        longer offers and its traces in the file. A store that is not available does not stop it: the file is saved as
        version 2 anyway, and the key that stays is never read by any code."""
        if self.settings.version >= SETTINGS_VERSION or self.settings.unreadable:
            return                                     # an unreadable file is not migrated: it is tried again next time
        removed: list[str] = []
        for provider_id in RETIRED_PROVIDERS:
            try:
                if self.store.has(provider_id):
                    removed.append(provider_id)
                self.store.delete(provider_id)
            except StoreUnavailable:
                pass
        try:
            removed += [p for p in self.settings.retire(RETIRED_PROVIDERS) if p not in removed]
        except OSError:
            return                                     # the folder cannot be written now: it is tried again next time
        for provider_id in removed:
            log.info("retired provider removed: %s", provider_id)

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

    def set_priority(self, use: str, order: list[str]) -> None:
        """`use` is any key of `USES` (4.4): an asset class, `bars`, `options` or `news`. The order must list every
        provider of that use exactly once."""
        if use not in USES:
            raise ServiceError("connections.bad_priority", 422)
        expected = set(self.catalog.default_priority.get(use, ()))
        if len(order) != len(set(order)) or set(order) != expected:
            raise ServiceError("connections.bad_priority", 422)
        self.settings.set_order(use, order)

    # ---- what "Add" and "Remove" do (SPEC-015 4.3 rules 3 and 4) ----

    def is_added(self, provider_id: str) -> bool:
        """"Connected" in the sense of the Connections screen (4.3 rule 1): a provider with its key saved, Schwab with a
        sign-in, a provider without key that is switched on. A paused one (key saved, switched off) still counts."""
        spec = self.catalog.get(provider_id)
        if spec is None:
            return False
        if spec.auth == "oauth":
            return self.signed_in(provider_id)
        if spec.needs_key:
            return self.has_key(provider_id)
        return self.is_enabled(provider_id)

    def signed_in(self, provider_id: str) -> bool:
        """An OAuth provider with a live session (cheap, in memory). Only Schwab has one."""
        if provider_id != "schwab":
            return False
        try:
            from hokma.schwab import auth

            return bool(auth.is_connected())
        except Exception:                                  # noqa: BLE001  (no auth module yet, or it cannot say: not signed in)
            return False

    def add(self, provider_id: str, fields: dict, test: Callable[[], dict]) -> dict:
        """Save the key (when the provider has one), switch it on and run `test()` (blocking, returns the test result
        of the hub). A failed test undoes the change: the new key is dropped and the previous one, if any, is put
        back; the switch returns to what it was. Returns `{"added": bool, "test": dict}`.

        Schwab (OAuth) is the one exception: its Client ID and Secret are kept when the test fails because there is no
        sign-in yet (the sign-in is the next step); with a live session the same undo rule applies."""
        spec = self.spec(provider_id)
        keeps_credentials = spec.auth == "oauth" and not self.signed_in(provider_id)
        previous_key = self.store.get(provider_id) if spec.needs_key else None
        previous_enabled = self.settings.explicit_enabled(provider_id)
        if spec.needs_key and (fields or not self.has_key(provider_id)):
            self.put_key(provider_id, fields)              # refuses a malformed key before anything changes
        self.settings.set_enabled(provider_id, True)
        try:
            result = test()
        except BaseException:
            self._undo_add(spec, previous_key, previous_enabled, keeps_credentials)
            raise
        if result.get("ok"):
            self.mark_added(provider_id, first_time=previous_enabled is not True)
            return {"added": True, "test": result}
        self._undo_add(spec, previous_key, previous_enabled, keeps_credentials)
        return {"added": False, "test": result}

    def _undo_add(self, spec: ProviderSpec, previous_key: str | None, previous_enabled: bool | None,
                  keep_new_key: bool) -> None:
        if spec.needs_key and not keep_new_key:
            try:
                if previous_key is not None:
                    self.store.set(spec.id, previous_key)
                    self._saved[spec.id] = True
                else:
                    self.store.delete(spec.id)
                    self._saved[spec.id] = False
            except StoreUnavailable:
                log.error("the key of %s could not be put back after a failed test", spec.id)
        self.settings.restore_enabled(spec.id, previous_enabled)

    def mark_added(self, provider_id: str, first_time: bool | None = None) -> list[int]:
        """The provider is connected for good: switch it on and give it to the desks (rule 5). Schwab, the first time,
        goes to the first place of every use it serves (4.4). Idempotent: a second call changes nothing. Returns the
        desks that got a row. Schwab's `auth.finish_login` calls it after a good test."""
        spec = self.spec(provider_id)
        if first_time is None:
            first_time = self.settings.explicit_enabled(provider_id) is not True
        if spec.auth == "oauth" and first_time:
            self.settings.move_first(tuple(self.catalog.uses_of(provider_id)), provider_id, self.catalog.default_priority)
        if first_time:
            self.settings.set_enabled(provider_id, True)
        if spec.role == "broker" and first_time:
            current = self.settings.data_source()
            if current is None or not self.is_added(current):
                self.settings.set_data_source(provider_id)      # the first broker that connects is the source, until the user picks another
        try:
            from hokma.desks import service as desks

            given = desks.enable_connection_everywhere(provider_id)
        except Exception as exc:                           # noqa: BLE001  (the desks are not the connection's business)
            log.warning("desks were not given %s: %s", provider_id, type(exc).__name__)
            given = []
        if provider_id == "schwab":
            self._open_schwab_accounts()
        return given

    def _open_schwab_accounts(self) -> None:
        """SPEC-015 18.2 point 2: the Schwab broker and one account per Schwab account (idempotent). No credential store
        for the fingerprint key is `connections.store_unavailable`; any other failure (Schwab or the database not
        answering now) only leaves a line in the log: the next `mark_added` does it again."""
        from hokma.broker import schwab as schwab_broker

        try:
            schwab_broker.sync_accounts()
        except StoreUnavailable:
            raise ServiceError("connections.store_unavailable", 503) from None
        except Exception as exc:                           # noqa: BLE001  (never a number or a hash in the log: only the type)
            log.warning("schwab accounts were not opened as broker accounts: %s", type(exc).__name__)

    def _archive_schwab_accounts(self) -> None:
        """SPEC-015 18.2 point 2: removing Schwab archives its accounts (nothing is deleted) and turns the orders off."""
        from hokma.broker import schwab as schwab_broker

        try:
            schwab_broker.archive_accounts()
        except Exception as exc:                           # noqa: BLE001
            log.warning("schwab broker accounts were not archived: %s", type(exc).__name__)

    def remove(self, provider_id: str) -> None:
        """Forget the provider: a key provider loses its key and is switched off; Coinbase is switched off; Schwab loses
        its Client ID, Secret and session."""
        spec = self.spec(provider_id)
        if spec.auth == "oauth":
            try:
                from hokma.schwab import auth

                auth.sign_out()
            except Exception as exc:                       # noqa: BLE001  (the entries are deleted below anyway)
                log.info("sign out of %s was not clean: %s", provider_id, type(exc).__name__)
            try:
                self.store.delete(provider_id + SESSION_SUFFIX)
                self.store.delete(provider_id)
            except StoreUnavailable:
                raise ServiceError("connections.store_unavailable", 503) from None
            self._saved[provider_id] = False
            if provider_id == "schwab":
                self._archive_schwab_accounts()
        elif spec.needs_key:
            self.delete_key(provider_id)
        self.settings.set_enabled(provider_id, False)

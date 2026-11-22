"""Where the data connections keep their state.

Keys live ONLY in the system credential store (Windows Credential Manager, macOS Keychain, Secret Service on Linux)
through `keyring`, service `hokma-market`, one entry per provider. When there is no usable store nothing is saved:
there is no file fallback and no environment variable, ever.

`<data>/connections.json` holds only what is not secret: which providers are switched on and the priority order.
"""
import json
import logging
import os
import tempfile
from pathlib import Path

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from hokma.core.config import hokma_data_dir

SERVICE = "hokma-market"
STATE_FILE = "connections.json"
SETTINGS_VERSION = 2
# Providers that Hokma no longer offers (1.0.8). Their saved key and their traces in `connections.json` are removed once.
RETIRED_PROVIDERS = ("databento", "massive")

log = logging.getLogger("hokma.connections")


class StoreUnavailable(Exception):
    """There is no system credential store, or it refused the operation."""


def _usable(backend) -> bool:
    module = type(backend).__module__
    if module.startswith(("keyring.backends.fail", "keyring.backends.null", "keyrings.alt")):
        return False                                  # no store, a store that discards, or a plain-text file store
    children = getattr(backend, "backends", None)     # the chain keyring builds from the viable backends
    if children is not None:
        return any(_usable(child) for child in children)
    return getattr(backend, "priority", 0) > 0


class KeyStore:
    """The saved key of each provider. A value is a string; a provider with several parts (Alpaca) stores JSON."""

    def available(self) -> bool:
        try:
            return _usable(keyring.get_keyring())
        except Exception:
            return False

    def _require(self) -> None:
        if not self.available():
            raise StoreUnavailable()

    def get(self, provider_id: str) -> str | None:
        if not self.available():
            return None
        try:
            return keyring.get_password(SERVICE, provider_id)
        except KeyringError:
            return None

    def has(self, provider_id: str) -> bool:
        return bool(self.get(provider_id))

    def set(self, provider_id: str, value: str) -> None:
        self._require()
        try:
            keyring.set_password(SERVICE, provider_id, value)
        except KeyringError:
            raise StoreUnavailable() from None

    def delete(self, provider_id: str) -> None:
        if not self.available():
            return
        try:
            keyring.delete_password(SERVICE, provider_id)
        except PasswordDeleteError:
            pass
        except KeyringError:
            raise StoreUnavailable() from None


class ConnectionSettings:
    """`<data>/connections.json`: `{"version": 2, "enabled": {id: bool}, "priority": {asset_class: [id]}}`."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._enabled: dict[str, bool] = {}
        self._priority: dict[str, list[str]] = {}
        self._data_source: str | None = None            # the broker the user chose as the source of live data (SPEC-016 3.6)
        self.version = 0                                 # of the file read; 0 when there is none
        self.unreadable = False                          # the file exists but could not be read or parsed
        self.load()

    @property
    def path(self) -> Path:
        return self._path or hokma_data_dir() / STATE_FILE

    def load(self) -> None:
        self.unreadable = False
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError) as exc:
            log.error("connections.json could not be read (%s); it is left as it is", type(exc).__name__)
            self.unreadable = True
            raw = {}
        version = raw.get("version") if isinstance(raw, dict) else None
        self.version = version if isinstance(version, int) and not isinstance(version, bool) else 0
        raw = raw if isinstance(raw, dict) else {}
        enabled, priority = raw.get("enabled"), raw.get("priority")
        self._enabled = {k: v for k, v in enabled.items() if isinstance(v, bool)} if isinstance(enabled, dict) else {}
        self._priority = ({k: [x for x in v if isinstance(x, str)] for k, v in priority.items()
                           if isinstance(v, list)} if isinstance(priority, dict) else {})
        source = raw.get("data_source")
        self._data_source = source if isinstance(source, str) else None
        # 1.0.9: the candles of stocks and ETFs follow their own order (`bars`). A file written by 1.0.8 has none, so it
        # gets the saved `equity` order once, in memory, so the candles keep coming from the same source. The file is
        # not rewritten here: the copy is saved the next time anything is saved.
        if "bars" not in self._priority and "equity" in self._priority:
            self._priority["bars"] = list(self._priority["equity"])

    def enabled(self, provider_id: str, default: bool) -> bool:
        return self._enabled.get(provider_id, default)

    def explicit_enabled(self, provider_id: str) -> bool | None:
        """The switch the owner saved for a provider, or None when the file says nothing about it."""
        return self._enabled.get(provider_id)

    def order(self, asset_class: str, default: tuple[str, ...]) -> list[str]:
        """The saved order, completed with any provider the saved list does not mention (a new provider goes last)."""
        saved = [p for p in self._priority.get(asset_class, []) if p in default]
        return saved + [p for p in default if p not in saved]

    def retire(self, provider_ids: tuple[str, ...]) -> list[str]:
        """Forget retired providers (switch and priority) and save the file as the current version. Returns the ids
        that had any trace in the file."""
        if self.unreadable:                              # a file we could not read is never rewritten from nothing
            log.error("connections.json is unreadable: retired providers were not cleaned from it")
            return []
        found = [p for p in provider_ids if p in self._enabled or any(p in v for v in self._priority.values())]
        for provider_id in provider_ids:
            self._enabled.pop(provider_id, None)
        self._priority = {k: [x for x in v if x not in provider_ids] for k, v in self._priority.items()}
        self._save()
        return found

    def set_enabled(self, provider_id: str, value: bool) -> None:
        self._enabled[provider_id] = value
        self._save()

    def restore_enabled(self, provider_id: str, value: bool | None) -> None:
        """Put the switch back as it was (None: the file did not mention the provider)."""
        if value is None:
            self._enabled.pop(provider_id, None)
            self._save()
        else:
            self.set_enabled(provider_id, value)

    def data_source(self) -> str | None:
        return self._data_source

    def set_data_source(self, provider_id: str | None) -> None:
        self._data_source = provider_id
        self._save()

    def set_order(self, asset_class: str, order: list[str]) -> None:
        self._priority[asset_class] = list(order)
        self._save()

    def move_first(self, uses: tuple[str, ...], provider_id: str, defaults: dict[str, tuple[str, ...]]) -> None:
        """Put `provider_id` at the head of each use that lists it, in one save."""
        for use in uses:
            default = defaults.get(use, ())
            if provider_id in default:
                current = self.order(use, default)
                self._priority[use] = [provider_id] + [p for p in current if p != provider_id]
        self._save()

    def _save(self) -> None:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        saved = {"version": SETTINGS_VERSION, "enabled": self._enabled, "priority": self._priority}
        if self._data_source is not None:
            saved["data_source"] = self._data_source              # only when the user has a chosen broker: older files stay as they were
        body = json.dumps(saved, indent=2)
        fd, temp = tempfile.mkstemp(prefix=".connections-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(body)
            os.replace(temp, path)
            self.version = SETTINGS_VERSION
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise

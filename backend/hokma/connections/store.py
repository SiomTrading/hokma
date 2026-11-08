"""Where the data connections keep their state.

Keys live ONLY in the system credential store (Windows Credential Manager, macOS Keychain, Secret Service on Linux)
through `keyring`, service `hokma-market`, one entry per provider. When there is no usable store nothing is saved:
there is no file fallback and no environment variable, ever.

`<data>/connections.json` holds only what is not secret: which providers are switched on and the priority order.
"""
import json
import os
import tempfile
from pathlib import Path

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from hokma.core.config import hokma_data_dir

SERVICE = "hokma-market"
STATE_FILE = "connections.json"


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
    """`<data>/connections.json`: `{"version": 1, "enabled": {id: bool}, "priority": {asset_class: [id]}}`."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._enabled: dict[str, bool] = {}
        self._priority: dict[str, list[str]] = {}
        self.load()

    @property
    def path(self) -> Path:
        return self._path or hokma_data_dir() / STATE_FILE

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        enabled, priority = raw.get("enabled"), raw.get("priority")
        self._enabled = {k: v for k, v in enabled.items() if isinstance(v, bool)} if isinstance(enabled, dict) else {}
        self._priority = ({k: [x for x in v if isinstance(x, str)] for k, v in priority.items()
                           if isinstance(v, list)} if isinstance(priority, dict) else {})

    def enabled(self, provider_id: str, default: bool) -> bool:
        return self._enabled.get(provider_id, default)

    def order(self, asset_class: str, default: tuple[str, ...]) -> list[str]:
        """The saved order, completed with any provider the saved list does not mention (a new provider goes last)."""
        saved = [p for p in self._priority.get(asset_class, []) if p in default]
        return saved + [p for p in default if p not in saved]

    def set_enabled(self, provider_id: str, value: bool) -> None:
        self._enabled[provider_id] = value
        self._save()

    def set_order(self, asset_class: str, order: list[str]) -> None:
        self._priority[asset_class] = list(order)
        self._save()

    def _save(self) -> None:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"version": 1, "enabled": self._enabled, "priority": self._priority}, indent=2)
        fd, temp = tempfile.mkstemp(prefix=".connections-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(body)
            os.replace(temp, path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise

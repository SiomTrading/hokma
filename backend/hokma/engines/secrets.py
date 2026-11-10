"""Engine secrets (API keys, tokens): only in the system credential store, service `hokma-engine`, one entry per
connection id. No file fallback and no environment variable, ever."""
from __future__ import annotations

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from hokma.connections.store import StoreUnavailable, _usable

SERVICE = "hokma-engine"


def available() -> bool:
    try:
        return _usable(keyring.get_keyring())
    except Exception:
        return False


def get(connection_id: int | str) -> str | None:
    if not available():
        return None
    try:
        return keyring.get_password(SERVICE, str(connection_id))
    except KeyringError:
        return None


def put(connection_id: int | str, value: str) -> None:
    if not available():
        raise StoreUnavailable()
    try:
        keyring.set_password(SERVICE, str(connection_id), value)
    except KeyringError as exc:
        raise StoreUnavailable() from exc


def delete(connection_id: int | str) -> None:
    if not available():
        return
    try:
        keyring.delete_password(SERVICE, str(connection_id))
    except (PasswordDeleteError, KeyringError):
        pass

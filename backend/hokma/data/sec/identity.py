"""The identity the SEC asks every automated client to declare (a User-Agent with a name and a contact email).

The owner stores it in Connections under the provider id `sec_edgar`. This module only reads it; it never keeps it in a
file and never puts it in a result.
"""
from __future__ import annotations

from typing import Callable

PROVIDER_ID = "sec_edgar"
MISSING_REASON = "sec_identity_missing"

IdentityReader = Callable[[], "str | None"]


def connections_reader() -> str | None:
    """Reads the identity from the connection store. Any failure of the store means no identity."""
    try:
        from hokma.connections.runtime import get_service

        return get_service().read_secret(PROVIDER_ID)
    except Exception:                                    # noqa: BLE001  (a store that cannot be read: no identity)
        return None


def valid_identity(text: str | None) -> str | None:
    """The identity text when it looks like a declaration (a name and an email), else None."""
    if not text:
        return None
    cleaned = " ".join(text.split())
    if "@" not in cleaned or len(cleaned) < 6:
        return None
    return cleaned


def resolve_identity(reader: IdentityReader | None = None) -> str | None:
    """The valid identity the owner saved, or None."""
    return valid_identity((reader or connections_reader)())

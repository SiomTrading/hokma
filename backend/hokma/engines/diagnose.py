"""Turns an engine failure into a code the panel has a text for: region, auth, quota, network or engine_error.

Region first: a provider that refuses the location answers 403 or 451 with a text about country or region. The
text patterns are data (catalog.json). The message never carries credentials."""
from __future__ import annotations

from hokma.engines import catalog


def classify(provider: str | None, status: int, body: str) -> str:
    if status == 451 or (status in (400, 401, 403) and catalog.region_blocked(provider, body)):
        return "region"
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "quota"
    if status == 0:
        return "network"
    return "engine_error"


def classify_text(provider: str | None, text: str) -> str:
    """For engines that fail with plain text (a CLI's error output)."""
    if catalog.region_blocked(provider, text):
        return "region"
    low = text.lower()
    if any(w in low for w in ("401", "unauthorized", "invalid api key", "invalid x-api-key", "not logged in",
                              "authentication", "please run /login")):
        return "auth"
    if any(w in low for w in ("429", "rate limit", "quota", "usage limit", "credit balance")):
        return "quota"
    return "engine_error"

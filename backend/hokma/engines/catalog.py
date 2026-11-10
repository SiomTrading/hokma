"""Providers, model tiers and the texts that mean "region". Data lives in catalog.json so it can be updated with
the normal update channel without new code."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

TIERS = ("fast", "balanced", "deep")
CLAUDE_TIERS = {"fast": "haiku", "balanced": "sonnet", "deep": "opus"}


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    return json.loads((Path(__file__).with_name("catalog.json")).read_text(encoding="utf-8"))


def provider(name: str | None) -> dict[str, Any]:
    return load()["providers"].get(name or "", {})


def tier_model(connection: dict[str, Any] | None, tier: str) -> str | None:
    """Concrete model for a tier: the connection's own map first, then the catalog suggestion for its provider."""
    if tier not in TIERS:
        return tier                                           # an exact model name typed by the student
    conn = connection or {}
    chosen = (conn.get("tier_models") or {}).get(tier)
    if chosen:
        return chosen
    if conn.get("engine") == "claude_code":
        return CLAUDE_TIERS[tier]
    return provider(conn.get("provider") or conn.get("engine")).get("tiers", {}).get(tier)


def region_blocked(provider_name: str | None, text: str) -> bool:
    patterns = load()["region_patterns"]
    pats = patterns.get("default", []) + patterns.get(provider_name or "", [])
    return any(re.search(p, text, re.I) for p in pats)

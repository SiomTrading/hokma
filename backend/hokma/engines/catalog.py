"""Providers, model tiers and the texts that mean "region". Data lives in catalog.json so it can be updated with
the normal update channel without new code.

Bug 1173: Hokma keeps no model name. A key connection reads the models its provider reports for that key (the
provider's own list endpoint) and the tiers are picked from that list with one rule (model_rules in catalog.json):
stable chat models only, the newest first, sorted into fast / balanced / deep by size words of the id. A CLI engine
(Codex, Gemini CLI) gets no model at all and uses its own default. Claude Code gets its own documented level aliases
(haiku, sonnet, opus), which the CLI itself resolves to the current model."""
from __future__ import annotations

import json
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

TIERS = ("fast", "balanced", "deep")
CLAUDE_TIERS = {"fast": "haiku", "balanced": "sonnet", "deep": "opus"}      # Claude Code's level aliases, not models
AUTO = "auto"                      # key in tier_models: the map came from the provider's list, not from the student
MAX_MODELS = 500


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    return json.loads((Path(__file__).with_name("catalog.json")).read_text(encoding="utf-8"))


def provider(name: str | None) -> dict[str, Any]:
    return load()["providers"].get(name or "", {})


def _words(kind: str) -> list[str]:
    return [str(w).lower() for w in load().get("model_rules", {}).get(kind, [])]


def _has(model_id: str, words: list[str]) -> bool:
    low = model_id.lower()
    return any(re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", low) for w in words)


def _time(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def normalize_models(data: Any) -> list[dict[str, Any]]:
    """The provider's list in one shape: {id, created, usable}. Reads the fields the providers document: OpenAI and
    Kimi `created` (unix), Anthropic `created_at` and `lifecycle` (its `deprecated_at` is not a signal: `lifecycle`
    is), OpenAI `shutdown_date`, DeepSeek
    `output_modalities`; Google's compatible list carries ids only (`models/` prefix removed). `usable` is False for a
    model its provider marks as going away or not producing text."""
    items = (data.get("data") or data.get("models") or []) if isinstance(data, dict) else data
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items if isinstance(items, list) else []:
        if isinstance(item, str):
            item = {"id": item}
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or item.get("name") or "").removeprefix("models/")      # never stripped: an id
        # with a space or a newline in it is not a model id (Vera B1), it is dropped below
        # isprintable is False for spaces' cousins, control characters (an ANSI escape) and bidi marks (U+202E):
        # such an id would show as something else in the panel (Vera B1)
        if not mid or mid in seen or not mid.isprintable() or any(c.isspace() for c in mid) or len(mid) > 200:
            continue
        seen.add(mid)
        lifecycle = str(item.get("lifecycle") or "active")
        modalities = item.get("output_modalities")
        usable = (lifecycle == "active" and not item.get("shutdown_date")
                  and (not isinstance(modalities, list) or "text" in modalities))
        out.append({"id": mid, "created": _time(item.get("created") or item.get("created_at")), "usable": usable})
    return out[:MAX_MODELS]


def _version(model_id: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", model_id)[:4])


def chat_models(provider_name: str | None, models: list[dict[str, Any]], rejected: set[str] | frozenset = frozenset()
                ) -> list[str]:
    """The stable chat models of a list, newest first (release time when the provider gives it, else the version
    numbers in the id). A provider's `id_prefix` keeps its own family (Google also lists open models)."""
    if any(not isinstance(m, dict) or "usable" not in m for m in models):
        models = normalize_models(list(models))                # plain ids: cleaned the same way (no "\n", no prefix)
    prefix = provider(provider_name).get("id_prefix") or ""
    skip = _words("skip")
    good = [m for m in models if m.get("usable", True) and m["id"] not in rejected
            and m["id"].startswith(prefix) and not _has(m["id"], skip)]
    good.sort(key=lambda m: (m.get("created") or 0.0, _version(m["id"])), reverse=True)
    return [m["id"] for m in good]


def pick_tiers(provider_name: str | None, models: list[dict[str, Any]],
               rejected: set[str] | frozenset = frozenset()) -> dict[str, str]:
    """Fast: the newest with a small-size word. Deep: the newest with a large-size word (and no small one). Balanced:
    the newest with neither. A level without its own model takes the balanced one. {} when nothing is usable."""
    ids = chat_models(provider_name, models, rejected)
    if not ids:
        return {}
    fast_w, deep_w = _words("fast"), _words("deep")
    fast = next((i for i in ids if _has(i, fast_w)), None)
    deep = next((i for i in ids if _has(i, deep_w) and not _has(i, fast_w)), None)
    balanced = next((i for i in ids if not _has(i, fast_w) and not _has(i, deep_w)), None) or deep or ids[0]
    return {"fast": fast or balanced, "balanced": balanced, "deep": deep or balanced, AUTO: "1"}


def tier_model(connection: dict[str, Any] | None, tier: str) -> str | None:
    """Concrete model for a tier: the connection's own map (picked from the provider's list, or set by the student).
    Claude Code: its level alias. Anything else without a map: None, and the engine uses its own default."""
    if tier not in TIERS:
        return tier                                           # an exact model name typed by the student
    conn = connection or {}
    chosen = (conn.get("tier_models") or {}).get(tier)
    if chosen:
        return chosen
    if conn.get("engine") == "claude_code":
        return CLAUDE_TIERS[tier]
    return None


def region_blocked(provider_name: str | None, text: str) -> bool:
    patterns = load()["region_patterns"]
    pats = patterns.get("default", []) + patterns.get(provider_name or "", [])
    return any(re.search(p, text, re.I) for p in pats)

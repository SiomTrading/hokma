"""Registry of model providers (SPEC-017: AH-56).

One table of every way Hokma can reach a model: the engines it can run (Claude Code, Codex, Gemini CLI) and the API
providers of `hokma.engines.catalog` (read from there, so a provider added to the catalog shows up here with no edit).
Which provider serves a model id is read from the shape of the id, not from a list of ids.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from hokma.engines import catalog

ENGINE_KINDS = ("claude_code", "codex", "gemini_cli")
ENGINE_LABELS = {"claude_code": "Claude Code", "codex": "Codex", "gemini_cli": "Gemini CLI"}
# provider id -> how a model id of that provider starts. A namespaced id (`vendor/model`) is an aggregator.
ID_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic", re.compile(r"^(claude|haiku$|sonnet$|opus$)", re.IGNORECASE)),
    ("openai", re.compile(r"^(gpt|o\d|chatgpt|codex)", re.IGNORECASE)),
    ("google", re.compile(r"^gemini", re.IGNORECASE)),
    ("deepseek", re.compile(r"^deepseek", re.IGNORECASE)),
    ("moonshot", re.compile(r"^(kimi|moonshot)", re.IGNORECASE)),
)


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    kind: str                  # "engine" (a program) or "api" (a key and a URL)
    wire: str                  # "openai", "anthropic", "cli"
    local: bool = False
    base_url: str = ""


def providers() -> list[Provider]:
    """Every provider, engines first, then the API providers of the catalog in catalog order."""
    out = [Provider(kind, ENGINE_LABELS[kind], "engine", "cli") for kind in ENGINE_KINDS]
    for pid, info in catalog.load()["providers"].items():
        if pid in ENGINE_KINDS:
            continue
        out.append(Provider(pid, str(info.get("label", pid)), "api", str(info.get("wire", "openai")), bool(info.get("local")),
                            str(info.get("base_url", ""))))
    return out


def get(provider_id: str) -> Provider | None:
    return next((p for p in providers() if p.id == provider_id), None)


def is_supported(provider_id: str) -> bool:
    """True when Hokma can talk to this provider through an engine."""
    return get(provider_id) is not None


def provider_for(model_id: str | None) -> str | None:
    """The provider a model id belongs to, or None when the id says nothing (a custom or local model).

    An id with a vendor namespace (`vendor/model`) is served by the aggregator `openrouter`."""
    name = (model_id or "").strip()
    if not name:
        return None
    if "/" in name:
        return "openrouter"
    for provider_id, pattern in ID_PATTERNS:
        if pattern.match(name):
            return provider_id
    return None


def engine_kind_for(provider_id: str | None) -> str:
    """The engine that runs a provider: the engine itself for a CLI, `api` for a key provider."""
    if provider_id in ENGINE_KINDS:
        return str(provider_id)
    return "api"

"""What each model accepts at the API level (SPEC-017: TA-89).

Hokma runs on Claude Code by default, but a student can connect any engine or any OpenAI-compatible endpoint. This is the
one table that knows which model families reject which parameters or need which way of asking for structured output, so
nothing else carries a ladder of `if model.startswith(...)`. Models are matched by exact id first, then by family pattern,
then by the default row. No version number of any model is written here: a model newer than the table inherits its family.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

StructuredMethod = Literal["function_calling", "json_mode", "json_schema", "prompt_only", "none"]


@dataclass(frozen=True)
class ModelCapabilities:
    supports_tool_choice: bool = True
    supports_json_mode: bool = True
    supports_json_schema: bool = True
    preferred_structured_method: StructuredMethod = "function_calling"
    supports_temperature: bool = True               # reasoning families ignore or reject it
    requires_reasoning_roundtrip: bool = False      # the reasoning of earlier turns must be sent back
    requires_reasoning_split: bool = False          # the reasoning arrives in its own field, not in the text
    supports_images: bool = True


DEFAULT = ModelCapabilities()
CLAUDE = ModelCapabilities(supports_json_mode=False, supports_json_schema=False, preferred_structured_method="function_calling")
REASONING_OPENAI = ModelCapabilities(supports_temperature=False, preferred_structured_method="json_schema")
THINKING_NO_CHOICE = ModelCapabilities(supports_tool_choice=False, supports_json_mode=True, supports_json_schema=False,
                                       preferred_structured_method="function_calling", requires_reasoning_roundtrip=True,
                                       supports_images=False)
CHAT_NO_SCHEMA = ModelCapabilities(supports_json_schema=False, preferred_structured_method="function_calling", supports_images=False)
SPLIT_REASONING = ModelCapabilities(supports_tool_choice=False, supports_json_mode=False, supports_json_schema=False,
                                    preferred_structured_method="function_calling", requires_reasoning_split=True,
                                    supports_images=False)
LOCAL_MODEL = ModelCapabilities(supports_tool_choice=False, supports_json_schema=False, preferred_structured_method="json_mode",
                                supports_images=False)
PROMPT_ONLY = ModelCapabilities(supports_tool_choice=False, supports_json_mode=False, supports_json_schema=False,
                                preferred_structured_method="prompt_only", supports_images=False)

# Exact ids that do not follow their family.
BY_ID: dict[str, ModelCapabilities] = {
    "deepseek-chat": CHAT_NO_SCHEMA,
}

# Family patterns, first match wins. Matching is on the id with a known namespace (`vendor/`) removed.
BY_PATTERN: list[tuple[re.Pattern[str], ModelCapabilities]] = [
    (re.compile(r"^(haiku|sonnet|opus)$"), CLAUDE),                    # the Claude Code level aliases
    (re.compile(r"^claude"), CLAUDE),
    (re.compile(r"^deepseek-(v\d|reasoner)"), THINKING_NO_CHOICE),
    (re.compile(r"^deepseek"), CHAT_NO_SCHEMA),
    (re.compile(r"^minimax-m\d", re.IGNORECASE), SPLIT_REASONING),
    (re.compile(r"^(o\d|gpt-\d+(\.\d+)?-?(pro|thinking|reasoning))"), REASONING_OPENAI),
    (re.compile(r"^(kimi|moonshot)"), CHAT_NO_SCHEMA),
    (re.compile(r"^(llama|mistral|mixtral|qwen|gemma|phi)"), LOCAL_MODEL),
]
NAMESPACES = ("deepseek/", "anthropic/", "openai/", "google/", "moonshotai/", "minimax/", "meta-llama/", "mistralai/", "qwen/")


def _strip_namespace(model: str) -> str:
    low = model.strip()
    for prefix in NAMESPACES:
        if low.lower().startswith(prefix):
            return low[len(prefix):]
    return low


def get_capabilities(model: str | None) -> ModelCapabilities:
    """Capabilities of a model id. An empty or unknown id gets the default row."""
    name = _strip_namespace(model or "")
    if not name:
        return DEFAULT
    if name in BY_ID:
        return BY_ID[name]
    for pattern, caps in BY_PATTERN:
        if pattern.match(name):
            return caps
    return DEFAULT


def structured_method(model: str | None, *, want_schema: bool = True) -> StructuredMethod:
    """How to ask this model for structured output. Falls back from the preferred way to the next one it supports."""
    caps = get_capabilities(model)
    if not want_schema:
        return "json_mode" if caps.supports_json_mode else "prompt_only"
    preferred = caps.preferred_structured_method
    if preferred == "json_schema" and caps.supports_json_schema:
        return "json_schema"
    if preferred == "function_calling":
        return "function_calling"
    if caps.supports_json_schema:
        return "json_schema"
    if caps.supports_json_mode:
        return "json_mode"
    return "prompt_only"


def drop_unsupported(model: str | None, kwargs: dict) -> dict:
    """`kwargs` without the parameters this model rejects (`temperature` for a reasoning family, `tool_choice` where unsupported)."""
    caps = get_capabilities(model)
    out = dict(kwargs)
    if not caps.supports_temperature:
        out.pop("temperature", None)
    if not caps.supports_tool_choice:
        out.pop("tool_choice", None)
    return out

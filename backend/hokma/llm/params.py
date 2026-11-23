"""Call parameters per provider (SPEC-017: TA-88).

The same wish (a lower temperature, less reasoning, a cap on output) is spelled differently by each provider. `provider_kwargs`
turns the desk settings into the parameters one provider takes, and leaves out what the provider does not take, so a call
never fails because of a parameter its provider rejects. A setting that is None is left out: the provider keeps its default.
"""
from __future__ import annotations

from typing import Any, Mapping

from hokma.llm.retry import coerce_max_retries, coerce_max_tokens

REASONING_KEY = {"google": "thinking_level", "openai": "reasoning_effort", "anthropic": "effort"}
REASONING_LEVELS = {
    "google": ("minimal", "low", "medium", "high"),
    "openai": ("minimal", "low", "medium", "high"),
    "anthropic": ("low", "medium", "high"),
}
MAX_TOKENS_KEY = {"google": "max_output_tokens"}


def _present(value: Any) -> bool:
    return value is not None and value != ""


def reasoning_level(provider: str, level: Any) -> str | None:
    """The reasoning level in the provider's own words, or ValueError when the provider has no such level."""
    if not _present(level):
        return None
    allowed = REASONING_LEVELS.get(provider)
    if allowed is None:
        return None
    text = str(level).strip().lower()
    if text not in allowed:
        raise ValueError(f"{provider} reasoning level must be one of {', '.join(allowed)}, got {level!r}")
    return text


def provider_kwargs(provider: str, config: Mapping[str, Any]) -> dict[str, Any]:
    """The parameters of one provider from the desk settings.

    Recognised keys of `config`: `temperature`, `llm_max_retries`, `max_tokens`, `reasoning_level`. Others are ignored."""
    provider = (provider or "").lower()
    out: dict[str, Any] = {}
    level = reasoning_level(provider, config.get("reasoning_level"))
    if level is not None:
        out[REASONING_KEY[provider]] = level
    temperature = config.get("temperature")
    if _present(temperature):
        value = float(temperature)
        if not 0.0 <= value <= 2.0:
            raise ValueError(f"temperature must be between 0 and 2, got {temperature!r}")
        out["temperature"] = value
    retries = config.get("llm_max_retries")
    if _present(retries):
        out["max_retries"] = coerce_max_retries(retries)
    tokens = config.get("max_tokens")
    if _present(tokens):
        out[MAX_TOKENS_KEY.get(provider, "max_tokens")] = coerce_max_tokens(tokens)
    return out

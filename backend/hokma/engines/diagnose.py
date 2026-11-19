"""Turns an engine failure into a code the panel has a text for: region, auth, model, quota, unavailable, network or
engine_error.

Region first: a provider that refuses the location answers 400, 403 or 451 with a text about country or region. The
text patterns are data (catalog.json). Bug 1173: Google answers a bad key with 400 INVALID_ARGUMENT and the reason
API_KEY_INVALID (not 401), and a model a key cannot use with 404; both read the body, not only the status. The message
never carries credentials."""
from __future__ import annotations

import json
from typing import Any

from hokma.engines import catalog

_KEY_WORDS = ("api_key_invalid", "api key not valid", "invalid api key", "incorrect api key", "invalid x-api-key",
              "invalid_api_key", "api key expired", "api_key_expired")
_MODEL_WORDS = ("model_not_found", "no longer available", "does not exist", "is not found for api version",
                "not supported for generatecontent", "unknown model", "model not found",
                # the model exists but does not take this request (Chat Completions, tools): another one from the
                # provider's list is tried (bug 1173, no model named here)
                "responses api", "v1/responses", "not supported with this model", "not a chat model",
                "does not support tools", "does not support function", "model is required", "must provide a model")


def _error_fields(body: str) -> str:
    """The text that names the cause: message, status and reasons of an `error` object, in any of the shapes the
    providers use (`{"error": {...}}` or Google's `[{"error": {...}}]`). Falls back to the raw body."""
    try:
        data: Any = json.loads(body)
    except (TypeError, ValueError):
        return body or ""
    if isinstance(data, list) and data:
        data = data[0]
    err = data.get("error") if isinstance(data, dict) else None
    if not isinstance(err, dict):
        return body or ""
    parts = [str(err.get(k) or "") for k in ("message", "status", "code", "type")]
    for d in err.get("details") or []:
        if isinstance(d, dict):
            parts.append(str(d.get("reason") or ""))
    return " ".join(p for p in parts if p)


def classify(provider: str | None, status: int, body: str) -> str:
    text = _error_fields(body)
    low = text.lower()
    if status == 451 or (status in (400, 403) and catalog.region_blocked(provider, body + " " + text)):
        return "region"
    if status == 401 or (status in (400, 403) and any(w in low for w in _KEY_WORDS)):
        return "auth"
    if status == 403:
        return "auth"
    if status == 404 or any(w in low for w in _MODEL_WORDS):
        return "model"
    if status in (402, 429):
        return "quota"
    if status in (500, 502, 503, 504):
        return "unavailable"
    if status == 0:
        return "network"
    return "engine_error"


def classify_text(provider: str | None, text: str) -> str:
    """For engines that fail with plain text (a CLI's error output)."""
    if catalog.region_blocked(provider, text):
        return "region"
    low = text.lower()
    if any(w in low for w in ("401", "unauthorized", "not logged in", "authentication", "please run /login",
                              *_KEY_WORDS)):
        return "auth"
    if any(w in low for w in ("429", "rate limit", "quota", "usage limit", "credit balance")):
        return "quota"
    if any(w in low for w in _MODEL_WORDS):
        return "model"
    return "engine_error"


_CONFIG = ("error loading configuration", "error loading config", "no longer supported", "invalid type",
           "unknown field", "config.toml", "invalid value", "unknown variant")
_REFUSED = ("not enabled", "disabled", "not allowed", "not permitted", "is blocked", "restricted", "turned off",
            "not available for your", "does not have access", "doesn't have access", "no access")
_WORKSPACE = ("workspace", "admin", "organization", "enterprise", "business", "your plan", "edu ")


def classify_codex(text: str) -> str:
    """Bug 1175: a ChatGPT workspace managed by an admin can keep Codex off for its members or not allow device code
    sign-in (OpenAI documents both settings for Enterprise workspaces; the screen names no plan). OpenAI does not
    publish the exact text of those refusals, so the words decide; the real text always goes to engine.log.
    `device_auth`: device code sign-in refused. `workspace`: the workspace keeps Codex off. Else classify_text."""
    low = (text or "").lower()
    if catalog.region_blocked("openai", text or ""):
        return "region"
    if any(w in low for w in _CONFIG):
        return "config"
    refused = any(w in low for w in _REFUSED) or "403" in low or "forbidden" in low
    if "device" in low and refused:
        return "device_auth"
    if refused and ("codex" in low or any(w in low for w in _WORKSPACE)) and "rate" not in low:
        return "workspace"
    return classify_text("openai", text)


def redact(text: str, *secrets: str | None) -> str:
    """The provider's answer with every secret replaced (a key echoed back in an error must not reach engine.log)."""
    for s in secrets:
        if s and len(s) >= 6:
            text = text.replace(s, "[key]")
    return text

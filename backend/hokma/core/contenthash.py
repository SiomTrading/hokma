"""Content hash and stable rendering that ignore generation dates (SPEC-017: AH-47, AH-48).

Lives in hokma.core so that worker processes of the quant modules can import it: hokma.desks refuses to load outside the server.

Two snapshots with the same numbers hash the same even if they were generated at different times, so an identical
payload never pays for a second model call. A changed number changes the hash.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime
from typing import Any

# Keys that only say when a payload was produced, never what it says.
VOLATILE_KEYS = frozenset({"generated_at", "created_at", "rendered_at", "fetched_at", "run_at", "timestamp_generated"})


def _normalise(value: Any, ignore: frozenset[str]) -> Any:
    if isinstance(value, dict):
        return {str(k): _normalise(v, ignore) for k, v in sorted(value.items(), key=lambda kv: str(kv[0])) if k not in ignore}
    if isinstance(value, (list, tuple)):
        return [_normalise(v, ignore) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_normalise(v, ignore) for v in value), key=repr)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Inf" if value > 0 else "-Inf"
        return repr(round(value, 12))
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _normalise(value.to_dict(), ignore)
    if hasattr(value, "tolist") and callable(value.tolist):
        return _normalise(value.tolist(), ignore)
    return value


def render(payload: Any, *, ignore: frozenset[str] | set[str] = VOLATILE_KEYS) -> bytes:
    """Stable bytes of a payload: sorted keys, no volatile keys, floats rounded to 12 digits."""
    clean = _normalise(payload, frozenset(ignore))
    return json.dumps(clean, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")


def content_hash(payload: Any, *, ignore: frozenset[str] | set[str] = VOLATILE_KEYS) -> str:
    """sha256 of the stable render."""
    return hashlib.sha256(render(payload, ignore=ignore)).hexdigest()

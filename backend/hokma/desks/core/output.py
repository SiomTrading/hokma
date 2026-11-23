"""Structured output that degrades instead of blocking (SPEC-017: TA-35).

An agent that must answer with a JSON object gets one try with the schema. If the answer is not valid (no JSON, a missing
field, a wrong type) the same question is asked once more as free text, and the run goes on with the text and
`degraded=True`, so the desk shows a warning instead of stopping. A model call that fails twice returns an empty result
with the error; it never raises into the run.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

from hokma.llm.jsonx import LLMParseError, extract_json

JSON_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,), "array": (list, tuple), "string": (str,), "boolean": (bool,), "null": (type(None),),
    "integer": (int,), "number": (int, float),
}


class SchemaError(ValueError):
    """A value does not match its schema. `path` says where."""

    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path or '$'}: {message}")
        self.path = path


def validate_schema(value: Any, schema: dict, path: str = "") -> None:
    """Check `value` against the JSON schema subset desks use: type, enum, required, properties, items, minimum, maximum,
    minLength, maxLength. Raises SchemaError at the first mismatch. Unknown keywords are ignored."""
    expected = schema.get("type")
    if expected is not None:
        names = expected if isinstance(expected, list) else [expected]
        ok = False
        for name in names:
            kinds = JSON_TYPES.get(name)
            if kinds is None:
                continue
            if name in ("integer", "number") and isinstance(value, bool):
                continue
            if name == "integer" and isinstance(value, float):
                ok = ok or value.is_integer()
                continue
            if isinstance(value, kinds):
                ok = True
        if not ok:
            raise SchemaError(path, f"expected {'/'.join(names)}, got {type(value).__name__}")
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError(path, f"{value!r} is not one of {schema['enum']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise SchemaError(path, "number is not finite")
        if "minimum" in schema and value < schema["minimum"]:
            raise SchemaError(path, f"{value} is below the minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise SchemaError(path, f"{value} is above the maximum {schema['maximum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise SchemaError(path, f"text is shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise SchemaError(path, f"text is longer than {schema['maxLength']}")
    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                raise SchemaError(f"{path}.{name}", "required field is missing")
        for name, sub in schema.get("properties", {}).items():
            if name in value:
                validate_schema(value[name], sub, f"{path}.{name}")
    if isinstance(value, (list, tuple)) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            validate_schema(item, schema["items"], f"{path}[{index}]")


@dataclass(frozen=True)
class StructuredResult:
    text: str                                   # the text to show: the rendered object, or the free text of a degraded run
    value: dict | None = None                   # the validated object; None when degraded
    degraded: bool = False
    warnings: tuple[str, ...] = field(default_factory=tuple)
    error: str = ""


def parse_structured(text: str, schema: dict, render: Callable[[dict], str] | None = None) -> StructuredResult:
    """The answer `text` as a validated object, or a degraded result that keeps the text and says why."""
    try:
        value = extract_json(text)
        validate_schema(value, schema)
    except (LLMParseError, SchemaError) as exc:
        return StructuredResult(text=text.strip(), value=None, degraded=True,
                                warnings=(f"structured output degraded to text: {exc}",))
    return StructuredResult(text=render(value) if render else text.strip(), value=value)


def ask_structured(llm: Any, messages: list[dict], schema: dict, *, render: Callable[[dict], str] | None = None,
                   max_tokens: int = 4096) -> StructuredResult:
    """Ask `llm` for an object. Valid: the object. Invalid or failed: one more try as free text, then `degraded=True`.

    The free text try gets the same messages, without the schema. If it fails too, the result is empty with `error` set."""
    warnings: list[str] = []
    try:
        reply = llm.complete(messages, schema=schema, max_tokens=max_tokens)
        parsed = parse_structured(reply.text, schema, render)
        if not parsed.degraded:
            return parsed
        warnings.extend(parsed.warnings)
    except Exception as exc:                                    # noqa: BLE001  (a failed call must not stop the run)
        warnings.append(f"structured call failed ({type(exc).__name__}): {exc}")
    try:
        reply = llm.complete(messages, schema=None, max_tokens=max_tokens)
    except Exception as exc:                                    # noqa: BLE001
        warnings.append(f"free text call failed ({type(exc).__name__}): {exc}")
        return StructuredResult(text="", degraded=True, warnings=tuple(warnings), error=str(exc))
    return StructuredResult(text=reply.text.strip(), value=None, degraded=True, warnings=tuple(warnings))

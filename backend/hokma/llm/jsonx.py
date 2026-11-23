"""Reading JSON out of what a model answered (SPEC-017: AH-53, AH-54).

Reasoning models answer with a list of blocks: thinking blocks first, then the text. Only the text blocks are the answer:
a thinking block written in front of the JSON is what breaks the parse. `_flatten` keeps the text and drops the rest, and
`extract_json` then tries, in order, a fenced block, the whole text and the first balanced `{...}`.
"""
from __future__ import annotations

import json
import re
from typing import Any

REASONING_TAGS = ("think", "thinking", "reasoning", "thought")
_REASONING_BLOCK = re.compile(r"<(" + "|".join(REASONING_TAGS) + r")>.*?</\1>", re.DOTALL | re.IGNORECASE)
_OPEN_REASONING = re.compile(r"^\s*<(" + "|".join(REASONING_TAGS) + r")>.*\Z", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"```(?:json|JSON)?\s*(\{.*?\})\s*```", re.DOTALL)
TEXT_TYPES = frozenset({"text", "output_text"})


class LLMParseError(ValueError):
    """The answer holds no JSON object."""


def _flatten(content: Any, sep: str = "\n") -> str:
    """The answer of one model as plain text.

    A string is returned as it is. A list of blocks keeps the `text` blocks (and plain strings) and drops thinking,
    tool and image blocks. `sep` joins whole blocks: a newline for a whole message, nothing for stream fragments of one
    continuing string. `None` is the empty string."""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in TEXT_TYPES:
                parts.append(str(block.get("text", "")))
        return sep.join(parts)
    if isinstance(content, dict):
        return _flatten([content], sep) if content.get("type") in TEXT_TYPES else ""
    return "" if content is None else str(content)


flatten = _flatten


def strip_reasoning(text: str) -> str:
    """`text` without `<think>...</think>`-style blocks. An opening tag that never closes drops everything after it."""
    cleaned = _REASONING_BLOCK.sub("", text)
    cleaned = _OPEN_REASONING.sub("", cleaned)
    return cleaned.strip()


def _balanced_object(text: str) -> str | None:
    """The first `{...}` of `text` whose braces balance, honouring strings and escapes."""
    start = text.find("{")
    while start != -1:
        depth, in_string, escaped = 0, False, False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:index + 1]
                    try:
                        json.loads(candidate)
                        return candidate
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


def extract_json(text: Any) -> dict:
    """The first JSON object in an answer. Reasoning blocks are removed first.

    Raises LLMParseError when there is none. A JSON value that is not an object (a list, a number) is not an answer."""
    flat = strip_reasoning(_flatten(text))
    fence = _FENCE.search(flat)
    if fence:
        try:
            value = json.loads(fence.group(1))
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
    try:
        value = json.loads(flat.strip())
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    candidate = _balanced_object(flat)
    if candidate is not None:
        value = json.loads(candidate)
        if isinstance(value, dict):
            return value
    raise LLMParseError(f"no JSON object found in the answer: {flat[:200]!r}")

"""Registry of agent tools with classified errors (SPEC-017, B9; ED-032).

A tool handler raises `ToolError(code, message, suggestions)` for a failure the model can act on. The MCP layer turns it
into an error result whose text is JSON: {"error_code": ..., "message": ..., "suggestions": [...]}. The codes are the
only ones the agents see: `no_data`, `unavailable`, `stale`, `bad_input`, `blocked`, plus `refused` and `awaiting_owner` for execution (SPEC-018).
"""
from __future__ import annotations

import json
from typing import Any, Callable

CODES = ("no_data", "unavailable", "stale", "bad_input", "blocked", "refused", "awaiting_owner")

Handler = Callable[[Any, Any, dict], str]
ToolSpec = tuple[str, dict[str, Any], list[str], Handler, bool]


class ToolError(ValueError):
    """A classified tool failure."""

    def __init__(self, code: str, message: str, suggestions: list[str] | None = None) -> None:
        if code not in CODES:
            raise ValueError(f"unknown error code {code!r}")
        self.code = code
        self.message = message
        self.suggestions = list(suggestions or [])
        super().__init__(self.payload())

    def payload(self) -> str:
        return json.dumps({"error_code": self.code, "message": self.message, "suggestions": self.suggestions}, ensure_ascii=False)


class ToolSet:
    """A group of tools declared with a decorator and merged into the server table."""

    def __init__(self) -> None:
        self.tools: dict[str, ToolSpec] = {}

    def tool(self, name: str, description: str, props: dict[str, Any], required: list[str] | None = None, *, writes: bool = False):
        def wrap(fn: Handler) -> Handler:
            if name in self.tools:
                raise ValueError(f"tool {name!r} is declared twice")
            self.tools[name] = (description, props, list(required or []), fn, writes)
            return fn
        return wrap

    def names(self) -> set[str]:
        return set(self.tools)

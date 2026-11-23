"""Tools of the agents: each role sees only its own set, and every tool goes through a module (SPEC-017: TA-15, TA-79).

A `ToolCatalog` holds every tool the desk knows. `assign` gives a role or an analyst its set; `toolbox_for` returns a
`Toolbox` that offers those tools and refuses any other with a readable error. Handlers call the Hokma modules (the quant
registry, the data functions), never a library. Errors reach the model as a class: no_data, unavailable, stale, bad_input
or blocked, the same classes the MCP server uses.
"""
from __future__ import annotations

import dataclasses
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from hokma.desks.core.graph import AnalystSpec
from hokma.desks.core.state import ToolCall
from hokma.desks.core.tool_audit import ToolAuditLog
from hokma.quant import registry
from hokma.quant.errors import BadInput, BlockedFunction, UnknownFunction

ERROR_KINDS = ("no_data", "unavailable", "stale", "bad_input", "blocked")
MAX_RESULT_CHARS = 20_000
_NAME = re.compile(r"^[a-z][a-z0-9_]{2,40}$")


class ToolNotAllowed(Exception):
    """A role asked for a tool that is not in its set."""

    def __init__(self, holder: str, tool: str, allowed: Iterable[str]) -> None:
        self.holder, self.tool, self.allowed = holder, tool, tuple(sorted(allowed))
        super().__init__(f"{holder} cannot use the tool {tool!r}; its tools are: {', '.join(self.allowed) or '(none)'}")


class ToolFailure(Exception):
    """A handler's own classified failure. `kind` is one of ERROR_KINDS."""

    def __init__(self, kind: str, message: str) -> None:
        if kind not in ERROR_KINDS:
            raise ValueError(f"unknown error class {kind!r}")
        self.kind = kind
        super().__init__(message)


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    data: Any = None
    error_kind: str = ""
    message: str = ""

    def to_text(self) -> str:
        body = {"ok": True, "data": self.data} if self.ok else {"ok": False, "error": {"kind": self.error_kind, "message": self.message}}
        text = json.dumps(body, ensure_ascii=False, default=_encode)
        if len(text) > MAX_RESULT_CHARS:
            text = json.dumps({"ok": self.ok, "truncated": True, "preview": text[:MAX_RESULT_CHARS]}, ensure_ascii=False)
        return text


def _encode(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    return str(value)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    handler: Callable[..., Any] = field(compare=False, repr=False)
    example: Mapping[str, Any] | Callable[[], Mapping[str, Any] | None] | None = field(default=None, compare=False)
    mutates: bool = False

    def __post_init__(self) -> None:
        if not _NAME.match(self.name):
            raise ValueError(f"tool name {self.name!r} must be snake_case")
        if not self.description.strip():
            raise ValueError(f"tool {self.name} needs a description")

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": dict(self.input_schema)}

    def resolve_example(self) -> Mapping[str, Any] | None:
        return self.example() if callable(self.example) else self.example


class ToolCatalog:
    """Every tool of the desk and who may use which. Holds its own state: catalogs of two desks never mix."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._holders: dict[str, tuple[str, ...]] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool {spec.name!r} is already registered")
        self._tools[spec.name] = spec

    def assign(self, holder: str, names: Iterable[str]) -> None:
        wanted = tuple(dict.fromkeys(names))
        if unknown := [n for n in wanted if n not in self._tools]:
            raise ValueError(f"{holder}: unknown tool(s) {unknown}")
        self._holders[holder] = wanted

    def holders(self) -> tuple[str, ...]:
        return tuple(self._holders)

    def toolbox_for(self, holder: str, *, audit: ToolAuditLog | None = None) -> "Toolbox":
        if holder not in self._holders:
            raise ToolNotAllowed(holder, "*", ())
        return Toolbox(holder, {n: self._tools[n] for n in self._holders[holder]}, audit)


class Toolbox:
    """The tools one role or analyst may use. Anything else is refused."""

    def __init__(self, holder: str, tools: Mapping[str, ToolSpec], audit: ToolAuditLog | None = None) -> None:
        self.holder, self._tools, self._audit = holder, dict(tools), audit

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._tools.values())

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values()]

    def call(self, name: str, arguments: Mapping[str, Any] | None = None, *, agent: str | None = None) -> ToolResult:
        """Run one tool. A tool outside the set raises ToolNotAllowed; every other failure comes back as a classified result."""
        who, args, started = agent or self.holder, dict(arguments or {}), time.monotonic()
        spec = self._tools.get(name)
        if spec is None:
            err = ToolNotAllowed(self.holder, name, self._tools)
            self._record(who, name, args, ToolResult(False, error_kind="blocked", message=str(err)), started)
            raise err
        result = self._execute(spec, args)
        self._record(who, name, args, result, started)
        return result

    def run(self, call: ToolCall, *, agent: str | None = None) -> str:
        """Graph entry point: the result as text for the model. A refused tool becomes a readable error text."""
        try:
            return self.call(call.name, call.arguments, agent=agent).to_text()
        except ToolNotAllowed as exc:
            return ToolResult(False, error_kind="blocked", message=str(exc)).to_text()

    def runner(self) -> Callable[[ToolCall], str]:
        return self.run

    def _execute(self, spec: ToolSpec, args: dict[str, Any]) -> ToolResult:
        try:
            data = spec.handler(**args)
        except ToolFailure as exc:
            return ToolResult(False, error_kind=exc.kind, message=str(exc))
        except BlockedFunction as exc:
            return ToolResult(False, error_kind="blocked", message=str(exc))
        except (BadInput, UnknownFunction, TypeError, ValueError, KeyError) as exc:
            return ToolResult(False, error_kind="bad_input", message=str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc))
        except Exception as exc:   # a handler that breaks is reported as unavailable, never raised into the run
            return ToolResult(False, error_kind="unavailable", message=f"{type(exc).__name__}: {exc}"[:300])
        return ToolResult(True, data)

    def _record(self, who: str, name: str, args: Mapping[str, Any], result: ToolResult, started: float) -> None:
        if self._audit is not None:
            self._audit.record(agent=who, tool=name, arguments=args, ok=result.ok, result_text=result.to_text() if result.ok else result.message,
                               error_kind=result.error_kind, started=started)


def analyst_spec(key: str, toolbox: Toolbox, max_tool_rounds: int = 3) -> AnalystSpec:
    """The graph entry of one analyst: its schemas and a runner bound to its own toolbox."""
    return AnalystSpec(key, tuple(toolbox.schemas()), toolbox.runner(), max_tool_rounds)


# ---- quant tools: thin wrappers over the registry (the only way an agent reaches a calculation) -------------------

def _quant_example() -> Mapping[str, Any] | None:
    found = registry.list()
    return {"name": found[0].name} if found else None


def _quant_call_example() -> Mapping[str, Any] | None:
    for spec in registry.list():
        if not any(isinstance(v, dict) and v.get("required") for v in spec.inputs.values()):
            return {"name": spec.name, "arguments": {}}
    return None


def quant_tool_specs() -> list[ToolSpec]:
    """quant_list, quant_describe and quant_call. They call hokma.quant.registry and nothing else."""
    def quant_list(module: str | None = None) -> list[dict[str, Any]]:
        return [{"name": s.name, "module": s.module, "summary": s.summary, "piece_ids": list(s.piece_ids)} for s in registry.list(module)]

    def quant_describe(name: str) -> dict[str, Any]:
        return registry.describe(name).describe()

    def quant_call(name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if arguments is not None and not isinstance(arguments, Mapping):
            raise BadInput("arguments must be an object")
        return registry.call(name, **dict(arguments or {})).to_dict()

    return [
        ToolSpec("quant_list", "List the registered calculations of the metrics, risk and validation modules, optionally one module.",
                 {"type": "object", "properties": {"module": {"type": "string", "enum": ["metrics", "risk", "validation"]}}}, quant_list, example={}),
        ToolSpec("quant_describe", "Describe one registered calculation: its inputs, unit, summary and the pieces behind it.",
                 {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}, quant_describe, example=_quant_example),
        ToolSpec("quant_call", "Run one registered calculation with named arguments and get its value, unit, method and warnings.",
                 {"type": "object", "properties": {"name": {"type": "string"}, "arguments": {"type": "object"}}, "required": ["name"]}, quant_call,
                 example=_quant_call_example),
    ]

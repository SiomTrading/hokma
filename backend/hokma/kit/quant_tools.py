"""Tools that expose the quant modules to agents: `quant_list`, `quant_describe` and `quant_call` (SPEC-017, B9).

Few tools plus access by name: an agent lists or searches the registry, reads the description of a function and calls it by
name. Every call goes through `hokma.quant.registry.call`, so the size and time limits and the worker process apply. A
result carries its value, unit, method, parameters, input hash, cut-off and warnings; nothing here holds keys or account ids.
"""
from __future__ import annotations

import difflib
import json
from typing import Any

from hokma.kit.tool_registry import ToolError, ToolSet

TOOLSET = ToolSet()
MAX_LIST = 200


def _s(desc: str = "", **extra: Any) -> dict[str, Any]:
    return {"type": "string", "description": desc, **extra}


def _o(desc: str = "") -> dict[str, Any]:
    return {"type": "object", "description": desc}


def _registry():
    from hokma.quant import registry

    registry.load_all()
    return registry


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def to_input(value: Any, kind: str) -> Any:
    """Turn a JSON argument into the pandas input a function declares.

    A series is a list of numbers, or {"values": [...], "index": [ISO dates]}. A table is {"columns": {name: [...]}, "index": [...]}
    or {name: [...]}. Anything else is passed as it is."""
    import pandas as pd

    from hokma.quant.errors import BadInput

    try:
        if kind == "series":
            if isinstance(value, dict):
                idx = pd.to_datetime(value["index"]) if "index" in value else None
                return pd.Series(value["values"], index=idx, dtype=float)
            if isinstance(value, list):
                return pd.Series(value, dtype=float)
        if kind in ("dataframe", "table"):
            if isinstance(value, dict):
                cols = value.get("columns", value) if isinstance(value.get("columns", None), dict) else value
                idx = pd.to_datetime(value["index"]) if isinstance(value.get("index"), list) else None
                data = {k: v for k, v in cols.items() if k != "index"}
                frame = pd.DataFrame(data, index=idx)
                try:
                    return frame.astype(float)
                except (ValueError, TypeError):      # a table with text columns (fiscal quarters, labels) keeps them as text
                    return frame
    except (KeyError, ValueError, TypeError) as exc:
        raise BadInput(f"cannot read the {kind}: {exc}") from exc
    return value


@TOOLSET.tool("quant_list", "List the registered quant functions (metrics, risk, validation, options, fundamentals, indicators, stats, portfolio) with a one line summary. Filter by module or by a word in the name or summary. Blocked library functions are not listed.", {
    "module": _s("metrics, risk, validation, options, fundamentals, indicators, stats or portfolio.", enum=["metrics", "risk", "validation", "options", "fundamentals", "indicators", "stats", "portfolio"]),
    "search": _s("A word to look for in the name or summary."), "limit": {"type": "integer", "description": f"At most {MAX_LIST}."}})
def quant_list(u, c, a: dict) -> str:
    registry = _registry()
    specs = registry.list(a.get("module"))
    word = (a.get("search") or "").strip().lower()
    if word:
        specs = [s for s in specs if word in s.name.lower() or word in s.summary.lower()]
    limit = min(int(a.get("limit") or MAX_LIST), MAX_LIST)
    return _dump({"functions": [{"name": s.name, "summary": s.summary} for s in specs[:limit]], "total": len(specs)})


@TOOLSET.tool("quant_describe", "Describe one quant function: summary, inputs with types and defaults, and the pieces behind it.", {
    "name": _s("Registry name, e.g. metrics.sharpe.")}, ["name"])
def quant_describe(u, c, a: dict) -> str:
    from hokma.quant.errors import UnknownFunction

    registry = _registry()
    try:
        return _dump(registry.describe(a["name"]).describe())
    except UnknownFunction:
        raise ToolError("bad_input", f"there is no quant function named {a['name']!r}", _similar(registry, a["name"])) from None


def _similar(registry, name: str) -> list[str]:
    names = [s.name for s in registry.list(include_blocked=True)]
    return difflib.get_close_matches(name, names, n=5, cutoff=0.5) or ["quant_list"]


@TOOLSET.tool("quant_call", "Call a registered quant function by name, for example metrics.sharpe, risk.var or validation.psr. Series are lists of numbers or {values, index}; tables are {column: [numbers]}. The answer has the value, unit, method, parameters, input hash and warnings. Sizes and run time are limited.", {
    "name": _s("Registry name."), "params": _o("Arguments of the function, by name (see quant_describe).")}, ["name"])
def quant_call(u, c, a: dict) -> str:
    from hokma.quant.errors import BadInput, BlockedFunction, UnknownFunction

    registry = _registry()
    name = a["name"]
    params = a.get("params") or {}
    if not isinstance(params, dict):
        raise ToolError("bad_input", "params must be an object")
    try:
        spec = registry.describe(name)
        kwargs = {key: to_input(val, (spec.inputs.get(key) or {}).get("type", "") if isinstance(spec.inputs.get(key), dict) else "")
                  for key, val in params.items()}
        result = registry.call(name, **kwargs)
    except UnknownFunction:
        raise ToolError("bad_input", f"there is no quant function named {name!r}", _similar(registry, name)) from None
    except BlockedFunction as exc:
        raise ToolError("blocked", str(exc), [exc.replacement] if exc.replacement else []) from None
    except BadInput as exc:
        raise ToolError("bad_input", str(exc), [f"quant_describe name={name}"]) from None
    return _dump(result.to_dict())


TOOLS = TOOLSET.tools

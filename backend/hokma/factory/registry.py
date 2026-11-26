"""Registry of alphas and strategies (SPEC-020: AH-12).

Built-in alphas register when the registry is created. Entries declared in YAML files (kit/template/service/strategies/*.yaml)
register when the folder is loaded. Every failure names the file and the line. The loader is strict: unknown keys, a missing
class, a class outside the factory packages and repeated ids all fail.

A file holds one entry (a mapping) or a list of entries. Keys: id, kind (alpha or strategy), class, line, description, params.
"""
from __future__ import annotations

import csv
import importlib
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from hokma.desks.core.signal import LINES
from hokma.factory.alphas import BUILTIN_ALPHAS
from hokma.factory.params import ParamError, ParamSchema, validate
from hokma.factory.strategy import BadStrategy, check_strategy

KINDS = ("alpha", "strategy")
ALLOWED_KEYS = ("id", "kind", "class", "line", "description", "params")
CLASS_PREFIXES = ("hokma.factory.alphas.", "hokma.factory.strategies.")     # a YAML file never imports other code
MAX_FILE_BYTES = 64 * 1024
MAX_ENTRIES = 256
_ID = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_CLASS = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+$")
_MARKUP = re.compile(r"[<>&\x00-\x1f]")


class RegistryError(ValueError):
    """A declaration is not valid. `code` is stable; the message starts with the file and the line."""

    def __init__(self, code: str, file: str, line: int, message: str) -> None:
        self.code, self.file, self.line = code, file, line
        super().__init__(f"{file}:{line}: {message}")


@dataclass(frozen=True)
class Entry:
    id: str
    kind: str
    cls: type
    line: str | None
    description: str
    params: Mapping[str, Any]
    source: str                      # "builtin" or "file:line"


@dataclass
class Registry:
    entries: dict[str, Entry] = field(default_factory=dict)

    @classmethod
    def with_builtins(cls) -> "Registry":
        registry = cls()
        for key, klass in BUILTIN_ALPHAS.items():
            registry.add(Entry(key, "alpha", klass, None, (klass.__doc__ or "").strip().splitlines()[0] if klass.__doc__ else "",
                               klass.SCHEMA.defaults(), "builtin"))
        return registry

    def add(self, entry: Entry, file: str = "builtin", line: int = 0) -> None:
        if entry.id in self.entries:
            raise RegistryError("duplicate_id", file, line, f"id {entry.id!r} is already registered ({self.entries[entry.id].source})")
        self.entries[entry.id] = entry

    def load_dir(self, folder: str | Path) -> int:
        """Register every *.yaml file of a folder, in name order. Returns how many entries were added."""
        added = 0
        for path in sorted(Path(folder).glob("*.yaml")):
            added += self.load_file(path)
        return added

    def load_file(self, path: str | Path) -> int:
        path = Path(path)
        name = path.name
        try:
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                raise RegistryError("file_too_large", name, 1, f"the file is {size} bytes; the limit is {MAX_FILE_BYTES}")
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RegistryError("unreadable", name, 1, f"cannot read the file ({exc.strerror or exc})") from exc
        try:
            root = yaml.compose(text, Loader=yaml.SafeLoader)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            raise RegistryError("bad_yaml", name, 1 if mark is None else mark.line + 1, "not valid YAML") from exc
        if root is None:
            raise RegistryError("empty_file", name, 1, "the file declares nothing")
        nodes = root.value if isinstance(root, yaml.SequenceNode) else [root]
        if len(nodes) > MAX_ENTRIES:
            raise RegistryError("too_many_entries", name, 1, f"at most {MAX_ENTRIES} entries per file")
        count = 0
        for node in nodes:
            self.add(*_entry(node, name))
            count += 1
        return count

    def get(self, entry_id: str) -> Entry:
        try:
            return self.entries[entry_id]
        except KeyError:
            raise KeyError(f"no alpha or strategy with id {entry_id!r}") from None

    def build(self, entry_id: str, params: Mapping[str, Any] | None = None) -> Any:
        """A new instance of the class of the entry with the declared parameters overridden by `params`, validated."""
        entry = self.get(entry_id)
        values = validate(entry.cls.SCHEMA, {**entry.params, **dict(params or {})})
        return entry.cls(**values)


def _scalar(loader: yaml.SafeLoader, node: yaml.Node, name: str, file: str) -> Any:
    if not isinstance(node, yaml.ScalarNode):
        raise RegistryError("bad_value", file, node.start_mark.line + 1, f"{name} must be a single value")
    return loader.construct_object(node, deep=True)


def _entry(node: yaml.Node, file: str) -> tuple[Entry, str, int]:
    start = node.start_mark.line + 1
    if not isinstance(node, yaml.MappingNode):
        raise RegistryError("bad_entry", file, start, "an entry is a mapping of keys")
    loader = yaml.SafeLoader("")
    seen: dict[str, tuple[Any, int]] = {}
    for key_node, value_node in node.value:
        line = key_node.start_mark.line + 1
        key = _scalar(loader, key_node, "a key", file)
        if key not in ALLOWED_KEYS:
            raise RegistryError("unknown_key", file, line, f"unknown key {key!r}; allowed: {', '.join(ALLOWED_KEYS)}")
        if key in seen:
            raise RegistryError("duplicate_key", file, line, f"key {key!r} appears twice")
        if key == "params":
            if not isinstance(value_node, yaml.MappingNode):
                raise RegistryError("bad_value", file, value_node.start_mark.line + 1, "params must be a mapping")
            params = {}
            for pk, pv in value_node.value:
                pname = _scalar(loader, pk, "a parameter name", file)
                if pname in params:
                    raise RegistryError("duplicate_key", file, pk.start_mark.line + 1, f"parameter {pname!r} appears twice")
                params[pname] = _scalar(loader, pv, f"parameter {pname}", file)
            seen[key] = (params, line)
        else:
            seen[key] = (_scalar(loader, value_node, key, file), line)

    def get(key: str) -> Any:
        return seen[key][0] if key in seen else None

    entry_id, kind, dotted = get("id"), get("kind"), get("class")
    if not isinstance(entry_id, str) or not _ID.match(entry_id):
        raise RegistryError("bad_id", file, seen["id"][1] if "id" in seen else start, "id is required: lower case letters, digits and underscores")
    if entry_id.startswith("u_"):
        raise RegistryError("reserved_id", file, seen["id"][1], "an id that starts with u_ belongs to a strategy written as code: a file cannot declare it")
    if kind not in KINDS:
        raise RegistryError("bad_kind", file, seen["kind"][1] if "kind" in seen else start, f"kind is required and is one of {', '.join(KINDS)}")
    if dotted is None:
        raise RegistryError("missing_class", file, start, f"{kind} {entry_id!r} declares no class")
    klass = _resolve(dotted, kind, file, seen["class"][1])
    line_name = get("line")
    if line_name is not None and line_name not in LINES:
        raise RegistryError("bad_line", file, seen["line"][1], f"line is one of {', '.join(LINES)}, got {line_name!r}")
    description = get("description") or ""
    if not isinstance(description, str) or len(description) > 300 or _MARKUP.search(description):
        raise RegistryError("bad_description", file, seen["description"][1] if "description" in seen else start,
                            "description is plain text up to 300 characters with no markup characters")
    params = get("params") or {}
    try:
        validate(klass.SCHEMA, params)
    except ParamError as exc:
        raise RegistryError("bad_param", file, seen["params"][1], str(exc)) from None
    return Entry(entry_id, kind, klass, line_name, description, params, f"{file}:{start}"), file, seen["id"][1]


def _resolve(dotted: Any, kind: str, file: str, line: int) -> type:
    if not isinstance(dotted, str) or not _CLASS.match(dotted) or not dotted.startswith(CLASS_PREFIXES):
        raise RegistryError("bad_class", file, line, f"class must be a dotted path under {' or '.join(CLASS_PREFIXES)}")
    module_name, _, class_name = dotted.rpartition(".")
    try:
        klass = getattr(importlib.import_module(module_name), class_name)
    except (ImportError, AttributeError):
        raise RegistryError("bad_class", file, line, f"class {dotted} does not exist") from None
    if not isinstance(klass, type):
        raise RegistryError("bad_class", file, line, f"{dotted} is not a class")
    if not isinstance(getattr(klass, "SCHEMA", None), ParamSchema):
        raise RegistryError("bad_class", file, line, f"{dotted} declares no SCHEMA (ParamSchema)")
    if kind == "alpha" and not callable(getattr(klass, "signal", None)):
        raise RegistryError("bad_class", file, line, f"{dotted} is not an alpha: it has no signal method")
    if kind == "strategy":
        try:
            check_strategy(klass)
        except BadStrategy as exc:
            raise RegistryError("bad_class", file, line, str(exc)) from None
    return klass


def add_user_rules(registry: Registry, *, db: Any = None, stages: Any = ("generated", "owner_reviewed")) -> int:
    """Add the strategies written as code that are approved and intact (state generated or owner_reviewed, hash of the file equal to the base)."""
    from hokma.factory import user_rules

    return user_rules.add_user_rules(registry, db=db, stages=stages)


def factory_list(registry: Registry, *, kind: str | None = None, line: str | None = None) -> list[dict[str, Any]]:
    """The registered alphas and strategies as plain data, sorted by id. This is what the factory_list tool returns."""
    if kind is not None and kind not in KINDS:
        raise ValueError(f"kind is one of {', '.join(KINDS)}, got {kind!r}")
    rows = [{"id": e.id, "kind": e.kind, "class": f"{e.cls.__module__}.{e.cls.__name__}", "line": e.line,
             "description": e.description, "params": dict(e.params), "source": e.source,
             **({"generated": True} if e.source.startswith("user:") else {})}
            for e in registry.entries.values() if (kind is None or e.kind == kind) and (line is None or e.line == line)]
    return sorted(rows, key=lambda r: r["id"])


LITERATURE_FILE = Path(__file__).parent / "data" / "literature_edges.csv"
EDGE_CLASSES = ("equities", "bonds", "commodities", "currencies", "crypto", "derivatives", "multi_asset")


def factory_literature(*, min_t: float | None = None, asset_class: str | None = None, path: str | Path | None = None) -> list[dict[str, Any]]:
    """The published edge ideas as plain data, sorted by t statistic (highest first). `min_t` keeps the rows with t at or above it.

    The Sharpe ratios are the gross figures the source reports (basis `gross`); a net value is never derived here.
    """
    if min_t is not None and (isinstance(min_t, bool) or not isinstance(min_t, (int, float)) or not math.isfinite(min_t)):
        raise ValueError("min_t must be a finite number")
    if asset_class is not None and asset_class not in EDGE_CLASSES:
        raise ValueError(f"asset_class is one of {', '.join(EDGE_CLASSES)}, got {asset_class!r}")
    with open(path or LITERATURE_FILE, newline="", encoding="utf-8") as handle:
        rows = [{**row, "sharpe_reported": float(row["sharpe_reported"]), "t_stat": float(row["t_stat"]), "years_tested": int(row["years_tested"])}
                for row in csv.DictReader(handle)]
    kept = [r for r in rows if (min_t is None or r["t_stat"] >= min_t) and (asset_class is None or r["asset_class"] == asset_class)]
    return sorted(kept, key=lambda r: (-r["t_stat"], r["edge_id"]))

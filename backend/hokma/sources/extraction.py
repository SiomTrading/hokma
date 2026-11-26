"""The extraction sheet of a source: five components, each field with its citation (SPEC-030, A1-6).

A strategy that comes out of a source is first written as a sheet of five components: the signal logic, the entry, the exit, the sizing and the risk
controls, plus the execution style. Every field is one of three things: not specified in the source (nothing was guessed), taken from the source (with the
ids of the passages that say it) or proposed (by Hokma or by the owner, with the reason). A field of the source with no real passage behind it is refused, and
so is a proposal that is shown as if the source said it.

Each component can name the piece of the factory that does it (the "map": an alpha, a sizer, an exit rule, an execution style, the filters and controls). A rule that
no registered piece does goes to `needs_new_rule`, with its citation, and that component has no map: nothing is approximated with another rule. A sheet with
a rule pending never links to a saved spec.

When the source belongs to a third party the rule is written in the words of the agent: a field that repeats 12 or more words in a row of a passage is
refused. The sheet is saved in the bank; the text of the source stays in its own table.
"""
from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from hokma.db import Database, get_database
from hokma.desks.core.hashing import content_hash
from hokma.desks.core.signal import LINES
from hokma.factory import parts as factory_parts
from hokma.factory.params import ParamError, validate
from hokma.factory.registry import Registry
from hokma.factory.strategy import BadStrategy
from hokma.sources.ingest import SourceError

SCHEMA = 1
ORIGINS = ("source", "proposed", "owner")
NOT_SPECIFIED = "not_specified"
MAX_BYTES = 64 * 1024
MAX_SUMMARY = 2_000
MAX_VALUE_TEXT = 1_000
MAX_CITES = 50
MAX_NEEDS = 20
COPY_WORDS = 12
WHY_RANGE = (10, 400)
TOP_KEYS = ("schema", "summary", "line", "instruments", "timeframe", "signal_logic", "entry", "exit", "sizing", "risk_controls", "execution", "needs_new_rule")
LEAF_KEYS = {"signal_logic": ("kind", "rule", "inputs", "lookback"), "entry": ("trigger", "direction", "filters", "timing"),
             "exit": ("stop", "target", "time_exit", "reversal", "trailing"), "sizing": ("method",),
             "risk_controls": ("max_trades_per_day", "flat_by_time", "max_daily_loss")}
LEAVES: tuple[str, ...] = ("instruments", "timeframe", *(f"{c}.{k}" for c, keys in LEAF_KEYS.items() for k in keys), "execution")
MUST_BE_STATED = ("signal_logic.rule", "entry.trigger", "exit.stop", "sizing.method")      # if the source is silent the agent proposes them
MAP_KEYS = ("signal_logic", "exit", "sizing", "risk_controls", "execution")                  # the components that name a piece of the factory
PENDING_COMPONENTS = ("signal_logic", "entry.trigger", "entry.direction", "entry.timing", "entry.filters", "exit", "sizing", "risk_controls", "execution")
PENDING_COVERS = {"signal_logic": "signal_logic", "entry.trigger": "signal_logic", "exit": "exit", "sizing": "sizing", "risk_controls": "risk_controls",
                  "execution": "execution"}
PIECE_KEY = {"exit": "exit", "sizing": "sizer", "execution": "execution"}
_WORD = re.compile(r"\w+", re.UNICODE)


class ExtractionError(SourceError):
    """The sheet is not valid. `code` is bad_input, or no_data when the source does not exist."""


def _bad(message: str, suggestions: list[str] | None = None) -> ExtractionError:
    return ExtractionError("bad_input", message, suggestions)


@dataclass(frozen=True)
class Assessment:
    complete: bool
    missing: list[str]
    needs_new_rule: list[dict[str, Any]]
    proposed: list[str]


@dataclass(frozen=True)
class Extraction:
    data: dict[str, Any]
    hash: str
    line: str | None
    assessment: Assessment
    citations_checked: int = 0
    registry: Registry | None = field(default=None, compare=False, repr=False)

    @property
    def complete(self) -> bool:
        return self.assessment.complete

    @property
    def status(self) -> str:
        """The state a strategy has with this sheet: draft when it is complete, idea when it is not."""
        return "draft" if self.complete else "idea"

    @property
    def mapped(self) -> bool:
        """Every component names its piece: nothing is pending and nothing is missing from the maps. Only then can a saved spec stand for the sheet."""
        return not self.assessment.needs_new_rule and all(_map_of(self.data, key) is not None for key in MAP_KEYS)


# ---------------------------------------------------------------------------------------------------- reading the sheet
def _get(data: Mapping[str, Any], path: str) -> Any:
    node: Any = data
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _map_of(data: Mapping[str, Any], key: str) -> Any:
    return _get(data, "execution.map" if key == "execution" else f"{key}.map")


def _is_blank(leaf: Any) -> bool:
    return isinstance(leaf, Mapping) and leaf.get("value") == NOT_SPECIFIED and "origin" not in leaf


def assess(data: Mapping[str, Any]) -> Assessment:
    """What a sheet lacks, what it still needs a new rule for and what the agent proposed. Pure: it does not look at the database."""
    needs = [dict(n) for n in data.get("needs_new_rule") or []]
    covered = {PENDING_COVERS[n["component"]] for n in needs if n.get("component") in PENDING_COVERS}
    missing: list[str] = []
    proposed: list[str] = []
    for path in LEAVES:
        leaf = _get(data, path)
        if not isinstance(leaf, Mapping):
            missing.append(path)
            continue
        if _is_blank(leaf) and path in MUST_BE_STATED:
            missing.append(path)
        if leaf.get("origin") == "proposed":
            proposed.append(path)
    for key in MAP_KEYS:
        if key not in covered and _map_of(data, key) is None:
            missing.append("execution.map" if key == "execution" else f"{key}.map")
    return Assessment(not missing, missing, needs, proposed)


# ---------------------------------------------------------------------------------------------------- checking the shape
def _only(node: Any, allowed: tuple[str, ...], where: str) -> dict[str, Any]:
    if not isinstance(node, Mapping):
        raise _bad(f"{where} is an object")
    extra = sorted(set(node) - set(allowed))
    if extra:
        raise _bad(f"{where} has the keys {', '.join(allowed)}, not {extra[0]!r}")
    return dict(node)


def _json_value(value: Any, where: str, depth: int = 0) -> None:
    if depth > 6:
        raise _bad(f"{where} is nested too deep")
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        if isinstance(value, float) and value != value:
            raise _bad(f"{where} holds a number that is not a number")
        return
    if isinstance(value, str):
        if len(value) > MAX_VALUE_TEXT:
            raise _bad(f"{where} holds text of more than {MAX_VALUE_TEXT} characters")
        return
    if isinstance(value, list):
        if len(value) > 50:
            raise _bad(f"{where} holds a list of more than 50 items")
        for item in value:
            _json_value(item, where, depth + 1)
        return
    if isinstance(value, Mapping):
        if len(value) > 50:
            raise _bad(f"{where} holds an object of more than 50 keys")
        for key, item in value.items():
            if not isinstance(key, str):
                raise _bad(f"{where} holds a key that is not text")
            _json_value(item, where, depth + 1)
        return
    raise _bad(f"{where} holds a value that is not plain data")


def _cites(raw: Any, where: str) -> list[int]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_CITES or any(isinstance(c, bool) or not isinstance(c, int) or c < 1 for c in raw):
        raise _bad(f"{where}: cite is a list of 1 to {MAX_CITES} passage ids")
    return raw


def _leaf(node: Any, where: str, *, extra: tuple[str, ...] = ()) -> dict[str, Any]:
    """One field: not specified, from the source with its passage ids, or proposed (by the agent or the owner) with the reason."""
    if not isinstance(node, Mapping) or "value" not in node:
        raise _bad(f"{where} is an object with a value: {{\"value\": \"not_specified\"}}, or a value with its origin and its cite or its why")
    if "origin" not in node:
        _only(node, ("value", *extra), where)
        if node["value"] != NOT_SPECIFIED:
            raise _bad(f"{where} has a value and no origin: say where it comes from (source, proposed or owner)")
        return dict(node)
    origin = node["origin"]
    if origin not in ORIGINS:
        raise _bad(f"{where}: origin is one of {', '.join(ORIGINS)}")
    if origin == "source":
        out = _only(node, ("value", "origin", "cite", *extra), where)
        _cites(out.get("cite"), where)
    else:
        out = _only(node, ("value", "origin", "why", *extra), where)
        why = out.get("why")
        if not isinstance(why, str) or not WHY_RANGE[0] <= len(why.strip()) <= WHY_RANGE[1]:
            raise _bad(f"{where}: a {origin} value needs a why of {WHY_RANGE[0]} to {WHY_RANGE[1]} characters, so it is never taken for the source")
    if out["value"] == NOT_SPECIFIED:
        raise _bad(f"{where}: a field with an origin has a value; use {{\"value\": \"not_specified\"}} when the source does not say it")
    _json_value(out["value"], where)
    return out


def _cited_ids(data: Mapping[str, Any]) -> set[int]:
    found: set[int] = set()

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            if isinstance(node.get("cite"), list):
                found.update(c for c in node["cite"] if isinstance(c, int))
            for key, item in node.items():
                if key != "cite":
                    walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return found


def _texts(data: Mapping[str, Any]) -> list[tuple[str, str]]:
    """The words the agent wrote: the summary, the text of each field and of each reason, and the rule of each pending piece."""
    out: list[tuple[str, str]] = [("summary", data.get("summary", ""))]

    def strings(node: Any):
        if isinstance(node, str):
            yield node
        elif isinstance(node, list):
            for item in node:
                yield from strings(item)
        elif isinstance(node, Mapping):
            for item in node.values():
                yield from strings(item)

    def leaves(prefix: str, node: Any) -> None:
        if isinstance(node, Mapping) and "value" in node:
            for text in strings(node["value"]):
                out.append((prefix, text))
            if isinstance(node.get("why"), str):
                out.append((f"{prefix}.why", node["why"]))
        elif isinstance(node, Mapping):
            for key, item in node.items():
                if key not in ("map", "needs_new_rule"):
                    leaves(f"{prefix}.{key}" if prefix else key, item)

    for key in ("instruments", "timeframe", "signal_logic", "entry", "exit", "sizing", "risk_controls", "execution"):
        if key in data:
            leaves(key, data[key])
    for n, item in enumerate(data.get("needs_new_rule") or []):
        out.append((f"needs_new_rule[{n}]", item.get("rule", "")))
    return out


def _words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text)]


def _grams(words: list[str]) -> set[tuple[str, ...]]:
    return {tuple(words[i:i + COPY_WORDS]) for i in range(len(words) - COPY_WORDS + 1)}


def _check_not_copied(data: Mapping[str, Any], passages: list[str], title: str | None = None) -> None:
    """A rule is written in the agent's words: 12 or more words in a row of a passage, anywhere in the sheet, are refused."""
    candidates = [(where, _grams(_words(text))) for where, text in [*_texts(data), *([("title", title)] if isinstance(title, str) else [])]]
    candidates = [(where, grams) for where, grams in candidates if grams]
    if not candidates:
        return
    wanted = set().union(*(g for _w, g in candidates))
    for body in passages:
        found = wanted & _grams(_words(body))
        if found:
            where = next(w for w, g in candidates if g & found)
            raise _bad(f"{where}: a rule is written in your words; the source text is not copied ({COPY_WORDS} or more words in a row repeat a passage)",
                       ["Say the rule again in your own words and keep the passage id in cite."])


# ---------------------------------------------------------------------------------------------------- the maps
def _piece(parts: dict[str, Any], where: str) -> dict[str, Any]:
    try:
        return factory_parts.validate_parts(parts)
    except BadStrategy as exc:
        raise _bad(f"{where}: {exc}", ["A rule that no registered piece does goes to needs_new_rule, with its cite; it is not approximated."]) from None


def _alpha_map(raw: Any, registry: Registry) -> dict[str, Any]:
    node = _only(raw, ("alpha", "params"), "signal_logic.map")
    name = node.get("alpha")
    if not isinstance(name, str) or name not in registry.entries or registry.entries[name].kind != "alpha":
        raise _bad(f"signal_logic.map: {name!r} is not an alpha of the registry", ["List the registry with factory_list; a rule that no alpha does goes to needs_new_rule."])
    entry = registry.entries[name]
    params = node.get("params", {})
    if not isinstance(params, Mapping):
        raise _bad("signal_logic.map.params is an object")
    try:
        values = validate(entry.cls.SCHEMA, {**entry.params, **dict(params)})
        entry.cls(**values)
    except (ParamError, ValueError, TypeError) as exc:
        raise _bad(f"signal_logic.map: {exc}") from None
    return {"alpha": name, "params": dict(params)}


def filters_of(data: Mapping[str, Any]) -> list[Any]:
    """The filters and controls of the sheet as one list: the ones of the entry and the ones of the risk controls."""
    raw = _get(data, "entry.filters")
    entry = raw.get("value") if isinstance(raw, Mapping) and isinstance(raw.get("value"), list) else []
    controls = _get(data, "risk_controls.map")
    return [*entry, *(controls if isinstance(controls, list) else [])]


def plan_of(data: Mapping[str, Any], registry: Registry) -> dict[str, Any]:
    """The mapped strategy as the factory would read it: the alpha with its parameters filled in and the parts with their defaults. A saved spec stands for
    the sheet only when it has this same plan."""
    alpha = _map_of(data, "signal_logic")
    entry = registry.entries[alpha["alpha"]]
    parts: dict[str, Any] = {"sizer": _map_of(data, "sizing"), "exit": _map_of(data, "exit"), "execution": _map_of(data, "execution")}
    chosen = _piece({k: v for k, v in parts.items() if v is not None}, "map")
    filters = filters_of(data)
    if filters:
        chosen["filters"] = _piece({"filters": filters}, "filters")["filters"]
    full = factory_parts.check_parts(chosen)
    plan = {"alpha": alpha["alpha"], "alpha_params": validate(entry.cls.SCHEMA, {**entry.params, **dict(alpha.get("params") or {})}),
            "parts": {k: dict(v) for k, v in full.items() if k != "filters"}}
    plan["filters"] = sorted((json.dumps(f, sort_keys=True) for f in full.get("filters", ())))
    return json.loads(json.dumps(plan, sort_keys=True, default=str))


def _check_maps(data: Mapping[str, Any], registry: Registry) -> None:
    alpha = _map_of(data, "signal_logic")
    if alpha is not None:
        _alpha_map(alpha, registry)
    for key, part in PIECE_KEY.items():
        node = _map_of(data, key)
        if node is not None:
            if not isinstance(node, Mapping) or not isinstance(node.get("kind"), str):
                raise _bad(f"{key}.map is an object with a kind")
            _piece({part: node}, f"{key}.map")
    controls = _map_of(data, "risk_controls")
    if controls is not None and (not isinstance(controls, list) or any(not isinstance(c, Mapping) for c in controls)):
        raise _bad("risk_controls.map is a list of controls, each an object with a kind")
    entry_filters = _get(data, "entry.filters")
    if isinstance(entry_filters, Mapping) and "origin" in entry_filters and not isinstance(entry_filters.get("value"), list):
        raise _bad("entry.filters is a list of filters of the factory; a filter that no piece does goes to needs_new_rule with the component entry.filters",
                   ["List the pieces with factory_list."])
    filters = filters_of(data)
    if filters:
        _piece({"filters": filters}, "the filters and controls")


def _needs(raw: Any, data: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) > MAX_NEEDS:
        raise _bad(f"needs_new_rule is a list of at most {MAX_NEEDS} items")
    out = []
    for n, item in enumerate(raw):
        node = _only(item, ("component", "rule", "cite"), f"needs_new_rule[{n}]")
        if node.get("component") not in PENDING_COMPONENTS:
            raise _bad(f"needs_new_rule[{n}]: component is one of {', '.join(PENDING_COMPONENTS)}")
        rule = node.get("rule")
        if not isinstance(rule, str) or not 10 <= len(rule.strip()) <= 400:
            raise _bad(f"needs_new_rule[{n}]: rule is the rule in your words, 10 to 400 characters")
        _cites(node.get("cite"), f"needs_new_rule[{n}]")
        key = PENDING_COVERS.get(node["component"])
        if key is not None and _map_of(data, key) is not None:
            raise _bad(f"needs_new_rule[{n}]: the component {node['component']} has a rule pending, so it has no map: nothing is approximated with another rule")
        out.append(node)
    return out


def check_shape(ext: Any, registry: Registry) -> dict[str, Any]:
    """The sheet checked without the database: its shape, its fields, its maps and the pending rules. Returns a copy."""
    if not isinstance(ext, Mapping):
        raise _bad("extraction is an object")
    try:
        text = json.dumps(ext, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        raise _bad("extraction holds a value that is not plain data") from None
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise _bad(f"extraction is at most {MAX_BYTES // 1024} KB")
    data = _only(copy.deepcopy(dict(ext)), TOP_KEYS, "extraction")
    if data.get("schema") != SCHEMA:
        raise _bad(f"schema is {SCHEMA}")
    summary = data.get("summary")
    if not isinstance(summary, str) or not 1 <= len(summary.strip()) <= MAX_SUMMARY:
        raise _bad(f"summary is text of 1 to {MAX_SUMMARY} characters")
    if data.get("line") is not None and data["line"] not in LINES:
        raise _bad(f"line is one of {', '.join(LINES)}")
    for key in ("instruments", "timeframe"):
        if key in data:
            data[key] = _leaf(data[key], key)
    if "execution" in data:
        data["execution"] = _leaf(data["execution"], "execution", extra=("map",))
    for component, keys in LEAF_KEYS.items():
        if component not in data:
            continue
        node = _only(data[component], (*keys, "map") if component != "entry" else keys, component)
        for key in keys:
            if key in node:
                node[key] = _leaf(node[key], f"{component}.{key}")
        data[component] = node
    pending = _needs(data.get("needs_new_rule", []), data)
    data["needs_new_rule"] = pending
    _check_maps(data, registry)
    return data


# ---------------------------------------------------------------------------------------------------- against the database
def _source(session: Any, source_id: Any) -> dict[str, Any]:
    if isinstance(source_id, bool) or not isinstance(source_id, int) or source_id < 1:
        raise _bad("source_id is a whole number")
    row = session.one("SELECT id, rights FROM strategy_sources WHERE id = :i", {"i": source_id})
    if row is None:
        raise ExtractionError("no_data", f"there is no source {source_id}")
    return row


def validate_extraction(ext: Any, source_id: int, *, registry: Registry | None = None, db: Database | None = None, title: str | None = None) -> Extraction:
    """The sheet of `source_id` checked in full: its shape, every passage id it cites (it must exist and belong to this source), the proposals with their
    reason, the maps against the registry and, for a source that is not the owner's, that no rule repeats the text of the source."""
    from hokma.factory.spec import default_registry

    registry = registry or default_registry()
    data = check_shape(ext, registry)
    with (db or get_database()).read() as u:
        source = _source(u.session, source_id)
        ids = {int(r["id"]) for r in u.session.query("SELECT id FROM strategy_source_passages WHERE source_id = :s", {"s": source_id})}
        cited = _cited_ids(data)
        stray = sorted(cited - ids)
        if stray:
            raise _bad(f"the passage {stray[0]} is not a passage of source {source_id}: a field of the source cites a real passage of it",
                       ["Read the source again with source_ingest (action read) and cite the ids it gives."])
        if source["rights"] != "own":
            bodies = [r["body"] for r in u.session.query("SELECT body FROM strategy_source_passages WHERE source_id = :s ORDER BY ord", {"s": source_id})]
        else:
            bodies = []
    if bodies:
        _check_not_copied(data, bodies, title)
    return Extraction(data, content_hash(data), data.get("line"), assess(data), len(cited), registry)

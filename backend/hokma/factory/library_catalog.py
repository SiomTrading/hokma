"""The shared library (SPEC-032: C7): one list of what a strategy can use, read the same by the optimizer, by Ian and by the screen.

It is not a copy. `catalog` reads the registries that exist: the indicator library of the factory (`LIBRARY` and its `WIRING`), the indicators of
H3 that the factory does not hand out yet (`h3_only`), the registry of alphas, and the parts (`SIZERS`, `EXITS`, `EXECUTORS` and `FILTERS` when the
module has them). A request for an indicator that is missing is a row of `library_requests`. Nothing here calculates anything and nothing here
reaches a broker.
"""
from __future__ import annotations

import inspect
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from hokma.db import Database, get_database
from hokma.factory import indicator_library, parts
from hokma.factory.params import ParamSchema
from hokma.quant import registry as quant_registry

KINDS = ("indicator", "alpha", "sizer", "exit", "execution", "filter")
STATUSES = ("ready", "h3_only", "requested")
REQUEST_STATUSES = ("open", "wired", "built", "user_rule", "declined")
BAR_FIELDS = ("open", "high", "low", "close", "volume")
MAX_NAME = 48
MIN_DESCRIPTION, MAX_DESCRIPTION = 20, 2000
MAX_CALLER = 80
MAX_PARAMS_CHARS = 20_000
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_FIRST_SOURCES = {"ema": ("indicators.ema", ("close",)), "sma": ("indicators.sma", ("close",)), "rsi": ("indicators.rsi", ("close",)),
                  "macd_line": ("indicators.macd", ("close",)), "macd_signal": ("indicators.macd", ("close",)),
                  "adx": ("indicators.adx", ("high", "low", "close")), "atr": ("indicators.atr", ("high", "low", "close")),
                  "half_life": ("stats.half_life", ("close",)), "ewma_vol": ("risk.ewma_volatility", ("close",))}      # where each of the first nine comes from


class LibraryError(ValueError):
    """A call is not valid. `code` is stable: bad_input. `item` is the card of the thing that already exists, when that is the reason."""

    code = "bad_input"

    def __init__(self, message: str, item: "LibraryItem | None" = None) -> None:
        super().__init__(message)
        self.item = item


@dataclass(frozen=True)
class LibraryItem:
    name: str
    kind: Literal["indicator", "alpha", "sizer", "exit", "execution", "filter"]
    status: Literal["ready", "h3_only", "requested"]
    params: tuple[Mapping[str, Any], ...]     # name, type, low, high, default when there is one
    fields: tuple[str, ...]                   # bar fields it reads (indicators)
    description: str
    source: str                               # "indicator_library", "quant.indicators", "factory.registry", "parts.SIZERS", ...

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "status": self.status, "params": [dict(p) for p in self.params], "fields": list(self.fields),
                "description": self.description, "source": self.source}


def _param(name: str, kind: str, *, low: Any = None, high: Any = None, default: Any = None, choices: Sequence[str] = ()) -> dict[str, Any]:
    out: dict[str, Any] = {"name": name, "type": kind}
    for key, value in (("low", low), ("high", high), ("default", default)):
        if value is not None:
            out[key] = value
    if choices:
        out["choices"] = list(choices)
    return out


def _schema_params(schema: ParamSchema) -> tuple[dict[str, Any], ...]:
    return tuple(_param(p.name, p.kind, low=p.low, high=p.high, default=p.default, choices=p.choices) for p in schema.params)


def _first_nine() -> list[LibraryItem]:
    out = []
    for name, (h3, fields) in _FIRST_SOURCES.items():
        fn = indicator_library.LIBRARY[name]
        params = tuple(_param(p.name, "int", default=None if p.default is p.empty else p.default) for p in list(inspect.signature(fn).parameters.values())[1:])
        doc = (fn.__doc__ or "").strip().splitlines()[0] if fn.__doc__ else ""
        out.append(LibraryItem(name, "indicator", "ready", params, fields, doc or quant_registry.describe(h3).summary, "indicator_library"))
    return out


def _wired() -> list[LibraryItem]:
    out = []
    for w in indicator_library.WIRING:
        spec = quant_registry.describe(w.h3)
        params = tuple(_param(lib_name, "int", default=spec.inputs[h3_name].get("default")) for lib_name, h3_name in w.params.items())
        out.append(LibraryItem(w.name, "indicator", "ready", params, w.fields, f"{w.column}: {spec.summary}", "indicator_library"))
    return out


def _h3_only() -> list[LibraryItem]:
    used = {h3 for h3, _fields in _FIRST_SOURCES.values()} | {w.h3 for w in indicator_library.WIRING}
    out = []
    for spec in quant_registry.list(module="indicators"):
        if spec.name in used:
            continue
        params = tuple(_param(k, str(v.get("type", "")), default=v.get("default")) for k, v in spec.inputs.items()
                       if isinstance(v, dict) and k not in ("bars", "line", "last_n"))
        out.append(LibraryItem(spec.name.removeprefix("indicators."), "indicator", "h3_only", params, (), spec.summary, "quant.indicators"))
    return out


def _alphas() -> list[LibraryItem]:
    from hokma.factory import registry as factory_registry
    from hokma.factory.spec import default_registry

    registry = default_registry()
    return [LibraryItem(row["id"], "alpha", "ready", _schema_params(registry.get(row["id"]).cls.SCHEMA), (), row["description"], "factory.registry")
            for row in factory_registry.factory_list(registry, kind="alpha")]


def _parts() -> list[LibraryItem]:
    out = []
    for kind, table, label in (("sizer", parts.SIZERS, "SIZERS"), ("exit", parts.EXITS, "EXITS"), ("execution", parts.EXECUTORS, "EXECUTORS"),
                               ("filter", getattr(parts, "FILTERS", {}), "FILTERS")):
        for name, part in sorted(table.items()):
            out.append(LibraryItem(name, kind, "ready", _schema_params(part.schema), (), part.summary, f"parts.{label}"))     # type: ignore[arg-type]
    return out


def _requested(db: Database | None) -> list[LibraryItem]:
    out = []
    for row in requests(status="open", db=db):
        params = tuple(_param(str(k), type(v).__name__, default=v) for k, v in row["params"].items())
        out.append(LibraryItem(row["name"], "indicator", "requested", params, tuple(row["fields"]), row["description"], "library_requests"))
    return out


def catalog(*, kind: str | None = None, status: str | None = None, db: Database | None = None) -> list[LibraryItem]:
    """What a strategy can use, sorted by kind and name. `ready` can be used now; `h3_only` exists in H3 and the factory does not hand it out yet;
    `requested` is an open request. The parts of `parts.FILTERS` are listed when the module has them."""
    if kind is not None and kind not in KINDS:
        raise LibraryError(f"kind is one of {', '.join(KINDS)}")
    if status is not None and status not in STATUSES:
        raise LibraryError(f"status is one of {', '.join(STATUSES)}")
    quant_registry.load_all()
    items = [*_first_nine(), *_wired(), *_h3_only(), *_alphas(), *_parts(), *_requested(db)]
    return sorted((i for i in items if (kind is None or i.kind == kind) and (status is None or i.status == status)), key=lambda i: (KINDS.index(i.kind), i.name))


def _notice_item(name: str) -> LibraryItem | None:
    """The card of an indicator that already exists under this name in the library (ready) or in H3 (h3_only), or None."""
    quant_registry.load_all()
    for item in (*_first_nine(), *_wired(), *_h3_only()):
        if item.name == name:
            return item
    return None


def request_notice(name: str) -> str | None:
    """What the caller must be told when a request is made for `name`: the indicator exists in H3 and only lacks its wiring. None otherwise."""
    item = _notice_item(name)
    if item is not None and item.status == "h3_only":
        return f"{name} exists in H3 (quant.indicators): it only needs to be wired into the library; the request is saved"
    return None


def _text(label: str, value: Any, low: int, high: int) -> str:
    if not isinstance(value, str) or _CONTROL.search(value.replace("\n", " ")) or not low <= len(value.strip()) <= high:
        raise LibraryError(f"{label} is text of {low} to {high} characters")
    return value


def request(name: str, *, description: str, fields: Sequence[str], params: Mapping[str, Any], bank_version_id: int | None, caller: str,
            db: Database | None = None) -> int:
    """Ask for an indicator the library does not have and return the id of the request (status open).

    A name that is already `ready` is `bad_input` and the error carries its card. A name that is `h3_only` is saved: `request_notice` gives the
    sentence that says it exists in H3 and only needs wiring. A `bank_version_id` that is not in `strategy_bank_versions` is `bad_input` when that table
    exists. A request is changed to wired, built, user_rule or declined by the owner or by the code of the version that resolves it, never by an agent."""
    if not isinstance(name, str) or not 1 <= len(name) <= MAX_NAME or _CONTROL.search(name):
        raise LibraryError(f"name is text of 1 to {MAX_NAME} characters")
    _text("description", description, MIN_DESCRIPTION, MAX_DESCRIPTION)
    _text("caller", caller, 1, MAX_CALLER)
    if isinstance(fields, (str, bytes)) or not isinstance(fields, Sequence) or any(f not in BAR_FIELDS for f in fields) or len(set(fields)) != len(fields):
        raise LibraryError(f"fields is a list of bar fields without repeats: {', '.join(BAR_FIELDS)}")
    if not isinstance(params, Mapping) or any(not isinstance(k, str) or not 1 <= len(k) <= MAX_NAME for k in params):
        raise LibraryError("params is a mapping of parameter names to values")
    try:
        params_text = json.dumps(dict(params), sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise LibraryError("the values of params are numbers, text, true, false or none") from None
    if len(params_text) > MAX_PARAMS_CHARS:
        raise LibraryError(f"params is at most {MAX_PARAMS_CHARS} characters")
    if bank_version_id is not None and (isinstance(bank_version_id, bool) or not isinstance(bank_version_id, int) or bank_version_id < 1):
        raise LibraryError("bank_version_id is a whole number from 1, or none")
    existing = _notice_item(name)
    if existing is not None and existing.status == "ready":
        raise LibraryError(f"{name} already exists in the library ({existing.source}); use it as it is", existing)
    with (db or get_database()).write() as u:
        if bank_version_id is not None and u.session.table_exists("strategy_bank_versions"):
            if u.session.one("SELECT 1 AS found FROM strategy_bank_versions WHERE id = :i", {"i": bank_version_id}) is None:
                raise LibraryError(f"bank_version_id {bank_version_id} does not exist")
        return u.session.insert("library_requests", {"name": name, "description": description, "fields_json": json.dumps(list(fields)),
                                                     "params_json": params_text, "bank_version_id": bank_version_id, "requested_by": caller})


def requests(*, status: str | None = None, db: Database | None = None) -> list[dict[str, Any]]:
    """The requests for an indicator, oldest first. Empty when the table does not exist (the migration of the verdict is not applied)."""
    if status is not None and status not in REQUEST_STATUSES:
        raise LibraryError(f"status is one of {', '.join(REQUEST_STATUSES)}")
    sql, params = ("SELECT id, name, description, fields_json, params_json, bank_version_id, status, resolution, requested_by, created_at, updated_at "
                   "FROM library_requests"), {}
    if status is not None:
        sql, params = sql + " WHERE status = :s", {"s": status}
    with (db or get_database()).read() as u:
        if not u.session.table_exists("library_requests"):
            return []
        rows = u.session.query(sql + " ORDER BY id", params)
    return [{**{k: r[k] for k in ("id", "name", "description", "bank_version_id", "status", "resolution", "requested_by", "created_at", "updated_at")},
             "fields": json.loads(r["fields_json"]), "params": json.loads(r["params_json"])} for r in rows]

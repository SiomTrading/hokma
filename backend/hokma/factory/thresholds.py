"""The editable table of limits of the audit, one versioned set per line (SPEC-032: 4.1 and section 6).

The 13 measures of `MEASURES` are the only keys a set holds. A set is saved as a new version of its line and is never changed: `audit_threshold_sets`
accepts inserts only. Version 1 of DT, SWC and SWO comes from the migration (changed_by 'system:seed'); LP and RF have none until the owner saves one.

`to_config` builds the `AuditConfig` of `factory.audit`, and its `__post_init__` is the last check of every save: a set that the audit would refuse is never
written. `edge_warns` is not a field of `AuditConfig`: the evidence bridge reads it from the set to decide whether the border of the grid is reported.
No calculation lives here: the values are limits that `factory.audit` compares, nothing is derived from a result. The actor of a change comes from the code
that calls (the route puts 'owner'), never from the body of a request.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from hokma.db import Database, get_database
from hokma.db.timeutil import from_db_time
from hokma.factory.audit import AuditConfig, PboThresholds
from hokma.quant.errors import BadInput

LINES = ("DT", "SWC", "SWO", "LP", "RF")
MAX_REASON = 500
_ACTOR = re.compile(r"^[a-z][a-z0-9_:-]{0,79}$")


class ThresholdError(ValueError):
    """The set cannot be read or saved. `code` is stable: bad_input (a value, a key, a line or an actor is wrong) or no_thresholds (the line has no set)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class Measure:
    key: str                       # section 6
    kind: Literal["share", "number", "years", "count", "toggle"]
    low: float | None
    high: float | None
    step: float | None
    effect: Literal["yellow", "red", "yellow_and_red", "setting"]
    checks: tuple[int, ...]        # numbers of CHECKLIST it decides


# The 13 rows of section 6, M1 to M13, in that order. The ranges and steps are the ones of the editor.
MEASURES: tuple[Measure, ...] = (
    Measure("pbo_warn", "share", 0.05, 0.95, 0.05, "yellow", (19,)),
    Measure("pbo_reject", "share", 0.05, 0.95, 0.05, "red", (19,)),
    Measure("min_dsr", "number", 0.50, 0.99, 0.01, "red", (18,)),
    Measure("min_oos_to_is", "number", 0.10, 1.00, 0.05, "red", (22,)),
    Measure("max_cost_share", "share", 0.05, 1.00, 0.01, "red", (10,)),
    Measure("edge_warns", "toggle", None, None, None, "yellow", (20,)),
    Measure("neighbor_spread", "share", 0.05, 0.50, 0.05, "setting", (20,)),
    Measure("max_neighbor_drop", "share", 0.05, 1.00, 0.05, "red", (20,)),
    Measure("red_flag_sharpe", "number", 1.0, 10.0, 0.1, "red", (22,)),
    Measure("max_top_trade_share", "share", 0.05, 1.00, 0.05, "red", (22,)),
    Measure("min_years", "years", 0.5, 30.0, 0.5, "red", (9,)),
    Measure("min_trades", "count", 10, 10_000, 10, "red", (9,)),
    Measure("min_regime_share", "share", 0.10, 1.00, 0.05, "red", (21,)),
)
_BY_KEY = {m.key: m for m in MEASURES}


@dataclass(frozen=True)
class ThresholdSet:
    id: int
    line: str
    version: int
    values: Mapping[str, float | bool]
    changed_by: str
    reason: str | None
    created_at: datetime
    restored_from: int | None = None
    unchanged: bool = False        # True on the set that `save`, `restore` or `copy_line` returns when nothing new was written


# ---- values ---------------------------------------------------------------------------------------------------------------------------

def _decimal(value: float) -> Decimal:
    return Decimal(repr(float(value))) if not isinstance(value, Decimal) else value


def _on_the_grid(measure: Measure, value: float) -> bool:
    """True when value is low plus a whole number of steps (decimal arithmetic: 0.35 is on the grid of 0.05, 0.36 is not)."""
    try:
        offset = (_decimal(value) - _decimal(measure.low)) / _decimal(measure.step)    # type: ignore[arg-type]
    except (InvalidOperation, ZeroDivisionError):
        return False
    return offset == offset.to_integral_value()


def _clean(measure: Measure, value: Any) -> float | int | bool:
    key = measure.key
    if measure.kind == "toggle":
        if not isinstance(value, bool):
            raise ThresholdError("bad_input", f"{key} is on or off (true or false)")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ThresholdError("bad_input", f"{key} is a finite number")
    if measure.kind == "count":
        if float(value) != int(value):
            raise ThresholdError("bad_input", f"{key} is a whole number")
        value = int(value)
    else:
        value = float(value)
    if not measure.low <= value <= measure.high:                                      # type: ignore[operator]
        raise ThresholdError("bad_input", f"{key} is between {measure.low:g} and {measure.high:g}, got {value:g}")
    if not _on_the_grid(measure, value):
        raise ThresholdError("bad_input", f"{key} moves in steps of {measure.step:g} from {measure.low:g}, got {value:g}")
    return value


def _normalize(values: Any) -> dict[str, float | int | bool]:
    """The values of a set in their canonical types, or ThresholdError: no key more, no key less, each one inside its range and step."""
    if not isinstance(values, Mapping):
        raise ThresholdError("bad_input", "values is a mapping of the 13 measures")
    extra = sorted(str(k) for k in values if k not in _BY_KEY)
    missing = [m.key for m in MEASURES if m.key not in values]
    if extra or missing:
        raise ThresholdError("bad_input", "; ".join(part for part in (f"unknown measures: {', '.join(extra)}" if extra else "",
                                                                      f"missing measures: {', '.join(missing)}" if missing else "") if part))
    return {m.key: _clean(m, values[m.key]) for m in MEASURES}


def _canonical(values: Mapping[str, Any]) -> str:
    return json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def to_config(values: Mapping[str, Any]) -> AuditConfig:
    """The `AuditConfig` of a set of values. The values are checked first (keys, ranges, steps) and then by `AuditConfig` itself: `bad_input` when either refuses."""
    clean = _normalize(values)
    try:
        return AuditConfig(pbo=PboThresholds(clean["pbo_warn"], clean["pbo_reject"]), min_years=clean["min_years"], min_trades=clean["min_trades"],
                           min_dsr=clean["min_dsr"], max_neighbor_drop=clean["max_neighbor_drop"], neighbor_spread=clean["neighbor_spread"],
                           min_regime_share=clean["min_regime_share"], red_flag_sharpe=clean["red_flag_sharpe"],
                           max_top_trade_share=clean["max_top_trade_share"], min_oos_to_is=clean["min_oos_to_is"], max_cost_share=clean["max_cost_share"])
    except BadInput as exc:
        raise ThresholdError("bad_input", str(exc)) from exc


# ---- reading --------------------------------------------------------------------------------------------------------------------------

def _line(line: Any) -> str:
    if not isinstance(line, str) or line not in LINES:
        raise ThresholdError("bad_input", f"line is one of {', '.join(LINES)}")
    return line


def _actor(actor: Any) -> str:
    if not isinstance(actor, str) or not _ACTOR.match(actor):
        raise ThresholdError("bad_input", "actor is a lowercase name given by the code that calls")
    return actor


def _reason(reason: Any) -> str | None:
    if reason is None:
        return None
    if not isinstance(reason, str) or len(reason.strip()) > MAX_REASON:
        raise ThresholdError("bad_input", f"reason is text of at most {MAX_REASON} characters")
    return reason.strip() or None


def _when(value: Any) -> datetime:
    return from_db_time(value) if isinstance(value, str) else value


def _set(row: Mapping[str, Any]) -> ThresholdSet:
    return ThresholdSet(int(row["id"]), row["line"], int(row["version"]), json.loads(row["values_json"]), row["changed_by"], row["reason"],
                        _when(row["created_at"]), row["restored_from"] if row["restored_from"] is None else int(row["restored_from"]))


_COLUMNS = "id, line, version, values_json, changed_by, reason, restored_from, created_at"


def current(line: str, *, db: Database | None = None) -> ThresholdSet:
    """The latest version of the line. `ThresholdError('no_thresholds')` when the line has none (LP and RF until the owner saves one)."""
    _line(line)
    with (db or get_database()).read() as u:
        row = u.session.one(f"SELECT {_COLUMNS} FROM audit_threshold_sets WHERE line = :l ORDER BY version DESC LIMIT 1", {"l": line})
    if row is None:
        raise ThresholdError("no_thresholds", f"the line {line} has no thresholds yet: save a set for it or copy one from another line")
    return _set(row)


def history(line: str, *, db: Database | None = None) -> list[ThresholdSet]:
    """Every version of the line, the newest first (empty when the line has none)."""
    _line(line)
    with (db or get_database()).read() as u:
        rows = u.session.query(f"SELECT {_COLUMNS} FROM audit_threshold_sets WHERE line = :l ORDER BY version DESC", {"l": line})
    return [_set(r) for r in rows]


# ---- writing --------------------------------------------------------------------------------------------------------------------------

def _write(line: str, clean: dict[str, Any], *, reason: str | None, actor: str, restored_from: int | None, db: Database | None) -> ThresholdSet:
    text = _canonical(clean)
    with (db or get_database()).write() as u:
        latest = u.session.one(f"SELECT {_COLUMNS}, values_hash FROM audit_threshold_sets WHERE line = :l ORDER BY version DESC LIMIT 1", {"l": line})
        if latest is not None and latest["values_hash"] == _hash(text):
            return replace(_set(latest), unchanged=True)
        version = 1 if latest is None else int(latest["version"]) + 1
        new_id = u.session.insert("audit_threshold_sets", {"line": line, "version": version, "values_json": text, "values_hash": _hash(text),
                                                           "changed_by": actor, "reason": reason, "restored_from": restored_from})
        row = u.session.one(f"SELECT {_COLUMNS} FROM audit_threshold_sets WHERE id = :i", {"i": new_id})
    return _set(row)                                                                  # type: ignore[arg-type]


def save(line: str, values: Mapping[str, Any], *, reason: str | None, actor: str, db: Database | None = None) -> ThresholdSet:
    """Write the next version of the line. Anything out of range, a key more or less, or a set that `AuditConfig` refuses is `bad_input` and nothing is written.

    A set equal to the current one is not written: the current set comes back with `unchanged=True`."""
    _line(line)
    clean = _normalize(values)
    to_config(clean)                                                                  # the audit has the last word
    return _write(line, clean, reason=_reason(reason), actor=_actor(actor), restored_from=None, db=db)


def restore(line: str, set_id: int, *, actor: str, db: Database | None = None) -> ThresholdSet:
    """A new version of the line with the values of an old one; the new row points at it in `restored_from`. The old version is not touched."""
    _line(line)
    if isinstance(set_id, bool) or not isinstance(set_id, int):
        raise ThresholdError("bad_input", "set_id is a whole number")
    with (db or get_database()).read() as u:
        old = u.session.one(f"SELECT {_COLUMNS} FROM audit_threshold_sets WHERE id = :i AND line = :l", {"i": set_id, "l": line})
    if old is None:
        raise ThresholdError("bad_input", f"there is no version {set_id} of the line {line}")
    clean = _normalize(json.loads(old["values_json"]))
    to_config(clean)
    return _write(line, clean, reason=f"restored from version {old['version']}", actor=_actor(actor), restored_from=int(old["id"]), db=db)


def copy_line(target: str, source: str, *, actor: str, db: Database | None = None) -> ThresholdSet:
    """The current set of `source` saved as the next version of `target`. `no_thresholds` when the source has none."""
    _line(target)
    if _line(source) == target:
        raise ThresholdError("bad_input", "target and source are different lines")
    origin = current(source, db=db)
    return save(target, origin.values, reason=f"copied from {source} version {origin.version}", actor=actor, db=db)

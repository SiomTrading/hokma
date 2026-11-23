"""Asset panel and descriptor helpers shared by the factor and alpha functions.

Descriptors are named by the public names of the fundamentals module (`hokma.quant.fundamentals.descriptors`); the class of the
library behind each name is instantiated here, never in an agent-reachable path.
"""
from __future__ import annotations

import math
from typing import Any, Mapping

import numpy as np
import pandas as pd

from hokma.quant.errors import BadInput
from hokma.quant.fundamentals import descriptors as fdesc
from hokma.quant.fundamentals.panel import build_panel
from hokma.quant.portfolio import _common as c
from hokma.quant.portfolio import _lib

MAX_DESCRIPTORS = 20
MAX_FACTORS = 30
MAX_WHOLE = 100_000
MAX_HALF_LIFE = 10_000.0
OUTLIERS = ("winsorizer", "passthrough")
SCORERS = ("standard", "gaussian_rank", "percentile_rank", "tanh", "passthrough")


def sk_fields(fields: Any, active: Any = None, *, infer_from: str = "returns") -> tuple[dict[str, pd.DataFrame], pd.DataFrame, pd.DataFrame, tuple[str, ...]]:
    """Wide frames (dates by assets) cleaned of inactive assets. Returns the numeric fields, the active mask and notes."""
    if not isinstance(fields, Mapping):
        raise BadInput(f"fields must be a dict of DataFrames (dates by assets), got {type(fields).__name__}")
    if not fields:
        raise c.Domain("fields is empty")
    if any(isinstance(f, pd.DataFrame) and f.size == 0 for f in fields.values()):
        raise c.Domain("a field has no rows or no assets")
    key = infer_from if infer_from in fields else next(iter(fields))
    p = build_panel(dict(fields), active, infer_from=key)
    notes = (f"cleared {p.cleared_cells} value(s) of inactive assets",) if p.cleared_cells else ()
    return p.numeric(), p.active, p.numeric()[key], notes


def _whole(label: str, value: Any, low: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise BadInput(f"{label} must be a whole number, got {value!r}")
    if not low <= int(value) <= MAX_WHOLE:
        raise c.Domain(f"{label}={int(value)} is outside [{low}, {MAX_WHOLE}]")
    return int(value)


def _param(spec_name: str, kind: str, name: str, value: Any) -> Any:
    if kind in ("lag", "window", "whole_or_none"):
        return _whole(name, value, 1)
    if kind == "skip":
        return _whole(name, value, 0)
    if kind == "pos_float":
        f = c.number(name, value, low=0.0, high=MAX_HALF_LIFE, low_open=True)
        return f
    if kind == "float":
        return c.number(name, value)
    if kind == "bool":
        return bool(value)
    raise BadInput(f"{spec_name}: unsupported parameter kind {kind}")


def descriptor_instance(item: Any, fields: Mapping[str, Any], *, label: str = "descriptor") -> tuple[str, Any]:
    """(name, library descriptor) from {"kind": "ew_momentum", "name": "mom", "params": {"half_life": 20}}.

    Raises Domain naming the panel field a descriptor needs and the panel does not have.
    """
    if not isinstance(item, Mapping) or set(item) - {"kind", "name", "params"}:
        raise BadInput(f"{label}: expected a dict with kind, name and params")
    kind = item.get("kind")
    spec = fdesc.BY_NAME.get(kind) if isinstance(kind, str) else None
    if spec is None:
        raise c.Domain(f"{label}: unknown descriptor kind {kind!r}; see the fundamentals descriptors")
    given = item.get("params") or {}
    if not isinstance(given, Mapping):
        raise BadInput(f"{label}: params must be a dict")
    allowed = set(spec.params) | set(spec.field_params)
    unknown = set(given) - allowed
    if unknown:
        raise BadInput(f"{label}: unknown parameter(s) {sorted(unknown)} for {kind}")
    params: dict[str, Any] = {}
    for pname, (pkind, default) in spec.params.items():
        value = given.get(pname, default)
        if value is None:
            if pkind == "lag" and default is None:
                raise c.Domain(f"{label}: {kind} needs {pname}")
            continue
        params[pname] = _param(str(kind), pkind, pname, value)
    needed = list(spec.fields)
    for pname in spec.field_params:
        value = given.get(pname)
        if not isinstance(value, str) or not value or len(value) > 40 or not all(ch.islower() or ch.isdigit() or ch == "_" for ch in value):
            raise BadInput(f"{label}: {pname} must be the name of a field of the panel")
        params[pname] = value
        needed.append(value)
    if spec.extra:
        raise c.Domain(f"{label}: {kind} needs an external series and is not available here")
    missing = [f for f in dict.fromkeys(needed) if f not in fields]
    if missing:
        raise c.Domain(f"data_missing:{','.join(missing)} (descriptor {kind})")
    name = item.get("name") or kind
    if not isinstance(name, str) or not name or len(name) > 40:
        raise BadInput(f"{label}: name must be a short text")
    return name, getattr(_lib.sk_descriptor, spec.cls)(**params)


def descriptor_list(items: Any, fields: Mapping[str, Any], *, label: str = "descriptors") -> list[tuple[str, Any]]:
    if not isinstance(items, (list, tuple)) or not items or len(items) > MAX_DESCRIPTORS:
        raise BadInput(f"{label} must be a list of 1 to {MAX_DESCRIPTORS} descriptor dicts")
    out = [descriptor_instance(it, fields, label=f"{label}[{i}]") for i, it in enumerate(items)]
    names = [n for n, _ in out]
    if len(set(names)) != len(names):
        raise BadInput(f"{label}: descriptor names must be unique")
    return out


def transformer(kind: Any, options: tuple[str, ...], label: str) -> Any:
    """A cross-sectional transformer by public name, or the string the library reads as passthrough."""
    which = c.choice(label, kind, options)
    return "passthrough" if which == "passthrough" else _lib.SCORERS[which]()


def required_fields(items: Any) -> tuple[str, ...]:
    """The panel fields a list of descriptor dicts reads (for an error message before building anything)."""
    names: list[str] = []
    for it in items if isinstance(items, (list, tuple)) else ():
        spec = fdesc.BY_NAME.get(it.get("kind")) if isinstance(it, Mapping) else None
        if spec is not None:
            names += list(spec.fields)
    return tuple(dict.fromkeys(names))


def finite_vector(values: Any, columns: pd.Index) -> dict[str, float | None]:
    arr = np.asarray(values, dtype=float)
    return {str(col): (float(v) if math.isfinite(float(v)) else None) for col, v in zip(columns, arr)}

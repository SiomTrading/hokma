"""Turns the typed objects the filing parsers return into plain JSON friendly structures (dicts, lists, numbers, text)."""
from __future__ import annotations

import dataclasses
import math
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

import numpy as np
import pandas as pd

MAX_DEPTH = 8
MAX_ROWS = 5000


def to_plain(obj: Any, *, depth: int = MAX_DEPTH) -> Any:
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, float):
        return None if math.isnan(obj) or math.isinf(obj) else obj
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return to_plain(obj.value, depth=depth)
    if isinstance(obj, np.generic):
        return to_plain(obj.item(), depth=depth)
    if pd.isna(obj) is True:                      # pandas NA / NaT scalar
        return None
    if depth <= 0:
        return repr(obj)[:200]
    if isinstance(obj, pd.DataFrame):
        return [to_plain(row, depth=depth - 1) for row in obj.head(MAX_ROWS).to_dict("records")]
    if isinstance(obj, pd.Series):
        return {str(k): to_plain(v, depth=depth - 1) for k, v in obj.items()}
    if isinstance(obj, dict):
        return {str(k): to_plain(v, depth=depth - 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [to_plain(v, depth=depth - 1) for v in list(obj)[:MAX_ROWS]]
    if hasattr(obj, "model_dump"):
        try:
            return to_plain(obj.model_dump(), depth=depth - 1)
        except Exception:                          # noqa: BLE001  (fall through to attribute walk)
            pass
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_plain(getattr(obj, f.name, None), depth=depth - 1) for f in dataclasses.fields(obj)}
    attrs = getattr(obj, "__dict__", None)
    if attrs:
        return {k: to_plain(v, depth=depth - 1) for k, v in attrs.items() if not k.startswith("_") and not callable(v)}
    return repr(obj)[:200]


def pick(obj: Any, *names: str) -> dict[str, Any]:
    """Plain values of the named attributes (missing ones are left out)."""
    out: dict[str, Any] = {}
    for name in names:
        if hasattr(obj, name):
            try:
                value = getattr(obj, name)
            except Exception:                      # noqa: BLE001  (a property that fails is a missing field)
                continue
            if not callable(value):
                out[name] = to_plain(value)
    return out

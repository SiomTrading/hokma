"""Asset panel (SK-103): aligned fields over dates and assets, with a policy for inactive assets.

An asset that is not in the universe on a date (before it listed, after it stopped trading) is inactive there. Its values on that date are
cleared (NaN, or the missing category) and are never filled forward: a delisted asset does not keep its last price. A gap inside the life of an
asset (a holiday, a missing quote) is a missing value of an active asset, and it stays missing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np
import pandas as pd

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.fundamentals import _common as c

MAX_FIELDS = 64
MISSING_CODE = -1


@dataclass(frozen=True)
class Field2D:
    """A numeric field: one value per date and asset."""

    name: str
    values: pd.DataFrame


@dataclass(frozen=True)
class Field3D:
    """A numeric field with a third axis (for example factor exposures): one frame per label."""

    name: str
    labels: tuple[str, ...]
    values: Mapping[str, pd.DataFrame]


@dataclass(frozen=True)
class FieldCategorical:
    """A categorical field stored as integer codes (MISSING_CODE when unknown) and its levels."""

    name: str
    levels: tuple[str, ...]
    codes: pd.DataFrame

    def decode(self) -> pd.DataFrame:
        table = np.array(list(self.levels) + [None], dtype=object)
        return pd.DataFrame(table[self.codes.to_numpy()], index=self.codes.index, columns=self.codes.columns)


@dataclass(frozen=True)
class AssetPanel:
    """Fields over the same dates and assets, and the mask of the assets that belong to the universe on each date."""

    index: pd.Index
    columns: pd.Index
    active: pd.DataFrame
    fields: Mapping[str, Field2D | Field3D | FieldCategorical] = field(default_factory=dict)
    cleared_cells: int = 0

    def numeric(self) -> dict[str, pd.DataFrame]:
        return {n: f.values for n, f in self.fields.items() if isinstance(f, Field2D)}

    def view(self, start: Any = None, stop: Any = None) -> "AssetPanel":
        """The panel between two dates (both included). The fields are cut, never filled."""
        sl = slice(start, stop)
        sub = self.active.loc[sl]
        out: dict[str, Field2D | Field3D | FieldCategorical] = {}
        for n, f in self.fields.items():
            if isinstance(f, Field2D):
                out[n] = Field2D(n, f.values.loc[sl])
            elif isinstance(f, Field3D):
                out[n] = Field3D(n, f.labels, {k: v.loc[sl] for k, v in f.values.items()})
            else:
                out[n] = FieldCategorical(n, f.levels, f.codes.loc[sl])
        return AssetPanel(sub.index, sub.columns, sub, out, 0)

    @property
    def inactive_cells(self) -> int:
        return int((~self.active.to_numpy()).sum())


def infer_active(frame: pd.DataFrame) -> pd.DataFrame:
    """Active from the first to the last finite value of each asset (both included). Nothing before or after counts as in the universe."""
    finite = np.isfinite(frame.to_numpy(dtype=float))
    mask = np.zeros_like(finite, dtype=bool)
    for j in range(finite.shape[1]):
        where = np.flatnonzero(finite[:, j])
        if len(where):
            mask[where[0]: where[-1] + 1, j] = True
    return pd.DataFrame(mask, index=frame.index, columns=frame.columns)


def _check_frame(name: str, frame: Any, ref: pd.DataFrame | None) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise BadInput(f"field {name!r}: expected a DataFrame, got {type(frame).__name__}")
    if frame.shape[0] == 0 or frame.shape[1] == 0:
        raise BadInput(f"field {name!r} is empty")
    if ref is not None and (not frame.index.equals(ref.index) or not frame.columns.equals(ref.columns)):
        raise BadInput(f"field {name!r} does not have the same dates and assets as the other fields")
    if len(frame.columns) != len(set(map(str, frame.columns))):
        raise BadInput(f"field {name!r}: asset names must be unique")
    try:
        return frame.astype(float)
    except (TypeError, ValueError) as exc:
        raise BadInput(f"field {name!r}: values are not numeric") from exc


def build_panel(fields: Mapping[str, pd.DataFrame], active: pd.DataFrame | None = None, *,
                categorical: Mapping[str, tuple[Any, tuple[str, ...]]] | None = None,
                three_d: Mapping[str, Mapping[str, pd.DataFrame]] | None = None,
                infer_from: str | None = None) -> AssetPanel:
    """A panel from wide frames. `active` is the universe mask; when it is not given it is inferred from the field `infer_from` (the first field by default)."""
    if not isinstance(fields, Mapping) or not fields:
        raise BadInput("fields must be a non-empty dict of DataFrames")
    if len(fields) + len(categorical or {}) + len(three_d or {}) > MAX_FIELDS:
        raise BadInput(f"more than {MAX_FIELDS} fields")
    names = list(fields)
    ref = _check_frame(names[0], fields[names[0]], None)
    numeric = {n: (ref if i == 0 else _check_frame(n, fields[n], ref)) for i, n in enumerate(names)}
    if active is None:
        key = infer_from if infer_from is not None else names[0]
        if key not in numeric:
            raise BadInput(f"infer_from {key!r} is not a field")
        mask = infer_active(numeric[key])
    else:
        if not isinstance(active, pd.DataFrame) or not active.index.equals(ref.index) or not active.columns.equals(ref.columns):
            raise BadInput("active must be a DataFrame with the same dates and assets as the fields")
        mask = active.astype(bool)
    out_mask = ~mask.to_numpy()
    cleared = 0
    built: dict[str, Field2D | Field3D | FieldCategorical] = {}
    for n, frame in numeric.items():
        values = frame.to_numpy(copy=True)
        cleared += int(np.isfinite(values[out_mask]).sum())
        values[out_mask] = np.nan
        built[n] = Field2D(n, pd.DataFrame(values, index=ref.index, columns=ref.columns))
    for n, (codes, levels) in (categorical or {}).items():
        cf = _check_frame(n, codes, ref).fillna(MISSING_CODE).astype(int)
        arr = cf.to_numpy(copy=True)
        if (arr >= len(levels)).any():
            raise BadInput(f"categorical field {n!r} has a code outside its levels")
        arr[out_mask] = MISSING_CODE
        built[n] = FieldCategorical(n, tuple(str(x) for x in levels), pd.DataFrame(arr, index=ref.index, columns=ref.columns))
    for n, parts in (three_d or {}).items():
        labels = tuple(parts)
        frames = {}
        for lab in labels:
            vals = _check_frame(f"{n}.{lab}", parts[lab], ref).to_numpy(copy=True)
            vals[out_mask] = np.nan
            frames[lab] = pd.DataFrame(vals, index=ref.index, columns=ref.columns)
        built[n] = Field3D(n, labels, frames)
    return AssetPanel(ref.index, ref.columns, pd.DataFrame(mask.to_numpy(), index=ref.index, columns=ref.columns), built, cleared)


@registry.register("fundamentals.panel", module="fundamentals", piece_ids=("SK-103",),
                   inputs={"fields": {"type": "dict", "required": True}, "active": {"type": "dataframe", "required": False, "default": None},
                           "infer_from": {"type": "str", "required": False, "default": None}},
                   summary="Builds an asset panel from wide frames (dates by assets) and reports its universe: first and last active date of each asset and the cells cleared because the asset was inactive. Inactive assets are never filled.")
def panel(fields: Any, active: Any = None, infer_from: str | None = None):
    name, pieces, method = "fundamentals.panel", ("SK-103",), "asset panel with inactive policy"
    if not isinstance(fields, dict):
        raise BadInput(f"fields must be a dict of DataFrames, got {type(fields).__name__}")
    if not fields or any(isinstance(f, pd.DataFrame) and f.size == 0 for f in fields.values()):
        return c.empty(name, "mixed", method, "fields is empty", piece_ids=pieces)
    p = build_panel(fields, active, infer_from=infer_from)
    if not any(np.isfinite(f.values.to_numpy()).any() for f in p.fields.values() if isinstance(f, Field2D)):
        return c.empty(name, "mixed", method, "no finite value in any field", piece_ids=pieces)
    act = p.active
    first = {str(col): (str(act.index[np.flatnonzero(act[col].to_numpy())[0]])[:10] if act[col].any() else None) for col in act.columns}
    last = {str(col): (str(act.index[np.flatnonzero(act[col].to_numpy())[-1]])[:10] if act[col].any() else None) for col in act.columns}
    latest = {n: {str(k): v for k, v in f.values.iloc[-1].items()} for n, f in p.fields.items() if isinstance(f, Field2D)}
    value = {"n_dates": len(p.index), "n_assets": len(p.columns), "assets": [str(x) for x in p.columns], "fields": list(p.fields),
             "first_active": first, "last_active": last, "inactive_cells": p.inactive_cells, "cleared_cells": p.cleared_cells, "latest": latest}
    warns = (f"cleared {p.cleared_cells} value(s) of inactive assets",) if p.cleared_cells else ()
    return c.build(name, value, unit="mixed", method=method, params={"inferred_active": active is None}, piece_ids=pieces,
                   inputs=([str(x) for x in p.columns], list(p.fields), len(p.index), p.cleared_cells), warnings=warns, as_of=p.index[-1])

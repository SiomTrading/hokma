"""Mixing of the views of the profiles of one desk (AH-16).

Abstentions are left out. Profiles whose past views are highly correlated (above 0.8 in the history of `desk_signals`) say the same
thing twice: they are merged and count as one profile, and the result says so in `warnings`.

Weight of a view = confidence / 100 times an optional weight per source. A merged group has the weighted mean of the views of its
members and the mean of their weights (one profile, not several).
"""
from __future__ import annotations

import math
from typing import Any, Mapping

import numpy as np
import pandas as pd

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.portfolio import _common as c
from hokma.quant.types import QuantResult

MAX_SIGNALS = 200
SOURCE_COLUMNS = ("source", "seat_code")
KEY_COLUMNS = ("symbol", "as_of")


def _view(item: Any, position: int) -> dict[str, Any]:
    """One signal (a Signal object or its dict) as {source, symbol, value, confidence, abstained}."""
    get = item.get if isinstance(item, Mapping) else (lambda k, d=None: getattr(item, k, d))
    if not isinstance(item, Mapping) and not hasattr(item, "abstained"):
        raise BadInput(f"signals[{position}]: expected a Signal or a dict, got {type(item).__name__}")
    source = get("source")
    if not isinstance(source, str) or not source:
        raise BadInput(f"signals[{position}]: source must be a text")
    abstained = get("abstained")
    if not isinstance(abstained, (bool, np.bool_)):
        raise BadInput(f"signals[{position}]: abstained must be true or false")
    value, confidence = get("value"), get("confidence")
    if not abstained:
        if isinstance(value, bool) or not isinstance(value, (int, float, np.integer, np.floating)) or not math.isfinite(float(value)):
            raise c.Domain(f"{source}: value must be a finite number when the signal did not abstain")
        if not -1.0 <= float(value) <= 1.0:
            raise c.Domain(f"{source}: value {value} is outside [-1, 1]")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float, np.integer, np.floating)) or not math.isfinite(float(confidence)):
            raise c.Domain(f"{source}: confidence must be a finite number")
        if not 0.0 <= float(confidence) <= 100.0:
            raise c.Domain(f"{source}: confidence {confidence} is outside [0, 100]")
    return {"source": source, "symbol": get("symbol"), "value": None if abstained else float(value),
            "confidence": 0.0 if abstained else float(confidence), "abstained": bool(abstained)}


def _history_matrix(history: Any) -> pd.DataFrame:
    """Views by (symbol, as_of) as rows and source as columns, from a long table like `desk_signals`. Abstentions stay empty."""
    if not isinstance(history, pd.DataFrame):
        raise BadInput(f"history: expected a DataFrame, got {type(history).__name__}")
    src = next((col for col in SOURCE_COLUMNS if col in history.columns), None)
    if src is None or "value" not in history.columns or not set(KEY_COLUMNS) <= set(history.columns):
        raise BadInput("history needs the columns source (or seat_code), symbol, as_of and value")
    frame = history[[src, *KEY_COLUMNS, "value"]].copy()
    try:
        frame["value"] = frame["value"].astype(float)
    except (TypeError, ValueError) as exc:
        raise BadInput("history: value is not numeric") from exc
    if "abstained" in history.columns:
        frame.loc[history["abstained"].astype(bool).to_numpy(), "value"] = np.nan
    frame["value"] = frame["value"].replace([np.inf, -np.inf], np.nan)
    return frame.pivot_table(index=list(KEY_COLUMNS), columns=src, values="value", aggfunc="last", dropna=False)


def _groups(sources: list[str], matrix: pd.DataFrame, threshold: float, min_overlap: int) -> tuple[list[list[str]], dict[tuple[str, str], float]]:
    """Connected components of the sources whose correlation in the history is above the threshold."""
    parent = {s: s for s in sources}

    def find(s: str) -> str:
        while parent[s] != s:
            parent[s] = parent[parent[s]]
            s = parent[s]
        return s

    found: dict[tuple[str, str], float] = {}
    present = [s for s in sources if s in matrix.columns]
    if len(present) > 1:
        corr = matrix[present].corr(min_periods=min_overlap)
        for i, a in enumerate(present):
            for b in present[i + 1:]:
                rho = corr.loc[a, b]
                if np.isfinite(rho) and rho > threshold:
                    found[(a, b)] = float(rho)
                    parent[find(a)] = find(b)
    groups: dict[str, list[str]] = {}
    for s in sources:
        groups.setdefault(find(s), []).append(s)
    return list(groups.values()), found


@registry.register("portfolio.blend_signals", module="portfolio",
                   summary="Blend the views of a desk: abstentions are excluded and profiles with correlation above 0.8 in the signal history count as one.",
                   inputs={"signals": {"type": "list", "required": True}, "history": {"type": "dataframe", "required": False},
                           "weights": {"type": "dict", "required": False},
                           "correlation_threshold": {"type": "float", "required": False, "default": 0.8},
                           "min_overlap": {"type": "int", "required": False, "default": 10}},
                   piece_ids=("AH-16",))
def blend_signals(signals: Any, history: Any = None, weights: Any = None, correlation_threshold: float = 0.8, min_overlap: int = 10) -> QuantResult:
    name, pieces = "portfolio.blend_signals", ("AH-16",)
    params = {"correlation_threshold": correlation_threshold, "min_overlap": min_overlap}

    def compute() -> Any:
        if not isinstance(signals, (list, tuple)):
            raise BadInput(f"signals must be a list, got {type(signals).__name__}")
        if not signals:
            raise c.Domain("signals is empty")
        if len(signals) > MAX_SIGNALS:
            raise BadInput(f"more than {MAX_SIGNALS} signals")
        threshold = c.number("correlation_threshold", correlation_threshold, low=0.0, high=1.0)
        overlap = int(c.number("min_overlap", min_overlap, low=2, integer=True))
        extra: dict[str, float] = {}
        if weights is not None:
            if not isinstance(weights, Mapping):
                raise BadInput("weights must be a dict source -> number")
            extra = {str(k): c.number(f"weights[{k}]", v, low=0.0) for k, v in weights.items()}
        views = [_view(s, i) for i, s in enumerate(signals)]
        names = [v["source"] for v in views]
        if len(set(names)) != len(names):
            raise c.Domain("two signals have the same source: one view per source")
        symbols = {v["symbol"] for v in views if v["symbol"] is not None}
        if len(symbols) > 1:
            raise c.Domain(f"signals are about different symbols {sorted(map(str, symbols))}: blend one symbol at a time")
        unknown = [k for k in extra if k not in names]
        if unknown:
            raise BadInput(f"weights: unknown source(s) {unknown}")
        used = [v for v in views if not v["abstained"]]
        abstained = [v["source"] for v in views if v["abstained"]]
        if not used:
            raise c.Domain("every source abstained: there is nothing to blend")
        weight = {v["source"]: (v["confidence"] / 100.0) * extra.get(v["source"], 1.0) for v in used}
        if sum(weight.values()) <= 0.0:
            raise c.Domain("every weight is zero (confidence 0 or weight 0)")
        by_source = {v["source"]: v for v in used}
        warns: list[str] = []
        if abstained:
            warns.append(f"excluded abstentions: {abstained}")
        pairs: dict[tuple[str, str], float] = {}
        groups = [[v["source"]] for v in used]
        if history is not None:
            matrix = _history_matrix(history)
            groups, pairs = _groups([v["source"] for v in used], matrix, threshold, overlap)
            for members in groups:
                if len(members) > 1:
                    rhos = [r for (a, b), r in pairs.items() if a in members and b in members]
                    warns.append(f"merged profiles {members} (correlation up to {max(rhos):.4f} above {threshold}): they count as one")
        rows: list[dict[str, Any]] = []
        for members in groups:
            w = np.array([weight[m] for m in members])
            vals = np.array([by_source[m]["value"] for m in members])
            confs = np.array([by_source[m]["confidence"] for m in members])
            denom = float(w.sum())
            if denom > 0:
                value_g, conf_g = float((w * vals).sum() / denom), float((w * confs).sum() / denom)
            else:
                value_g, conf_g = float(vals.mean()), float(confs.mean())
            rows.append({"sources": list(members), "value": value_g, "confidence": conf_g, "weight": float(w.mean())})
        total = sum(r["weight"] for r in rows)
        if total <= 0.0:
            raise c.Domain("every group has zero weight")
        blended = sum(r["weight"] * r["value"] for r in rows) / total
        confidence = sum(r["weight"] * r["confidence"] for r in rows) / total
        for r in rows:
            r["share"] = r["weight"] / total
        return {"value": float(max(-1.0, min(1.0, blended))), "confidence": float(confidence), "n_signals": len(views), "n_used": len(used),
                "n_groups": len(rows), "abstained": abstained, "groups": rows}, tuple(warns)

    return c.run(name, unit="view", method="weighted mean of non-abstaining views with correlated profiles merged", params=params,
                 piece_ids=pieces, compute=compute, inputs=(signals, history, weights, params))

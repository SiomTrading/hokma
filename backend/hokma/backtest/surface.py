"""Surface of an optimization (SPEC-020, 4.5: B-29, L-42 to L-46): the objective over the lattice of parameters, to see whether an optimum is a
broad hill or a spike. Pure functions over a finished `OptimizationResult`; nothing runs the engine here. A part that cannot be computed
is reported as missing with its reason (`analyze`), never left out."""
from __future__ import annotations

import html
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

from hokma.backtest.optimize import OptimizationResult, ParamSpace, SearchError, load
from hokma.db import Database
from hokma.quant.errors import BadInput

PARTS = ("peaks", "plateaus", "slices", "failed")
Cluster = Callable[[np.ndarray, int, int], "tuple[np.ndarray, np.ndarray]"]     # (points, k, seed) -> (labels, centers)


class ClusterUnavailable(SearchError):
    """The grouping of the portfolio module is not available here. `code` is the stable `cluster_unavailable`."""

    code = "cluster_unavailable"


def _default_cluster() -> Cluster:
    """The k-means of the portfolio module (one implementation: no grouping is written here). It is imported on use because that module is built
    in another hito; without it the plateaus part is reported as missing."""
    try:
        from hokma.quant.portfolio.cluster import kmeans
    except ImportError as exc:
        raise ClusterUnavailable("cluster_unavailable: the grouping of hokma.quant.portfolio is not available") from exc
    return kmeans


# --------------------------------------------------------------------------------------------------------------
# The surface
# --------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, eq=False)
class Surface:
    space: ParamSpace
    metric: str
    values: np.ndarray                  # the objective on the lattice, NaN where there is none
    status: np.ndarray                  # "ok", "failed", "excluded" (the constraints cut it) or "missing" (never tried)
    best: tuple[int, ...] | None        # lattice index of the best point

    def params(self, idx: tuple[int, ...]) -> dict[str, Any]:
        return {a.name: a.values[i] for a, i in zip(self.space.axes, idx)}

    def valid(self) -> np.ndarray:
        return self.status == "ok"

    def best_point(self) -> dict[str, Any]:
        if self.best is None:
            raise SearchError("no_valid_points: no trial has an objective")
        return self.params(self.best)


def surface(result: OptimizationResult, *, source: Literal["any", "engine", "fast"] = "any") -> Surface:
    """The lattice filled from the trials. `any` takes the engine value of a point when it has one and the fast one otherwise."""
    if source not in ("any", "engine", "fast"):
        raise SearchError("source is any, engine or fast")
    space = result.space
    axes = space.axes
    where = [{v: i for i, v in enumerate(a.values)} for a in axes]
    shape = tuple(len(a.values) for a in axes)
    values = np.full(shape, np.nan)
    status = np.full(shape, "missing", dtype=object)
    for idx in np.ndindex(shape):
        if not space.valid({a.name: a.values[i] for a, i in zip(axes, idx)}):
            status[idx] = "excluded"
    ordered = sorted(result.trials, key=lambda t: t.source == "engine")           # engine trials come last and win
    for t in ordered:
        if source != "any" and t.source != source:
            continue
        idx = tuple(where[k][t.params[a.name]] for k, a in enumerate(axes))
        if t.objective is None:
            if status[idx] != "ok":
                status[idx] = "failed"
        else:
            values[idx], status[idx] = t.objective, "ok"
    ok = status == "ok"
    best = None
    if result.best is not None and source in ("any", "engine"):
        best = tuple(where[k][result.best.params[a.name]] for k, a in enumerate(axes))
    elif ok.any():
        best = tuple(int(i) for i in np.unravel_index(np.argmax(np.where(ok, values, -np.inf)), shape))
    return Surface(space, result.objective, values, status, best)


def _surface(source: "Surface | OptimizationResult") -> Surface:
    return source if isinstance(source, Surface) else surface(source)


def _neighbours(surf: Surface, idx: tuple[int, ...]) -> list[tuple[int, ...]]:
    """The points one step away along each numeric axis that have an objective."""
    out = []
    for k, a in enumerate(surf.space.axes):
        if a.kind == "choice":
            continue
        for d in (-1, 1):
            j = idx[k] + d
            if 0 <= j < surf.values.shape[k]:
                n = idx[:k] + (j,) + idx[k + 1:]
                if surf.status[n] == "ok":
                    out.append(n)
    return out


# --------------------------------------------------------------------------------------------------------------
# B-29: the map
# --------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Cell:
    x: Any
    y: Any
    value: float | None
    status: str


@dataclass(frozen=True)
class Heatmap:
    x: str
    y: str
    metric: str
    x_values: tuple[Any, ...]
    y_values: tuple[Any, ...]
    fixed: Mapping[str, Any]            # the other parameters, held at one value
    cells: tuple[Cell, ...]             # one per combination, x changing slowest

    def to_dict(self) -> dict[str, Any]:
        return {"x": self.x, "y": self.y, "metric": self.metric, "x_values": list(self.x_values), "y_values": list(self.y_values),
                "fixed": dict(self.fixed), "cells": [{"x": c.x, "y": c.y, "value": c.value, "status": c.status} for c in self.cells]}

    def svg(self, *, cell: int = 48) -> str:
        """The map as one SVG file with no external resource: no link, no script, no font file, no image. Text is escaped."""
        valid = [c.value for c in self.cells if c.value is not None]
        low, high = (min(valid), max(valid)) if valid else (0.0, 0.0)
        left, top, gap = 76, 28, 8
        width, height = left + len(self.x_values) * cell + gap, top + len(self.y_values) * cell + 38
        esc = lambda value: html.escape(str(value), quote=True)                  # noqa: E731
        parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" '
                 f'aria-label="{esc(self.metric)} by {esc(self.x)} and {esc(self.y)}">',
                 f'<text x="{left}" y="16" font-size="12" font-family="sans-serif">{esc(self.metric)}</text>']
        for ix, xv in enumerate(self.x_values):
            parts.append(f'<text x="{left + ix * cell + cell // 2}" y="{height - 22}" font-size="11" font-family="sans-serif" text-anchor="middle">{esc(xv)}</text>')
        parts.append(f'<text x="{left + len(self.x_values) * cell // 2}" y="{height - 6}" font-size="11" font-family="sans-serif" text-anchor="middle">{esc(self.x)}</text>')
        for iy, yv in enumerate(reversed(self.y_values)):
            parts.append(f'<text x="{left - 6}" y="{top + iy * cell + cell // 2 + 4}" font-size="11" font-family="sans-serif" text-anchor="end">{esc(yv)}</text>')
        parts.append(f'<text x="10" y="{top - 8}" font-size="11" font-family="sans-serif">{esc(self.y)}</text>')
        by_point = {(c.x, c.y): c for c in self.cells}
        for ix, xv in enumerate(self.x_values):
            for iy, yv in enumerate(reversed(self.y_values)):
                c = by_point[(xv, yv)]
                text = "" if c.value is None else format(c.value, ".4g")
                parts.append(f'<g><title>{esc(self.x)}={esc(xv)}, {esc(self.y)}={esc(yv)}, {esc(self.metric)}={esc(text or c.status)}</title>'
                             f'<rect x="{left + ix * cell}" y="{top + iy * cell}" width="{cell}" height="{cell}" fill="{_colour(c, low, high)}" stroke="#ffffff"/>'
                             f'<text x="{left + ix * cell + cell // 2}" y="{top + iy * cell + cell // 2 + 4}" font-size="10" font-family="sans-serif" '
                             f'text-anchor="middle" fill="#1a202c">{esc(text if c.value is not None else ("x" if c.status == "failed" else ""))}</text></g>')
        parts.append("</svg>")
        return "".join(parts)


def _colour(c: Cell, low: float, high: float) -> str:
    if c.value is None:
        return {"failed": "#e53e3e", "excluded": "#a0aec0"}.get(c.status, "#e2e8f0")
    t = 0.5 if high == low else (c.value - low) / (high - low)
    start, end = (43, 108, 176), (246, 224, 94)                                  # blue for the lowest value, yellow for the highest
    return "#" + "".join(f"{round(a + (b - a) * t):02x}" for a, b in zip(start, end))


def heatmap(source: "Surface | OptimizationResult", x: str | None = None, y: str | None = None, *, metric: str | None = None,
            fixed: Mapping[str, Any] | None = None) -> Heatmap:
    """The objective over two parameters: one cell per combination, with its status. With more than two parameters the others are held at the
    best point (or at `fixed`). `metric` is the objective of the search: that is the only one the trials carry."""
    surf = _surface(source)
    if metric is not None and metric != surf.metric:
        raise SearchError(f"metric_not_recorded: the trials carry {surf.metric!r}, not {metric!r}")
    names = surf.space.names
    if x is None and y is None and len(names) == 2:
        x, y = names
    if x is None or y is None or x == y or x not in names or y not in names:
        raise SearchError("a heat map needs two different parameters of the space")
    axes = surf.space.axes
    held = {n: v for n, v in (surf.best_point() if surf.best is not None else {}).items() if n not in (x, y)}
    held.update(fixed or {})
    index: list[Any] = []
    for a in axes:
        if a.name in (x, y):
            index.append(slice(None))
        elif a.name in held and held[a.name] in a.values:
            index.append(a.values.index(held[a.name]))
        else:
            raise SearchError(f"parameter {a.name} needs a value to hold it at: pass it in fixed")
    sub_values, sub_status = surf.values[tuple(index)], surf.status[tuple(index)]
    ax, ay = surf.space.axes[names.index(x)], surf.space.axes[names.index(y)]
    if names.index(x) > names.index(y):
        sub_values, sub_status = sub_values.T, sub_status.T                        # the slice keeps the order of the axes: x first
    cells = tuple(Cell(xv, yv, None if math.isnan(sub_values[i, j]) else float(sub_values[i, j]), str(sub_status[i, j]))
                  for i, xv in enumerate(ax.values) for j, yv in enumerate(ay.values))
    return Heatmap(x, y, surf.metric, ax.values, ay.values, {k: v for k, v in held.items()}, cells)


# --------------------------------------------------------------------------------------------------------------
# L-42: isolated peaks
# --------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Peak:
    params: Mapping[str, Any]
    value: float
    neighbour_mean: float
    prominence: float                    # (value - neighbour mean) as a share of the range of the whole surface
    label: str = "isolated_peak"


def isolated_peaks(source: "Surface | OptimizationResult", *, drop: float = 0.3) -> tuple[Peak, ...]:
    """The local maxima that stand alone: strictly above every neighbour and above their mean by at least `drop` of the range of the surface.
    A plateau has no strict maximum and a broad hill has neighbours close to its top, so neither is marked. An isolated peak is overfit."""
    if not 0 < drop <= 1:
        raise SearchError("drop is a share of the range, above 0 and at most 1")
    surf = _surface(source)
    ok = surf.valid()
    if not ok.any():
        raise SearchError("no_valid_points: no trial has an objective")
    span = float(np.nanmax(surf.values[ok]) - np.nanmin(surf.values[ok]))
    if span == 0:
        return ()
    found = []
    for idx in zip(*np.nonzero(ok)):
        idx = tuple(int(i) for i in idx)
        near = _neighbours(surf, idx)
        if not near:
            continue
        value = float(surf.values[idx])
        if value <= max(float(surf.values[n]) for n in near):
            continue
        mean = float(np.mean([surf.values[n] for n in near]))
        if (value - mean) / span >= drop:
            found.append(Peak(surf.params(idx), value, mean, (value - mean) / span))
    return tuple(sorted(found, key=lambda p: -p.value))


# --------------------------------------------------------------------------------------------------------------
# L-43: plateaus
# --------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Plateau:
    center: Mapping[str, float]          # in the units of the parameters
    size: int                            # points of the group
    mean_value: float
    tight: bool                          # every point is within `radius` lattice steps of the centre


def plateaus(source: "Surface | OptimizationResult", *, band: float = 0.2, radius: float = 1.5, k_max: int = 6, seed: int = 0,
             cluster: Cluster | None = None) -> tuple[Plateau, ...]:
    """Groups of the points near the top of the surface (within `band` of the range from the maximum). The smallest number of groups that
    are all tight is used (up to `k_max`); the grouping is the k-means of the portfolio module, or the `cluster` that is passed."""
    if not 0 < band <= 1 or radius <= 0 or not 1 <= k_max <= 20:
        raise SearchError("band is a share of the range, radius is above 0 and k_max is between 1 and 20")
    surf = _surface(source)
    if any(a.kind == "choice" for a in surf.space.axes):
        raise SearchError("plateaus_need_numeric_axes: a surface with a choice parameter has no distance between its points")
    ok = surf.valid()
    if not ok.any():
        raise SearchError("no_valid_points: no trial has an objective")
    top, low = float(np.nanmax(surf.values[ok])), float(np.nanmin(surf.values[ok]))
    members = [tuple(int(i) for i in idx) for idx in zip(*np.nonzero(ok & (surf.values >= top - band * (top - low) - 1e-12)))]
    group = cluster or _default_cluster()
    points = np.asarray(members, dtype=float)
    labels = centers = None
    tight = False
    for k in range(1, min(k_max, len(members)) + 1):
        labels, centers = group(points, k, seed)
        labels, centers = np.asarray(labels), np.asarray(centers, dtype=float)
        tight = bool(np.max(np.linalg.norm(points - centers[labels], axis=1)) <= radius)
        if tight:
            break
    out = []
    for g in range(len(centers)):
        mask = labels == g
        if not mask.any():
            continue
        centre = {a.name: round(_at(a.values, float(centers[g][k])), 10) for k, a in enumerate(surf.space.axes)}
        out.append(Plateau(centre, int(mask.sum()), float(np.mean([surf.values[m] for m, flag in zip(members, mask) if flag])), tight))
    return tuple(sorted(out, key=lambda p: -p.mean_value))


def _at(values: tuple[Any, ...], position: float) -> float:
    """The value of a parameter at a fractional lattice position."""
    if len(values) == 1:
        return float(values[0])
    return float(values[0]) + position * (float(values[1]) - float(values[0]))


# --------------------------------------------------------------------------------------------------------------
# L-44: slices
# --------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Slice:
    param: str
    values: tuple[Any, ...]
    objective: tuple[float | None, ...]
    effect: float | None                 # highest minus lowest objective along the slice
    flat: bool | None                    # None when the slice has fewer than two points with an objective


def slices(source: "Surface | OptimizationResult", *, at: Mapping[str, Any] | None = None, flat_tol: float = 1e-6) -> tuple[Slice, ...]:
    """The objective along each parameter with the others held at the best point (or at `at`). A parameter with no effect gives a flat slice:
    its effect is at most `flat_tol` of the range of the whole surface."""
    surf = _surface(source)
    ok = surf.valid()
    if not ok.any():
        raise SearchError("no_valid_points: no trial has an objective")
    centre = dict(surf.best_point())
    centre.update(at or {})
    span = float(np.nanmax(surf.values[ok]) - np.nanmin(surf.values[ok]))
    out = []
    for k, a in enumerate(surf.space.axes):
        index: list[Any] = []
        for b in surf.space.axes:
            if b.name == a.name:
                index.append(slice(None))
            elif centre.get(b.name) in b.values:
                index.append(b.values.index(centre[b.name]))
            else:
                raise SearchError(f"parameter {b.name} needs a value in the lattice to hold it at")
        line, state = surf.values[tuple(index)], surf.status[tuple(index)]
        objective = tuple(None if s != "ok" else float(v) for v, s in zip(line, state))
        seen = [v for v in objective if v is not None]
        effect = None if len(seen) < 2 else max(seen) - min(seen)
        out.append(Slice(a.name, a.values, objective, effect, None if effect is None else effect <= flat_tol * span))
    return tuple(out)


# --------------------------------------------------------------------------------------------------------------
# L-45: failed runs
# --------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Failure:
    params: Mapping[str, Any]
    error: str | None


def failed(result: OptimizationResult) -> tuple[Failure, ...]:
    """The points that failed, with the error of each, for whoever designs the strategy. They count in the trials of the ledger."""
    return tuple(Failure(t.params, t.error) for t in result.trials if t.status == "failed")


# --------------------------------------------------------------------------------------------------------------
# L-46: the whole analysis
# --------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Part:
    status: Literal["ok", "missing"]
    value: Any = None
    reason: str | None = None            # why a missing part is missing, with its stable code


@dataclass(frozen=True)
class Analysis:
    opt_id: int | None
    strategy_id: str
    objective: str
    parts: Mapping[str, Part]            # always the four parts of PARTS
    edge_of_space: tuple[str, ...]
    warnings: tuple[str, ...]


def analyze_result(result: OptimizationResult, *, opt_id: int | None = None, cluster: Cluster | None = None) -> Analysis:
    """Peaks, plateaus, slices and failed runs of one optimization. A part that cannot be computed says so with its reason."""
    surf = surface(result)
    jobs: dict[str, Callable[[], Any]] = {"peaks": lambda: isolated_peaks(surf), "plateaus": lambda: plateaus(surf, cluster=cluster),
                                          "slices": lambda: slices(surf), "failed": lambda: failed(result)}
    parts: dict[str, Part] = {}
    for name in PARTS:
        try:
            parts[name] = Part("ok", jobs[name]())
        except SearchError as exc:
            parts[name] = Part("missing", None, str(exc))                       # every message starts with its stable code
        except (BadInput, ValueError) as exc:
            parts[name] = Part("missing", None, f"bad_input: {exc}")
    missing = tuple(f"analysis_part_missing:{n}" for n in PARTS if parts[n].status == "missing")
    return Analysis(opt_id, result.strategy_id, result.objective, parts, result.edge_of_space, missing)


def analyze(opt_id: int, *, db: Database, cluster: Cluster | None = None) -> Analysis:
    """`analyze(opt_id)` of a saved optimization (optimize.save): the four parts, and a warning for each one that is missing."""
    return analyze_result(load(db, opt_id), opt_id=opt_id, cluster=cluster)

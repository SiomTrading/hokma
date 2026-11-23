"""Series accessor and summary of the price statistics (SPEC-017, metrics module).

`install()` hangs one accessor, `series.quant`, on pandas Series. It calls the registry, never a library
directly, so blocked functions stay blocked and every call returns a QuantResult:

    returns.quant.sharpe(rf=0.02)
    returns.quant.information_ratio(benchmark=bench)

The libraries' own `extend_pandas` helpers are not used: they would attach their functions, defective ones
included, straight onto pandas.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.metrics import _lib
from hokma.quant.metrics.returns import metric, scalar_function

ACCESSOR_NAME = "quant"


def available() -> list[str]:
    """Short names of the metrics callable through the accessor (those that take a series)."""
    return sorted(s.name.split(".", 1)[1] for s in registry.list("metrics")
                  if "returns" in s.inputs or "prices" in s.inputs)


class MetricsAccessor:
    """`series.quant.<metric>(**params)` calls `metrics.<metric>` with the series as its first input."""

    def __init__(self, series: pd.Series) -> None:
        self._series = series

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        full = f"metrics.{name}"
        try:
            spec = registry.describe(full)
        except KeyError as exc:
            raise AttributeError(f"no metric named {name!r}; see series.quant.available()") from exc
        if spec.blocked:
            raise AttributeError(f"{full} is blocked; use {spec.replacement}")
        key = "returns" if "returns" in spec.inputs else "prices" if "prices" in spec.inputs else None
        if key is None:
            raise AttributeError(f"{full} does not take a series")

        def call(**kwargs: Any) -> Any:
            if key in kwargs:
                raise BadInput(f"{full}: the series is the accessor itself, do not pass {key!r}")
            return registry.call(full, **{key: self._series}, **kwargs)
        call.__name__ = name
        call.__doc__ = spec.summary
        return call

    def available(self) -> list[str]:
        return available()


def install() -> bool:
    """Register the accessor on pandas Series. Returns True the first time and False when it is already there."""
    if ACCESSOR_NAME in pd.Series._accessors:
        return False
    pd.api.extensions.register_series_accessor(ACCESSOR_NAME)(MetricsAccessor)
    return True


install()


def _accessor_methods() -> dict:
    blocked = sorted(s.name.split(".", 1)[1] for s in registry.list("metrics", include_blocked=True) if s.blocked)
    return {"accessor": ACCESSOR_NAME, "methods": available(), "blocked": blocked}


accessor_methods = scalar_function(
    "metrics.accessor_methods", piece_ids=("QS-060",), unit="table", method="registry listing of the metrics reachable as series.quant.<name>",
    summary="Metrics available as methods of the pandas accessor, and the library functions blocked behind it.",
    inputs={}, compute=_accessor_methods)


def _calc_stats(s: pd.Series, b: object, p: dict) -> pd.Series:
    stats = _lib.ffn.calc_stats(s).stats
    stats = stats.copy()
    for key in ("start", "end"):
        if key in stats.index:
            stats[key] = str(pd.Timestamp(stats[key]).date())
    return stats


calc_stats_prices = metric(
    "metrics.calc_stats_prices", input_label="prices", piece_ids=("FF-024",), unit="table",
    method="performance table of a price series: returns by horizon, daily, monthly and yearly moments, drawdowns",
    summary="Performance statistics table of a price series (the one-call summary of the accessor).", compute=_calc_stats, min_obs=5)


def _compat_info() -> dict:
    return {"versions": _lib.versions(), "resample_aliases": dict(_lib.RESAMPLE_ALIASES), "period_labels": dict(_lib.PERIOD_LABELS)}


compat_info = scalar_function(
    "metrics.compat_info", piece_ids=("QS-061",), unit="table", method="versions and frequency aliases of the compatibility layer",
    summary="Versions of the numerical stack and the pandas frequency aliases the metrics module uses.",
    inputs={}, compute=_compat_info)

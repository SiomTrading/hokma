"""Metrics module (SPEC-017, section 4.4).

Public entry points: compute, summary, report_html, psr. Every function of the module is registered as "metrics.<name>" in
hokma.quant.registry, which is how agents reach it. Importing this package registers all of them.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.metrics import (  # noqa: F401  (importing registers the functions)
    accessor,
    attribution,
    benchmark as _benchmark_module,
    distribution,
    drawdown,
    locks,
    montecarlo,
    ratios,
    report,
    returns as _returns_module,
    summary as _summary_module,
    tail_view,
    trades,
    volatility,
)
from hokma.quant.types import QuantResult


def compute(returns: pd.Series, names: list[str], *, benchmark: pd.Series | None = None, rf: float = 0.0,
            periods_per_year: int = 252) -> dict[str, QuantResult]:
    """Run the named metrics over one return series. Arguments a metric does not take are not passed to it."""
    out: dict[str, QuantResult] = {}
    wanted = {"returns": returns, "benchmark": benchmark, "rf": rf, "periods_per_year": periods_per_year}
    for name in names:
        full = name if name.startswith("metrics.") else f"metrics.{name}"
        spec = registry.describe(full)
        if spec.blocked:
            raise registry.BlockedFunction(full, spec.replacement)
        accepted = {k: v for k, v in wanted.items() if k in spec.inputs and v is not None}
        if "returns" not in spec.inputs:
            raise BadInput(f"{full} does not take a return series; call it through the registry")
        out[name] = registry.call(full, **accepted)
    return out


def summary(returns: Any, *, benchmark: Any = None, rf: float = 0.0) -> dict[str, QuantResult]:
    """The full table, block by block (header, returns, ratios, probabilistic Sharpe, drawdown, volatility, ...)."""
    return _summary_module.build_summary(returns, benchmark=benchmark, rf=rf)


def report_html(returns: Any, *, benchmark: Any = None, title: str) -> str:
    """One HTML file with inline charts and no external resources. Raises BadInput when the series cannot be reported."""
    text, warnings = report.render_html(returns, benchmark, title)
    if text is None:
        raise BadInput("; ".join(warnings) or "the report could not be built")
    return text


def psr(returns: Any, *, sr_benchmark: float = 0.0) -> QuantResult:
    """Probabilistic Sharpe ratio: delegates to validation.psr (the only one; the library versions are blocked)."""
    return locks.psr(returns, sr_benchmark=sr_benchmark)


__all__ = ["compute", "summary", "report_html", "psr"]

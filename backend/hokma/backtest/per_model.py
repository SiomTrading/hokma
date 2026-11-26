"""One backtest per alpha model (SPEC-020: AH-37).

Each registered alpha is a view of the market and nothing more: to be run it is put in a strategy (sizer, exit rule, execution) by the
caller, run on its own spec and judged on its own Sharpe. The result of each one carries the Sharpe with its probabilistic Sharpe ratio (PSR,
from `hokma.quant.validation`) and the mark `below_realistic_expectation` when the Sharpe is under what a strategy of its kind can be
expected to keep once it is traded.

The expectation is a parameter. Its default is the realistic reference of the judge of the desks (`REPLICATION_REFERENCE` of
`hokma.desks.agents.judge`: the median Sharpe ratio, 0.37, of the published replication studies that the source row AS-001 cites, a figure that
the third party declares itself, with medium confidence): one figure, owned by the judge, read here and never copied. A Sharpe of 0.33 is below
it: that is the example of the row, and it is not a strategy to operate.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from hokma.backtest import results, runner
from hokma.backtest.results import BacktestResult
from hokma.backtest.spec import BacktestSpec, SpecError
from hokma.db import Database
from hokma.factory.parts import build_composite
from hokma.factory.registry import Registry
from hokma.quant import limits, validation

BELOW = "below_realistic_expectation"
MAX_ALPHAS = 200


@dataclass(frozen=True)
class ModelResult:
    alpha_id: str
    result: BacktestResult
    sharpe: float | None                 # of the daily returns of the curve, from `hokma.quant.metrics`
    psr: float | None                    # the probability that the true Sharpe is above zero, from `hokma.quant.validation`
    below_realistic_expectation: bool
    warnings: tuple[str, ...]


def realistic_sharpe() -> float:
    """The realistic Sharpe ratio of the judge of the desks (AS-001). It is read when asked, so the judge stays the only owner of the figure."""
    from hokma.desks.agents.judge import REPLICATION_REFERENCE      # imported here: the desks package loads the model layer

    return float(REPLICATION_REFERENCE["median_sharpe"])


def below_realistic_expectation(sharpe: float | None, threshold: float | None = None) -> bool:
    """Whether a Sharpe ratio is under the realistic expectation (the judge's figure unless `threshold` is given). A Sharpe that could not be
    computed is not judged here: it is not below anything, and the result carries its own warning."""
    return sharpe is not None and sharpe < (realistic_sharpe() if threshold is None else threshold)


def _number(block: Any) -> float | None:
    value = getattr(block, "value", None)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def judge(alpha_id: str, result: BacktestResult, *, realistic_sharpe: float | None = None) -> ModelResult:
    """The Sharpe and the PSR of one run and the mark against the realistic expectation."""
    sharpe = _number(result.stats.get("sharpe"))
    psr = None
    if result.status != "failed" and result.equity.timestamps:
        try:
            psr = _number(validation.psr(results.equity_returns(result.equity)))
        except Exception:                          # noqa: BLE001 - a PSR that cannot be computed is None with the Sharpe still reported
            psr = None
    below = below_realistic_expectation(sharpe, realistic_sharpe)
    notes = (BELOW,) if below else ()
    if sharpe is None:
        notes += ("sharpe_unavailable",)
    return ModelResult(alpha_id, result, sharpe, psr, below, tuple(dict.fromkeys((*result.warnings, *notes))))


def run_per_alpha(spec: BacktestSpec, make_strategy: Callable[[str, Any], Any] | None = None, *, registry: Registry | None = None, alpha_ids: Sequence[str] | None = None,
                  realistic_sharpe: float | None = None, source: runner.Source | None = None, fetch: Callable[..., Any] | None = None,
                  db: Database | None = None, curve_dir: Path | None = None) -> tuple[ModelResult, ...]:
    """A backtest for each alpha of the registry (or of `alpha_ids`), in the order of their ids. `make_strategy(alpha_id, alpha)` puts the alpha in
    a strategy that trades the instrument of `spec`; without it the alpha goes in the composite of the factory with its defaults (one contract, no
    stop, market entries; the indicators it asks for come from H3). Each run is a run of its own, saved with the alpha as its strategy, and counts as a
    trial of that alpha. A run that fails is returned with its failure, not raised."""
    book = registry or Registry.with_builtins()
    wanted = sorted(e.id for e in book.entries.values() if e.kind == "alpha") if alpha_ids is None else list(alpha_ids)
    limits.check_param("n_packages", len(wanted))
    if not wanted or len(wanted) > MAX_ALPHAS or len(set(wanted)) != len(wanted):
        raise SpecError(f"bad_spec: one to {MAX_ALPHAS} different alphas")
    out = []
    for alpha_id in wanted:
        entry = book.get(alpha_id)
        if entry.kind != "alpha":
            raise SpecError(f"bad_spec: {alpha_id} is a {entry.kind}, not an alpha")
        own = replace(spec, strategy_id=alpha_id, params={})                 # the alpha is built with its defaults: the strategy that wraps it has no parameters
        alpha = book.build(alpha_id)
        strategy = make_strategy(alpha_id, alpha) if make_strategy is not None else build_composite(
            alpha, sheet=runner.sheet_of(spec.instruments[0], spec.end.date()), desk_id=spec.desk_id or 0, line=spec.line, tag=alpha_id[:48])
        result = runner.run(own, strategy=strategy, source=source, fetch=fetch, db=db, curve_dir=curve_dir,
                            hypothesis_id=alpha_id)
        out.append(judge(alpha_id, result, realistic_sharpe=realistic_sharpe))
    return tuple(out)


__all__ = ["BELOW", "ModelResult", "below_realistic_expectation", "judge", "realistic_sharpe", "run_per_alpha"]

"""Audit of a backtest result in six phases and 24 checks (SPEC-020: ECT-006).

The audit reads a finished run (`BacktestResult` and its `BacktestSpec`) and the facts that the run alone cannot tell (how many ideas were
tried, whether the universe is free of survivorship, ...). Each check ends in one status:

  pass     the evidence is there and it holds
  fail     the evidence is there and it does not hold
  warn     the evidence is there and it is concerning without being disqualifying
  unknown  nobody gave the evidence: an unknown check is never a pass
  na       the check does not apply to this run (for example borrow costs of a run with no short trade)

The verdict is no_go when any check fails, needs_evidence when any is unknown, review when any is a warning, and go otherwise.
Every limit (PBO, DSR, history, sensitivity) is in `AuditConfig`, which has no defaults: the owner of the gate states them.

The audited fill is the one of the house: decided at the close of a bar, filled at the next open one tick against. A bound on the best
Sharpe of N skill-less trials is a number of standard deviations: it is divided by the square root of the years to be an annual Sharpe.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from hokma.backtest.results import BacktestResult
from hokma.backtest.spec import BacktestSpec
from hokma.factory.params import ParamSchema
from hokma.quant.errors import BadInput
from hokma.quant.limits import as_int
from hokma.quant.types import QuantResult

PHASES = ("data_integrity", "specification", "execution", "statistics", "overfitting", "reporting")
Status = Literal["pass", "fail", "warn", "unknown", "na"]
VALIDATION_SCHEMES = ("walk_forward", "cpcv")
MAX_NEIGHBORS = 128


@dataclass(frozen=True)
class CheckItem:
    number: int
    phase: str
    key: str
    text: str


# The 24 checks, in the order they are read. Each key names the rule that decides it in `_RULES`.
CHECKLIST: tuple[CheckItem, ...] = (
    CheckItem(1, "data_integrity", "point_in_time", "Every input is the value known at the time, never a later revision."),
    CheckItem(2, "data_integrity", "survivorship_free", "The universe holds the names that were delisted or failed, not only the survivors."),
    CheckItem(3, "data_integrity", "corporate_actions", "Splits, dividends and contract rolls are applied and documented."),
    CheckItem(4, "data_integrity", "no_leakage", "No feature, label or statistic reads a row after the one it describes."),
    CheckItem(5, "data_integrity", "data_gaps", "Missing bars, stale prices and outlier returns were looked for and handled."),
    CheckItem(6, "specification", "trials_counted", "The number of ideas, parameter sets and variants tried is known, failed ones included."),
    CheckItem(7, "specification", "in_sample_tuning", "Every parameter was chosen on in-sample data only."),
    CheckItem(8, "specification", "holdout_untouched", "The holdout was kept apart during development and read once."),
    CheckItem(9, "specification", "history_length", "The history is long enough: years or closed trades above the stated minimum."),
    CheckItem(10, "execution", "costs_charged", "Commission and slippage were charged on every trade."),
    CheckItem(11, "execution", "impact_modeled", "Market impact is modeled, or shown not to matter at this size."),
    CheckItem(12, "execution", "financing_and_borrow", "Financing of leverage and the borrow cost of shorts are included."),
    CheckItem(13, "execution", "house_fill", "Orders decided at a close fill at the next open one tick against; the stop wins a tie; a gap fills at the open."),
    CheckItem(14, "execution", "capacity", "The size traded is a small share of what the market trades."),
    CheckItem(15, "statistics", "validation_scheme", "A walk-forward or combinatorial purged split was used, not one in-sample and out-of-sample cut."),
    CheckItem(16, "statistics", "multiple_testing", "A correction for the number of trials was applied when more than one was tried."),
    CheckItem(17, "statistics", "chance_hurdle", "The annual Sharpe is above the best that skill-less trials reach for this many trials and years."),
    CheckItem(18, "statistics", "deflated_sharpe", "The deflated Sharpe probability is above the stated minimum."),
    CheckItem(19, "overfitting", "pbo", "The probability of backtest overfitting is below the stated limits."),
    CheckItem(20, "overfitting", "parameter_neighbors", "Parameters moved by the stated share keep most of the performance."),
    CheckItem(21, "overfitting", "regimes", "The result holds in most market regimes, not in one."),
    CheckItem(22, "overfitting", "red_flags", "No red flag: an absurd Sharpe, a result carried by a few trades."),
    CheckItem(23, "reporting", "gross_and_net", "Gross and net results are both reported, with the cost split."),
    CheckItem(24, "reporting", "drawdown_analysis", "Drawdown depth and duration are reported."),
)


@dataclass(frozen=True)
class PboThresholds:
    """Between `warn` and `reject` the PBO check warns; at `reject` or above it fails. Both are chosen by the owner of the gate."""

    warn: float
    reject: float

    def __post_init__(self) -> None:
        if not (isinstance(self.warn, (int, float)) and isinstance(self.reject, (int, float)) and 0.0 < self.warn <= self.reject < 1.0):
            raise BadInput("PBO thresholds need 0 < warn <= reject < 1")


@dataclass(frozen=True)
class AuditConfig:
    pbo: PboThresholds
    min_years: float                    # history needed unless min_trades closed trades exist
    min_trades: int
    min_dsr: float                      # smallest deflated Sharpe probability that passes
    max_neighbor_drop: float            # largest share of the base Sharpe a neighbor may lose
    neighbor_spread: float              # how far each parameter moves, as a share of its value
    min_regime_share: float             # share of regimes with a positive Sharpe
    red_flag_sharpe: float              # an annual Sharpe above this is checked as a likely bug
    max_top_trade_share: float          # share of the total profit one trade may carry
    min_oos_to_is: float                # smallest ratio of out-of-sample to in-sample Sharpe
    max_cost_share: float | None = None  # largest share of the gross Sharpe the costs may take; None leaves that limit off (SPEC-032)

    def __post_init__(self) -> None:
        unit = (self.min_dsr, self.max_neighbor_drop, self.neighbor_spread, self.min_regime_share, self.max_top_trade_share, self.min_oos_to_is)
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0.0 < v <= 1.0 for v in unit):
            raise BadInput("min_dsr, max_neighbor_drop, neighbor_spread, min_regime_share, max_top_trade_share and min_oos_to_is are in (0, 1]")
        if not (self.min_years > 0 and as_int("min_trades", self.min_trades) >= 1 and self.red_flag_sharpe > 0):
            raise BadInput("min_years, min_trades and red_flag_sharpe are positive")
        if self.max_cost_share is not None and not (isinstance(self.max_cost_share, (int, float)) and not isinstance(self.max_cost_share, bool)
                                                    and 0.0 < self.max_cost_share <= 1.0):
            raise BadInput("max_cost_share is in (0, 1]")


@dataclass(frozen=True)
class AuditEvidence:
    """What the run cannot say about itself. None means nobody said: the check is unknown."""

    n_trials: int | None = None
    point_in_time: bool | None = None
    survivorship_free: bool | None = None
    corporate_actions: bool | None = None
    leak_checked: bool | None = None
    gaps_checked: bool | None = None
    in_sample_tuning: bool | None = None
    holdout_untouched: bool | None = None
    impact_modeled: bool | None = None
    borrow_costs_modeled: bool | None = None
    capacity_checked: bool | None = None
    validation_scheme: str | None = None
    multiple_testing_applied: bool | None = None
    dsr: float | None = None
    pbo: float | None = None
    is_sharpe: float | None = None
    oos_sharpe: float | None = None
    neighbor_sharpes: Mapping[str, float] | None = None     # annual Sharpe of each moved-parameter run, by label
    regime_sharpes: Mapping[str, float] | None = None       # annual Sharpe in each regime, by name
    cost_sharpe: float | None = None                        # what the costs take, in units of annual Sharpe (SPEC-032)
    gross_sharpe: float | None = None                       # annual Sharpe before costs
    edge_of_space: tuple[str, ...] | None = None            # parameters whose best value sits on the border of the grid
    uses_regimes: bool | None = None                        # the strategy declares it reads market regimes; None keeps the check as it was


@dataclass(frozen=True)
class CheckResult:
    number: int
    key: str
    text: str
    status: Status
    note: str


@dataclass(frozen=True)
class PhaseReport:
    phase: str
    items: tuple[CheckResult, ...]

    @property
    def status(self) -> Status:
        statuses = {i.status for i in self.items}
        for worst in ("fail", "unknown", "warn"):
            if worst in statuses:
                return worst   # type: ignore[return-value]
        return "pass" if "pass" in statuses else "na"


@dataclass(frozen=True)
class AuditReport:
    phases: tuple[PhaseReport, ...]
    verdict: Literal["go", "review", "needs_evidence", "no_go"]
    oos_to_is: float | None = None

    @property
    def items(self) -> tuple[CheckResult, ...]:
        return tuple(i for p in self.phases for i in p.items)

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "oos_to_is": self.oos_to_is,
                "phases": [{"phase": p.phase, "status": p.status,
                            "items": [{"number": i.number, "key": i.key, "text": i.text, "status": i.status, "note": i.note} for i in p.items]}
                           for p in self.phases]}


# ---- numbers --------------------------------------------------------------------------------------------------------

def chance_sd_bound(n_trials: int) -> float:
    """sqrt(2 ln N): the bound, in standard deviations of the Sharpe estimate, on the best of N trials with no skill. N = 1 gives 0."""
    n = as_int("n_trials", n_trials)
    if n < 1:
        raise BadInput("n_trials must be at least 1")
    return math.sqrt(2.0 * math.log(n))


def chance_hurdle_annual(n_trials: int, years: float) -> float:
    """The bound above as an annual Sharpe: divided by the square root of the years observed (2.15 over 14 years is 0.57 a year)."""
    if isinstance(years, bool) or not isinstance(years, (int, float)) or not math.isfinite(years) or years <= 0:
        raise BadInput(f"years must be a positive number, got {years!r}")
    return chance_sd_bound(n_trials) / math.sqrt(float(years))


@dataclass(frozen=True)
class Neighbor:
    label: str                         # e.g. "fast-" or "slow+"
    params: Mapping[str, Any]


def parameter_neighbors(params: Mapping[str, Any], spread: float, schema: ParamSchema | None = None) -> list[Neighbor]:
    """The parameter sets one step away: each number moved down and up by `spread` of its value, one at a time.

    Whole numbers are rounded to the nearest whole number; with a schema each value is held inside its range. A move that gives back the
    base value is left out.
    """
    if isinstance(spread, bool) or not isinstance(spread, (int, float)) or not 0.0 < float(spread) < 1.0:
        raise BadInput("spread is a number between 0 and 1")
    known = {p.name: p for p in schema.params} if schema is not None else {}
    out: list[Neighbor] = []
    for name in sorted(params):
        value = params[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        for sign, label in ((-1, "-"), (1, "+")):
            moved: Any = value * (1.0 + sign * float(spread))
            if isinstance(value, int):
                moved = int(math.floor(moved + 0.5))
            spec = known.get(name)
            if spec is not None:
                if spec.low is not None:
                    moved = max(moved, int(spec.low) if isinstance(value, int) else float(spec.low))
                if spec.high is not None:
                    moved = min(moved, int(spec.high) if isinstance(value, int) else float(spec.high))
            if moved != value:
                out.append(Neighbor(f"{name}{label}", {**params, name: moved}))
    if len(out) > MAX_NEIGHBORS:
        raise BadInput(f"more than {MAX_NEIGHBORS} neighbors")
    return out


# ---- the rules ------------------------------------------------------------------------------------------------------

@dataclass
class _Facts:
    result: BacktestResult
    spec: BacktestSpec
    evidence: AuditEvidence
    config: AuditConfig
    years: float
    closed: list = field(default_factory=list)
    sharpe: float | None = None
    dsr: float | None = None


def _flag(value: bool | None, ok: str, bad: str) -> tuple[Status, str]:
    if value is None:
        return "unknown", "no evidence was given"
    return ("pass", ok) if value is True else ("fail", bad)


def _years(result: BacktestResult) -> float:
    stamps = result.equity.timestamps
    return (stamps[-1] - stamps[0]).total_seconds() / (365.25 * 86400.0) if len(stamps) > 1 else 0.0


def _stat(result: BacktestResult, key: str) -> float | None:
    block = result.stats.get(key)
    value = block.value if isinstance(block, QuantResult) else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _costs(f: _Facts) -> Mapping[str, Decimal]:
    return f.result.costs_usd


def _rule_trials(f: _Facts) -> tuple[Status, str]:
    n = f.evidence.n_trials
    if n is None:
        return "unknown", "the number of trials was not given"
    return ("pass", f"{n} trial(s) counted") if as_int("n_trials", n) >= 1 else ("fail", "the count of trials is below 1")


def _rule_history(f: _Facts) -> tuple[Status, str]:
    ok = f.years >= f.config.min_years or len(f.closed) >= f.config.min_trades
    return ("pass" if ok else "fail"), f"{f.years:.2f} years and {len(f.closed)} closed trades; needs {f.config.min_years} years or {f.config.min_trades} trades"


def _rule_costs(f: _Facts) -> tuple[Status, str]:
    if not f.closed:
        return "na", "the run has no closed trade"
    commission, slippage = _costs(f).get("commission", Decimal(0)), _costs(f).get("slippage", Decimal(0))
    if commission > 0 and slippage > 0:
        ev, limit = f.evidence, f.config.max_cost_share
        if limit is not None and ev.cost_sharpe is not None and ev.gross_sharpe is not None and ev.gross_sharpe > 0:
            share = ev.cost_sharpe / ev.gross_sharpe
            if share > limit:
                return "fail", f"cost_share: costs take {share:.0%} of the gross Sharpe; the limit is {limit:.0%}"
        return "pass", f"commission {commission} and slippage {slippage} USD charged"
    return "fail", f"no_costs: commission {commission} and slippage {slippage} USD: both must be above zero when trades exist"


def _rule_impact(f: _Facts) -> tuple[Status, str]:
    if f.spec.costs.impact != "none" or f.evidence.impact_modeled is True:
        return "pass", f"impact model {f.spec.costs.impact}" if f.spec.costs.impact != "none" else "impact shown not to matter"
    if f.evidence.impact_modeled is False:
        return "fail", "no impact model and no argument that it does not matter"
    return "unknown", "no impact model in the spec and no evidence about it"


def _rule_financing(f: _Facts) -> tuple[Status, str]:
    shorts = any(t.side == "short" for t in f.closed)
    swap = _costs(f).get("swap", Decimal(0))
    if not shorts and not f.spec.costs.cfd_swap:
        return "na", "no short trade and no financed instrument"
    if swap > 0 or f.evidence.borrow_costs_modeled is True:
        return "pass", f"swap {swap} USD charged" if swap > 0 else "borrow cost documented"
    if f.evidence.borrow_costs_modeled is False:
        return "fail", "short or financed trades with no financing cost"
    return "unknown", "short or financed trades and no evidence of the financing cost"


def _rule_fill(f: _Facts) -> tuple[Status, str]:
    fill = f.spec.fill
    house = (fill.next_open_ticks >= 1 and not fill.trade_on_close and fill.stop_first and fill.gap_fills_at_open and fill.model == "one_tick"
             and f.spec.costs.slippage_ticks == fill.next_open_ticks)
    if house:
        return "pass", f"next open plus {fill.next_open_ticks} tick(s) against, stop first, gap at the open"
    return "fail", "the fill is not the house fill (next open plus one tick, stop first, gap at the open, one tick model)"


def _rule_scheme(f: _Facts) -> tuple[Status, str]:
    scheme = f.evidence.validation_scheme
    if scheme is None:
        return "unknown", "the validation scheme was not given"
    return ("pass", scheme) if scheme in VALIDATION_SCHEMES else ("fail", f"{scheme!r} is not one of {', '.join(VALIDATION_SCHEMES)}")


def _rule_multiple_testing(f: _Facts) -> tuple[Status, str]:
    n = f.evidence.n_trials
    if n is None:
        return "unknown", "the number of trials was not given"
    if n == 1:
        return "na", "one trial: nothing to correct"
    return _flag(f.evidence.multiple_testing_applied, f"corrected for {n} trials", f"{n} trials and no correction")


def _rule_hurdle(f: _Facts) -> tuple[Status, str]:
    n = f.evidence.n_trials
    if n is None or f.sharpe is None or f.years <= 0:
        return "unknown", "needs the number of trials, the annual Sharpe of the run and its length"
    hurdle = chance_hurdle_annual(n, f.years)
    note = f"annual Sharpe {f.sharpe:.3f} against {hurdle:.3f} (sqrt(2 ln {n}) = {chance_sd_bound(n):.3f} over sqrt({f.years:.2f}) years)"
    return ("pass" if f.sharpe > hurdle else "fail"), note


def _rule_dsr(f: _Facts) -> tuple[Status, str]:
    if f.dsr is None:
        return "unknown", "no deflated Sharpe probability"
    return ("pass" if f.dsr >= f.config.min_dsr else "fail"), f"DSR {f.dsr:.3f} against the minimum {f.config.min_dsr}"


def _rule_pbo(f: _Facts) -> tuple[Status, str]:
    pbo, limits = f.evidence.pbo, f.config.pbo
    if pbo is None:
        return "unknown", "no PBO"
    if pbo >= limits.reject:
        return "fail", f"PBO {pbo:.3f} is at or above {limits.reject}"
    return ("warn", f"PBO {pbo:.3f} is at or above {limits.warn}") if pbo >= limits.warn else ("pass", f"PBO {pbo:.3f} is below {limits.warn}")


def _rule_neighbors(f: _Facts) -> tuple[Status, str]:
    neighbors = f.evidence.neighbor_sharpes
    if not neighbors or f.sharpe is None:
        return "unknown", "needs the base Sharpe and the Sharpe of each moved-parameter run"
    if f.sharpe <= 0:
        return "fail", "the base Sharpe is not positive"
    worst_label, worst = min(neighbors.items(), key=lambda kv: kv[1])
    drop = (f.sharpe - worst) / f.sharpe
    note = f"worst neighbor {worst_label} loses {drop:.1%} of the base Sharpe; the limit is {f.config.max_neighbor_drop:.0%}"
    if drop > f.config.max_neighbor_drop:
        return "fail", note
    if f.evidence.edge_of_space:
        return "warn", f"edge: best point on the border of: {', '.join(f.evidence.edge_of_space)}; {note}"
    return "pass", note


def _rule_regimes(f: _Facts) -> tuple[Status, str]:
    if f.evidence.uses_regimes is False:
        return "na", "the strategy does not use market regimes"
    regimes = f.evidence.regime_sharpes
    if not regimes or len(regimes) < 2:
        return "unknown", "needs the Sharpe of at least two regimes"
    share = sum(1 for v in regimes.values() if v > 0) / len(regimes)
    return ("pass" if share >= f.config.min_regime_share else "fail"), f"{share:.0%} of {len(regimes)} regimes have a positive Sharpe; the minimum is {f.config.min_regime_share:.0%}"


def _rule_red_flags(f: _Facts) -> tuple[Status, str]:
    flags = []
    if f.sharpe is not None and f.sharpe > f.config.red_flag_sharpe:
        flags.append(f"sharpe_high: annual Sharpe {f.sharpe:.2f} is above {f.config.red_flag_sharpe}")
    profits = sorted((t.pnl_usd for t in f.closed if t.pnl_usd is not None and t.pnl_usd > 0), reverse=True)
    if profits and sum(profits) > 0 and profits[0] / sum(profits) > Decimal(str(f.config.max_top_trade_share)):
        flags.append(f"top_trade: one trade carries {float(profits[0] / sum(profits)):.0%} of the profit")
    ev = f.evidence
    if ev.is_sharpe is not None and ev.oos_sharpe is not None and ev.is_sharpe > 0 and ev.oos_sharpe / ev.is_sharpe < f.config.min_oos_to_is:
        flags.append(f"oos_ratio: out-of-sample Sharpe is {ev.oos_sharpe / ev.is_sharpe:.0%} of in-sample; the minimum is {f.config.min_oos_to_is:.0%}")
    if f.sharpe is None and not f.closed:
        return "unknown", "no Sharpe and no closed trade to look at"
    return ("fail", "; ".join(flags)) if flags else ("pass", "no red flag found")


def _rule_gross_net(f: _Facts) -> tuple[Status, str]:
    needed = ("commission", "slippage", "swap", "impact")
    missing = [k for k in needed if k not in f.result.costs_usd]
    return ("fail", f"the cost split lacks {', '.join(missing)}") if missing else ("pass", "net result with the four costs split")


def _rule_drawdown(f: _Facts) -> tuple[Status, str]:
    present = [k for k in ("drawdown_duration", "max_drawdown", "drawdown") if k in f.result.stats]
    return ("pass", f"reported: {', '.join(present)}") if present else ("unknown", "the stats hold no drawdown block")


_RULES = {
    "point_in_time": lambda f: _flag(f.evidence.point_in_time, "point in time data", "later revisions are in the data"),
    "survivorship_free": lambda f: _flag(f.evidence.survivorship_free, "the universe holds delisted names", "survivors only"),
    "corporate_actions": lambda f: _flag(f.evidence.corporate_actions, "corporate actions applied", "corporate actions not applied"),
    "no_leakage": lambda f: _flag(f.evidence.leak_checked, "no feature reads the future", "a feature reads the future"),
    "data_gaps": lambda f: _flag(f.evidence.gaps_checked, "gaps, stale prices and outliers handled", "data problems not handled"),
    "trials_counted": _rule_trials,
    "in_sample_tuning": lambda f: _flag(f.evidence.in_sample_tuning, "tuned on in-sample data only", "tuned on data it was tested on"),
    "holdout_untouched": lambda f: _flag(f.evidence.holdout_untouched, "holdout kept apart", "the holdout was used during development"),
    "history_length": _rule_history,
    "costs_charged": _rule_costs,
    "impact_modeled": _rule_impact,
    "financing_and_borrow": _rule_financing,
    "house_fill": _rule_fill,
    "capacity": lambda f: _flag(f.evidence.capacity_checked, "capacity checked", "capacity not checked"),
    "validation_scheme": _rule_scheme,
    "multiple_testing": _rule_multiple_testing,
    "chance_hurdle": _rule_hurdle,
    "deflated_sharpe": _rule_dsr,
    "pbo": _rule_pbo,
    "parameter_neighbors": _rule_neighbors,
    "regimes": _rule_regimes,
    "red_flags": _rule_red_flags,
    "gross_and_net": _rule_gross_net,
    "drawdown_analysis": _rule_drawdown,
}


def verdict_for(statuses: Iterable[Status]) -> Literal["go", "review", "needs_evidence", "no_go"]:
    """The verdict of a set of check statuses: no_go when any fails, needs_evidence when any is unknown, review when any warns, go otherwise.

    The one place of this precedence: the audit uses it, and so does the verdict that leaves some checks out (SPEC-032)."""
    seen = set(statuses)
    return "no_go" if "fail" in seen else "needs_evidence" if "unknown" in seen else "review" if "warn" in seen else "go"


def audit(result: BacktestResult, spec: BacktestSpec, evidence: AuditEvidence, config: AuditConfig) -> AuditReport:
    """Run the 24 checks in six phases over a finished result. The run itself is never changed."""
    if result.status != "done":
        raise BadInput(f"only a finished run can be audited, this one is {result.status!r}")
    facts = _Facts(result, spec, evidence, config, _years(result), [t for t in result.trades if t.pnl_usd is not None],
                   _stat(result, "sharpe"), evidence.dsr if evidence.dsr is not None else _stat(result, "dsr"))
    phases = []
    for phase in PHASES:
        items = []
        for item in (c for c in CHECKLIST if c.phase == phase):
            status, note = _RULES[item.key](facts)
            items.append(CheckResult(item.number, item.key, item.text, status, note))
        phases.append(PhaseReport(phase, tuple(items)))
    verdict = verdict_for(i.status for p in phases for i in p.items)
    ratio = None
    if evidence.is_sharpe and evidence.oos_sharpe is not None and evidence.is_sharpe > 0:
        ratio = evidence.oos_sharpe / evidence.is_sharpe
    return AuditReport(tuple(phases), verdict, ratio)   # type: ignore[arg-type]


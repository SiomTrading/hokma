"""Practice book of setup candidates (SPEC-018: CTS-019). Two tools in one module:

* the candidate book: for each candidate, the outcome after 3 and 5 trading days with MFE, MAE, the stop and the target, and a label;
* blind replay scoring: the own decision (enter, wait or pass) is scored for rule adherence and for result in separate axes, which gives four
  quadrants (followed or deviated, favorable or adverse).

Rules that make it honest:
* costs are subtracted: the round trip cost (percent of the entry) lowers the forward return and the favorable excursion and worsens the adverse
  excursion before any label threshold is read;
* if the stop and the target are touched in the same bar, the stop wins;
* a gap through the stop exits at the open, not at the stop;
* a case captured on or after the date of the first later bar is a late capture and is left out of the tallies;
* a case without enough later bars is pending, not scored;
* with fewer than 20 scored cases nothing is suggested.
Nothing here promotes a rule; a suggestion is a proposal for the owner.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from hokma.exec.money import ZERO, dec, text

HORIZONS = (3, 5)
MIN_CASES = 20
MAX_BARS = 5000
HUNDRED = Decimal(100)
LABELS = ("STRONG_WINNER", "WORKED", "FAILED_STOP", "FAILED_FADE", "CHOPPY_FAILURE", "NEUTRAL", "PENDING")
DECISIONS = ("enter", "wait", "pass")


class SetupBookError(ValueError):
    """A candidate, a bar or a decision that cannot be used."""


@dataclass(frozen=True)
class Thresholds:
    """Label thresholds in percent. They are data, so a line can set its own; the defaults are applied to figures that are already net of costs."""
    strong_mfe: Decimal = Decimal(12)
    strong_return: Decimal = Decimal(8)
    worked_mfe: Decimal = Decimal(6)
    worked_return: Decimal = Decimal(4)
    fade_return: Decimal = Decimal(-2)
    choppy_mae: Decimal = Decimal(-5)
    choppy_return: Decimal = Decimal(2)
    rally_return: Decimal = Decimal(4)


@dataclass(frozen=True)
class Bar:
    date: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    @classmethod
    def of(cls, day: str, o: Any, h: Any, lo: Any, c: Any) -> "Bar":
        bar = cls(str(day)[:10], dec(o), dec(h), dec(lo), dec(c))
        if bar.high < max(bar.open, bar.close, bar.low) or bar.low > min(bar.open, bar.close, bar.high) or bar.low <= 0:
            raise SetupBookError(f"bar {day} is not consistent")
        return bar


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    symbol: str
    setup_date: str
    entry: Decimal
    stop: Decimal | None
    target: Decimal | None = None
    captured_on: str | None = None
    rule_expected: str = "not_enter"          # enter or not_enter, as the rule scored it at capture time

    def __post_init__(self) -> None:
        if self.entry <= 0:
            raise SetupBookError("the entry must be positive")
        if self.stop is not None and not 0 < self.stop < self.entry:
            raise SetupBookError("a stop is below the entry (long candidates only)")
        if self.target is not None and self.target <= self.entry:
            raise SetupBookError("a target is above the entry")
        if self.rule_expected not in ("enter", "not_enter"):
            raise SetupBookError("rule_expected is enter or not_enter")


@dataclass(frozen=True)
class Outcome:
    horizon: int
    matured: bool
    label: str
    forward_return_pct: Decimal | None = None
    mfe_pct: Decimal | None = None
    mae_pct: Decimal | None = None
    stop_hit: bool = False
    target_hit: bool = False
    exit_date: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"horizon": self.horizon, "matured": self.matured, "label": self.label, "forward_return_pct": text(self.forward_return_pct),
                "mfe_pct": text(self.mfe_pct), "mae_pct": text(self.mae_pct), "stop_hit": self.stop_hit, "target_hit": self.target_hit,
                "exit_date": self.exit_date, "reason": self.reason}


def later_bars(bars: Sequence[Bar], setup_date: str) -> list[Bar]:
    if len(bars) > MAX_BARS:
        raise SetupBookError("too many bars")
    return sorted((b for b in bars if b.date > setup_date), key=lambda b: b.date)


def label_for(forward: Decimal, mfe: Decimal, mae: Decimal, *, stop_hit: bool, th: Thresholds = Thresholds()) -> str:
    if stop_hit:
        return "FAILED_STOP"
    if mfe >= th.strong_mfe or forward >= th.strong_return:
        return "STRONG_WINNER"
    if mfe >= th.worked_mfe or forward >= th.worked_return:
        return "WORKED"
    if forward <= th.fade_return:
        return "FAILED_FADE"
    if mae <= th.choppy_mae and forward < th.choppy_return:
        return "CHOPPY_FAILURE"
    return "NEUTRAL"


def outcome(candidate: Candidate, bars: Sequence[Bar], horizon: int, *, cost_pct: Decimal = ZERO, th: Thresholds = Thresholds()) -> Outcome:
    """The outcome of one candidate over `horizon` trading days after its setup date. `cost_pct` is the round trip cost in percent of the entry."""
    cost = dec(cost_pct, "cost_pct")
    window = later_bars(bars, candidate.setup_date)
    if len(window) < horizon:
        return Outcome(horizon, False, "PENDING", reason="not_enough_later_bars")
    entry, stop, target = candidate.entry, candidate.stop, candidate.target
    high, low, last_close, exit_price, exit_date = entry, entry, entry, None, None
    stop_hit = target_hit = False
    for bar in window[:horizon]:
        hit_stop = stop is not None and bar.low <= stop
        hit_target = target is not None and bar.high >= target
        if hit_stop:                               # stop first, also when the same bar reaches the target
            stop_hit, exit_date = True, bar.date
            exit_price = min(bar.open, stop)       # a gap through the stop exits at the open
            low, high = min(low, exit_price), max(high, bar.open)       # the high of a stop bar is not counted: its order is unknown
            break
        if hit_target:
            target_hit, exit_date, exit_price = True, bar.date, target
            low, high = min(low, bar.low), max(high, target)
            break
        high, low = max(high, bar.high), min(low, bar.low)
        last_close, exit_date = bar.close, bar.date
    final = exit_price if exit_price is not None else last_close
    forward = (final / entry - 1) * HUNDRED - cost
    mfe = (high / entry - 1) * HUNDRED - cost
    mae = (low / entry - 1) * HUNDRED - cost
    return Outcome(horizon, True, label_for(forward, max(mfe, ZERO), min(mae, ZERO), stop_hit=stop_hit, th=th), forward, mfe, mae, stop_hit, target_hit, exit_date)


def book_row(candidate: Candidate, bars: Sequence[Bar], *, cost_pct: Decimal = ZERO, th: Thresholds = Thresholds()) -> dict[str, Any]:
    """One row of the candidate book with the 3 and 5 day outcomes. The overall label is the one of the longest matured horizon."""
    outs = {h: outcome(candidate, bars, h, cost_pct=cost_pct, th=th) for h in HORIZONS}
    matured = [o for h, o in sorted(outs.items()) if o.matured]
    return {"candidate_id": candidate.candidate_id, "symbol": candidate.symbol, "setup_date": candidate.setup_date, "entry": text(candidate.entry),
            "stop": text(candidate.stop), "target": text(candidate.target), "cost_pct": text(dec(cost_pct)),
            "outcomes": {f"{h}d": o.to_dict() for h, o in outs.items()}, "overall": matured[-1].label if matured else "PENDING"}


@dataclass(frozen=True)
class Decision:
    candidate: Candidate
    decision: str
    reason: str = ""

    def __post_init__(self) -> None:
        if self.decision not in DECISIONS:
            raise SetupBookError("a decision is enter, wait or pass")


@dataclass(frozen=True)
class Scored:
    candidate_id: str
    adherence: str                  # followed or deviated
    result: str | None              # favorable or adverse; None when excluded
    quadrant: str | None
    excluded: str | None            # pending, late_capture or None
    outcome: Outcome


def score_decision(item: Decision, bars: Sequence[Bar], *, horizon: int = 5, cost_pct: Decimal = ZERO, th: Thresholds = Thresholds()) -> Scored:
    """Adherence compares the decision with what the rule expected; the result compares it with what the price did. The two never mix."""
    cand = item.candidate
    adherence = "followed" if (item.decision == "enter") == (cand.rule_expected == "enter") else "deviated"
    out = outcome(cand, bars, horizon, cost_pct=cost_pct, th=th)
    later = later_bars(bars, cand.setup_date)
    if later and cand.captured_on is not None and cand.captured_on >= later[0].date:
        return Scored(cand.candidate_id, adherence, None, None, "late_capture", out)
    if not out.matured:
        return Scored(cand.candidate_id, adherence, None, None, "pending", out)
    rallied = (not out.stop_hit) and out.forward_return_pct is not None and out.forward_return_pct >= th.rally_return
    favorable = rallied if item.decision == "enter" else not rallied
    result = "favorable" if favorable else "adverse"
    return Scored(cand.candidate_id, adherence, result, f"{adherence}_{result}", None, out)


def summarize(scored: Iterable[Scored]) -> dict[str, Any]:
    """Quadrant counts over the scored cases only (late and pending ones are counted apart) and the adherence rate."""
    rows = list(scored)
    counted = [s for s in rows if s.excluded is None]
    quadrants = Counter(s.quadrant for s in counted)
    keys = ("followed_favorable", "followed_adverse", "deviated_favorable", "deviated_adverse")
    followed = sum(quadrants[k] for k in keys[:2])
    return {"scored": len(counted), "quadrants": {k: quadrants[k] for k in keys},
            "adherence_rate_pct": text(Decimal(followed) * HUNDRED / len(counted)) if counted else None,
            "excluded": {"late_capture": sum(s.excluded == "late_capture" for s in rows), "pending": sum(s.excluded == "pending" for s in rows)}}


def suggestions(scored: Iterable[Scored], *, min_cases: int = MIN_CASES) -> dict[str, Any]:
    """Proposals for the owner. With fewer than `min_cases` scored cases the list is empty: a small sample suggests nothing."""
    summary = summarize(scored)
    out: dict[str, Any] = {"scored": summary["scored"], "enough": summary["scored"] >= min_cases, "suggestions": []}
    if not out["enough"]:
        return out
    q = summary["quadrants"]
    followed = q["followed_favorable"] + q["followed_adverse"]
    deviated = q["deviated_favorable"] + q["deviated_adverse"]
    if followed and q["followed_adverse"] * 2 > followed:
        out["suggestions"].append({"code": "review_rule", "reason": "more than half of the followed decisions had an adverse result"})
    if deviated and q["deviated_favorable"] > q["deviated_adverse"]:
        out["suggestions"].append({"code": "review_rule_gap", "reason": "deviations did better than adherence: the rule may be missing something, or those were luck"})
    if deviated * 4 > summary["scored"]:
        out["suggestions"].append({"code": "adherence", "reason": "more than a quarter of the decisions deviated from the rule"})
    return out


__all__ = ["Bar", "Candidate", "Decision", "Outcome", "Scored", "SetupBookError", "Thresholds", "book_row", "label_for", "outcome", "score_decision",
           "suggestions", "summarize"]

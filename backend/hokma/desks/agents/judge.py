"""Amos (A10), the judge of the desk tribunal (SPEC-019: B9).

What lives in code, so that it does not depend on a prompt:
- The rating scale of the line and the parser that reads a rating without a model (`extract_rating`): an ambiguous text is REVIEW.
- The anchored confidence scale of the line (`ConfidenceScale`): a band, its anchor and a justification that names the band and
  cites the snapshot. A model that always answers 75 does not validate without that justification.
- The review rubric (`rubric`): eight weighted criteria, a failure in plausibility or overfitting rejects whatever the total.
  Its pass marks are uncalibrated defaults and say so.
- The double review (`double_review`): two reviewers with the same rubric and no shared context, both must approve, three rounds
  at most, then REVIEW.
- The evidence phase: the judge may ask the validation module for the deflated Sharpe ratio, the overfitting probability and the
  purged cross validation study through `quant_call`, restricted to that module. It never computes them.
- The judgment phase has no tools at all (TA-34) and its prompt carries the phrase that says so.
- The rules that turn a verdict into a safer one: a candle signal without costs and portfolio impact is rejected, an approval needs a
  catalyst on the lines without backtest, a line that needs a backtest and has none is REVIEW, and a failed gate is REVIEW.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from hokma.desks.agents.debate import (
    MISSING, BearOutput, BullOutput, DebateLedger, JUDGE, OutputError, _guard, _list, _object, _text, check_numbers, judge_toolbox,
    render_snapshot, reports_block, resolve_ref, seat_prompt, _ask,
)
from hokma.desks.core.graph import ChatModel
from hokma.desks.core.prompts import PromptComposer
from hokma.desks.core.signal import LINES
from hokma.desks.core.state import Message, clip
from hokma.desks.core.tools import ToolCatalog, ToolNotAllowed, Toolbox, ToolSpec
from hokma.desks.plantel import SEATS, JudgeVerdict
from hokma.llm.jsonx import LLMParseError, extract_json
from hokma.quant import registry
from hokma.quant.errors import BadInput

REVIEW = "REVIEW"
NO_EXTERNAL_TOOLS = ("Use only the evidence in this prompt. You have no tools in this step: do not call, request or simulate any "
                     "external tool or data source, and do not rely on facts this prompt does not hold.")
APPROVALS = ("APPROVE", "APPROVE_WITH_CONDITIONS")
_SCALE = ("APPROVE", "APPROVE_WITH_CONDITIONS", "REJECT")
RATING_SCALES: Mapping[str, tuple[str, ...]] = MappingProxyType({line: _SCALE for line in LINES})
REQUIRES_BACKTEST = frozenset({"DT", "SWC"})
CATALYST_LINES = frozenset({"SWO", "LP"})
MAX_EVIDENCE_REQUESTS = 8
EVIDENCE_PREFIX = "validation."
_NEGATION = frozenset({"not", "no", "never", "dont", "cannot", "cant", "neither", "nor", "without"})
_FUNCTION_NAME = re.compile(r"^validation\.[a-z0-9_]{2,60}$")

# What published replications of academic results show (medium confidence, self-declared by a third party, not verified here).
REPLICATION_REFERENCE: Mapping[str, Any] = MappingProxyType({
    "median_sharpe": 0.37, "share_with_t_above_1_96": 0.48, "t_threshold": 1.96, "confidence": "medium",
    "note": "self-declared by a third party; use as a prior, not as a threshold",
})


# ---- rating parser (TA-39) -----------------------------------------------------------------------------------------

def rating_scale(line: str) -> tuple[str, ...]:
    if line not in RATING_SCALES:
        raise OutputError(f"line must be one of {LINES}")
    return RATING_SCALES[line]


def _squash(text: str) -> str:
    return "_" + re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_") + "_"


def extract_rating(text: str, scale: Sequence[str] = _SCALE) -> str:
    """The rating a text holds, read without a model. Exactly one rating of `scale` (or an explicit `rating:` label that
    agrees with itself) gives that rating; none, several that differ, a negated one or REVIEW itself give REVIEW."""
    labels = sorted({s.upper() for s in scale} | {REVIEW}, key=len, reverse=True)
    body = _squash(text)
    alternation = "|".join(re.escape(label.lower()) for label in labels)
    found = [(m.start(1), m.group(1).upper()) for m in re.finditer(rf"(?<=_)({alternation})(?=_)", body)]

    def negated(position: int) -> bool:
        before = body[:position].strip("_").split("_")[-2:]
        return any(token in _NEGATION for token in before)

    if not found:
        return REVIEW
    if any(negated(pos) for pos, _ in found):
        return REVIEW
    labelled = {m.group(1).upper() for m in re.finditer(rf"_rating_(?:is_|equals_)?({alternation})(?=_)", body)}
    chosen = labelled if labelled else {label for _, label in found}
    if len(chosen) != 1:
        return REVIEW
    return next(iter(chosen))


# ---- anchored confidence (AH-13) -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Band:
    name: str
    low: int
    high: int
    anchor: str


def _bands(decisive: str, solid: str, mixed: str, weak: str, none: str) -> tuple[Band, ...]:
    return (Band("decisive", 90, 100, decisive), Band("solid", 70, 89, solid), Band("mixed", 40, 69, mixed),
            Band("weak", 10, 39, weak), Band("none", 0, 9, none))


BANDS_BY_LINE: Mapping[str, tuple[Band, ...]] = MappingProxyType({
    "SWO": _bands("Several independent sources agree, a dated catalyst falls inside the life of the option, the volatility read supports the structure and no concern of the bear side is left unanswered.",
                  "At least two independent sources agree and a dated catalyst exists. The bear side named gaps but could not make one decisive.",
                  "The evidence points both ways or is early, or the catalyst is undated, or the contract is thin.",
                  "Little evidence for the operation, mostly from one source, with no dated catalyst.",
                  "No view: the evidence is missing or the case cannot be judged."),
    "LP": _bands("The business evidence, the valuation and a named catalyst agree across independent sources and the bear side has no open point.",
                 "Strong evidence on the business and the valuation from at least two sources, with small gaps.",
                 "Quality or valuation is supported but not both, or the catalyst is vague.",
                 "Thin evidence, or the thesis rests on one source or on a forecast.",
                 "No view: the evidence is missing."),
    "DT": _bands("The backtest holds out of sample across costs and regimes and the validation evidence agrees.",
                 "The backtest holds out of sample with small gaps in sample size or regime coverage.",
                 "The backtest is positive but short, or fragile to costs, or untested out of sample.",
                 "The backtest is weak, or the result needs many tuned parameters.",
                 "No view: no usable backtest."),
    "SWC": _bands("The backtest of the setup holds out of sample and the chart structure and the news agree.",
                  "The backtest holds with small gaps and the structure is clear.",
                  "The setup works in some regimes only, or the sample is short.",
                  "The setup rests on a few trades or a tuned threshold.",
                  "No view: no usable backtest."),
    "RF": _bands("Rates, curve and macro evidence agree across independent sources.",
                 "Strong evidence with small gaps.",
                 "Mixed signals across the curve and the macro data.",
                 "Little evidence for the view.",
                 "No view."),
})


@dataclass(frozen=True)
class ConfidenceScale:
    """The anchored scale of one line. It lives here and in the prompt: the prompt shows the anchors, this class checks them."""

    line: str

    def __post_init__(self) -> None:
        if self.line not in BANDS_BY_LINE:
            raise OutputError(f"no confidence scale for line {self.line!r}")

    @property
    def bands(self) -> tuple[Band, ...]:
        return BANDS_BY_LINE[self.line]

    def band(self, name: str) -> Band:
        for band in self.bands:
            if band.name == name:
                return band
        raise OutputError(f"band: {name!r} is not one of {[b.name for b in self.bands]}")

    def band_for(self, confidence: int) -> Band:
        for band in self.bands:
            if band.low <= confidence <= band.high:
                return band
        raise OutputError("confidence: must be a whole number from 0 to 100")

    def prompt_text(self) -> str:
        rows = "\n".join(f"- {b.name} ({b.low}-{b.high}): {b.anchor}" for b in self.bands)
        return (f"## Confidence scale of line {self.line}\nPick a band by its anchor first, then a number inside the band. Do not answer a "
                f"default number. Name the band and cite the snapshot in the justification of the band.\n{rows}")

    def as_snapshot(self) -> dict[str, dict[str, int]]:
        return {b.name: {"low": b.low, "high": b.high} for b in self.bands}

    def validate(self, confidence: Any, band: Any, justification: Any, refs: Sequence[str], snapshot: Mapping[str, Any]) -> Band:
        """The band if the confidence sits inside it and the justification names the band and cites real keys of the snapshot."""
        if isinstance(confidence, bool) or not isinstance(confidence, int) or not 0 <= confidence <= 100:
            raise OutputError("confidence: must be a whole number from 0 to 100")
        chosen = self.band(band) if isinstance(band, str) else self.band_for(confidence)
        if not chosen.low <= confidence <= chosen.high:
            raise OutputError(f"confidence {confidence} is outside the band {chosen.name} ({chosen.low}-{chosen.high})")
        text = _text(justification, "band_justification", limit=700) if justification else ""
        if len(text) < 30 or chosen.name not in text.lower():
            raise OutputError("band_justification: justify the band, name it and say which evidence puts the case there")
        if not any(resolve_ref(snapshot, r) is not MISSING for r in refs):
            raise OutputError("evidence_refs: cite at least one key that exists in the snapshot to support the band")
        return chosen


# ---- rubric (CTS-007) ------------------------------------------------------------------------------------------------------

RUBRIC_WEIGHTS: Mapping[str, int] = MappingProxyType({
    "plausibility": 20, "overfitting": 20, "sample": 15, "regime": 10, "exit": 10, "concentration": 10,
    "execution_realism": 10, "invalidation": 5,
})
CRITICAL_CRITERIA = ("plausibility", "overfitting")
assert sum(RUBRIC_WEIGHTS.values()) == 100


@dataclass(frozen=True)
class RubricThresholds:
    """The marks of the rubric. The defaults are starting values that nobody has calibrated, and the result says so."""

    pass_total: float = 70.0
    reject_total: float = 35.0
    critical_floor: float = 0.4
    calibrated: bool = False


@dataclass(frozen=True)
class RubricResult:
    total: float
    verdict: str                              # "pass", "review" or "reject"
    contributions: Mapping[str, float]
    failed_critical: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def rubric(scores: Mapping[str, Any], thresholds: RubricThresholds = RubricThresholds()) -> RubricResult:
    """Weighted total of eight criteria scored from 0 to 1. A critical criterion under its floor rejects whatever the total is."""
    if not isinstance(scores, Mapping):
        raise OutputError("scores: expected an object")
    if missing := sorted(set(RUBRIC_WEIGHTS) - set(scores)):
        raise OutputError(f"scores: missing criteria {missing}")
    if extra := sorted(set(scores) - set(RUBRIC_WEIGHTS)):
        raise OutputError(f"scores: unknown criteria {extra}")
    contributions: dict[str, float] = {}
    for name, weight in RUBRIC_WEIGHTS.items():
        value = scores[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= float(value) <= 1.0:
            raise OutputError(f"scores.{name}: a number from 0 to 1 is required")
        contributions[name] = round(weight * float(value), 4)
    total = round(sum(contributions.values()), 4)
    failed = tuple(name for name in CRITICAL_CRITERIA if float(scores[name]) < thresholds.critical_floor)
    if failed or total < thresholds.reject_total:
        verdict = "reject"
    elif total >= thresholds.pass_total:
        verdict = "pass"
    else:
        verdict = "review"
    warnings = () if thresholds.calibrated else ("pass_total, reject_total and critical_floor are uncalibrated defaults; treat a result near a mark as undecided",)
    return RubricResult(total, verdict, MappingProxyType(contributions), failed, warnings)


# ---- double review (SKL-004) ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ReviewVerdict:
    passed: bool
    issues: tuple[str, ...] = ()
    detail: Any = None


Reviewer = Callable[[str, int], ReviewVerdict]       # (subject, round number) -> verdict
MAX_REVIEW_ROUNDS = 3


@dataclass(frozen=True)
class ReviewRound:
    number: int
    first: ReviewVerdict
    second: ReviewVerdict


@dataclass(frozen=True)
class DoubleReviewResult:
    status: str                                # "approved" or "REVIEW"
    rounds: int
    subject: str
    history: tuple[ReviewRound, ...] = ()

    @property
    def approved(self) -> bool:
        return self.status == "approved"


def _safe_review(reviewer: Reviewer, subject: str, number: int) -> ReviewVerdict:
    try:
        verdict = reviewer(subject, number)
    except Exception as exc:                        # noqa: BLE001  (a reviewer that breaks is a reviewer that did not approve)
        return ReviewVerdict(False, (f"reviewer failed: {type(exc).__name__}",))
    return verdict if isinstance(verdict, ReviewVerdict) else ReviewVerdict(False, ("reviewer answered in a shape that does not validate",))


def double_review(subject: str, first: Reviewer, second: Reviewer, *, revise: Callable[[str, tuple[str, ...]], str] | None = None,
                  max_rounds: int = MAX_REVIEW_ROUNDS) -> DoubleReviewResult:
    """Two independent reviewers, same rubric, no shared context. Both must approve. After a failed round the author revises
    (`revise` gets the issues of both) and the reviewers start again from the revised subject alone. Three rounds at most,
    then REVIEW. Without `revise` nothing can change, so a failed first round is final.

    The reviewers are called with the subject and the round number only: nothing one reviewer returns reaches the other."""
    limit = min(max(int(max_rounds), 1), MAX_REVIEW_ROUNDS)
    history: list[ReviewRound] = []
    current = subject
    for number in range(1, limit + 1):
        a = _safe_review(first, current, number)
        b = _safe_review(second, current, number)
        history.append(ReviewRound(number, a, b))
        if a.passed and b.passed:
            return DoubleReviewResult("approved", number, current, tuple(history))
        if revise is None or number == limit:
            break
        try:
            current = revise(current, tuple([*a.issues, *b.issues]))
        except Exception:                           # noqa: BLE001
            break
    return DoubleReviewResult(REVIEW, len(history), current, tuple(history))


REVIEWER_BRIEF = (
    "## Reviewer mode\nYou are one of two independent reviewers. Score the subject against the rubric and answer with one JSON object "
    'and nothing else: {"scores": {"plausibility": 0-1, "overfitting": 0-1, "sample": 0-1, "regime": 0-1, "exit": 0-1, '
    '"concentration": 0-1, "execution_realism": 0-1, "invalidation": 0-1}, "issues": ["something the author can fix"]}. '
    "The code computes the total and the verdict. Do not add the numbers yourself.")


class RubricReviewer:
    """One reviewer of the double review. Every call builds its messages from scratch, so a reviewer holds no history and no
    other reviewer's text can reach it. Give each reviewer its own model."""

    def __init__(self, model: ChatModel, *, line: str, desk_id: int, as_of: datetime, thresholds: RubricThresholds = RubricThresholds(),
                 kit_root: Path | None = None, composer: PromptComposer | None = None, language: str | None = None) -> None:
        self._model = model
        self._thresholds = thresholds
        self._system = seat_prompt(JUDGE, line=line, desk_id=desk_id, as_of=as_of, kit_root=kit_root, composer=composer,
                                   language=language, extra=[REVIEWER_BRIEF])
        self._box = judge_toolbox()

    def __call__(self, subject: str, number: int) -> ReviewVerdict:
        def parse(text: str) -> tuple[RubricResult, tuple[str, ...]]:
            try:
                data = extract_json(text)
            except LLMParseError as exc:
                raise OutputError("review: the answer holds no JSON object") from exc
            if set(data) - {"scores", "issues"}:
                raise OutputError("review: expected scores and issues")
            result = rubric(data.get("scores"), self._thresholds)
            issues = tuple(_text(i, "issues[]", limit=300) for i in _list(data.get("issues"), "issues", minimum=0, maximum=12))
            return result, issues

        try:
            result, issues = _ask(self._model, self._system, f"Review round {number}. Subject:\n{subject}", parse, self._box)
        except OutputError as exc:
            return ReviewVerdict(False, (f"review did not validate: {exc}",))
        return ReviewVerdict(result.verdict == "pass", issues, result)


# ---- evidence phase (ECT-021) ------------------------------------------------------------------------------------------------

def false_positive_probability(n_tests: int, alpha: float = 0.05) -> float:
    """Chance of at least one false positive among `n_tests` independent tests at level `alpha`: 1 - (1 - alpha) ** n_tests.
    It is the disclosure number of the count of everything tried; it is not a haircut of any other metric."""
    if isinstance(n_tests, bool) or not isinstance(n_tests, int) or not 1 <= n_tests <= 1_000_000:
        raise BadInput("n_tests must be a whole number from 1 to 1000000")
    if not 0.0 < alpha < 1.0:
        raise BadInput("alpha must be between 0 and 1")
    return 1.0 - (1.0 - alpha) ** n_tests


def count_disclosure(n_tests: int) -> dict[str, Any]:
    return {"n_tests": n_tests, "false_positive_probability": round(false_positive_probability(n_tests), 6), "alpha": 0.05}


@dataclass(frozen=True)
class EvidenceRequest:
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _FUNCTION_NAME.match(self.name):
            raise OutputError(f"evidence request {self.name!r}: only functions of the validation module can be asked")


def evidence_toolbox() -> Toolbox:
    """The only tool of the judge before it judges: `quant_call`, limited to the validation module and to the registry."""
    def quant_call(name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(name, str) or not _FUNCTION_NAME.match(name):
            raise BadInput("the judge may call only functions of the validation module")
        if registry.describe(name).module != "validation":
            raise BadInput("the judge may call only functions of the validation module")
        if arguments is not None and not isinstance(arguments, Mapping):
            raise BadInput("arguments must be an object")
        return registry.call(name, **dict(arguments or {})).to_dict()

    spec = ToolSpec("quant_call", "Ask the validation module for evidence: deflated Sharpe ratio, overfitting probability, purged cross validation.",
                    {"type": "object", "properties": {"name": {"type": "string"}, "arguments": {"type": "object"}}, "required": ["name"]},
                    quant_call, example={"name": "validation.dsr"})
    seat = SEATS[JUDGE]
    catalog = ToolCatalog()
    catalog.register(spec)
    catalog.assign(seat.name, seat.tools)
    return catalog.toolbox_for(seat.name)


def gather_evidence(box: Toolbox, requests: Sequence[EvidenceRequest]) -> dict[str, Any]:
    """Ask each request of the validation module through `quant_call`. A failed request is reported, never guessed."""
    if len(requests) > MAX_EVIDENCE_REQUESTS:
        raise OutputError(f"at most {MAX_EVIDENCE_REQUESTS} evidence requests per case")
    out: dict[str, Any] = {}
    for request in requests:
        result = box.call("quant_call", {"name": request.name, "arguments": dict(request.arguments)}, agent=SEATS[JUDGE].name)
        out[request.name] = result.data if result.ok else {"error": result.error_kind, "message": result.message}
    return out


# ---- the case and the verdict -------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class JudgeCase:
    line: str
    desk_id: int
    symbol: str
    as_of: datetime
    bull: BullOutput | None
    bear: BearOutput | None
    snapshot: Mapping[str, Any]
    reports: Mapping[str, str] = field(default_factory=dict)
    summary: str = ""
    ledger: DebateLedger = field(default_factory=DebateLedger)
    backtest: Mapping[str, Any] | None = None
    signal_source: str | None = None                  # "candles" when the signal was read from price candles
    n_tests: int | None = None


def argument_order(case: JudgeCase) -> tuple[str, str]:
    """The order in which the two arguments are shown, fixed by the case itself (never by the content), so neither side is always
    first."""
    digest = hashlib.sha256(f"{case.symbol}|{case.as_of.isoformat()}|{case.desk_id}".encode()).digest()
    return ("bull", "bear") if digest[0] % 2 == 0 else ("bear", "bull")


@dataclass(frozen=True)
class JudgeOutput:
    rating: str
    confidence: int
    band: str
    band_justification: str
    deciding_claim: str
    weakest_claim: str
    rationale: str
    catalysts: tuple[str, ...] = ()
    conditions: tuple[str, ...] = ()
    evidence_requested: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()

    @staticmethod
    def from_dict(data: Any, *, line: str, snapshot: Mapping[str, Any]) -> "JudgeOutput":
        keys = {"rating", "confidence", "band", "band_justification", "deciding_claim", "weakest_claim", "rationale", "catalysts",
                "conditions", "evidence_requested", "evidence_refs"}
        if isinstance(data, str):
            try:
                data = extract_json(data)
            except LLMParseError as exc:
                raise OutputError("judge: the answer holds no JSON object") from exc
        if not isinstance(data, Mapping) or set(map(str, data)) - keys:
            raise OutputError(f"judge: expected an object with the fields {sorted(keys)}")
        rating = extract_rating(str(data.get("rating", "")), rating_scale(line))
        rationale = _text(data.get("rationale"), "rationale", limit=900)
        catalysts = tuple(_text(c, "catalysts[]", limit=300) for c in _list(data.get("catalysts"), "catalysts", minimum=0, maximum=6))
        conditions = tuple(_text(c, "conditions[]", limit=300) for c in _list(data.get("conditions"), "conditions", minimum=0, maximum=6))
        requested = tuple(_text(c, "evidence_requested[]", limit=300) for c in _list(data.get("evidence_requested"), "evidence_requested", minimum=0, maximum=8))
        refs = tuple(_text(r, "evidence_refs[]", limit=160) for r in _list(data.get("evidence_refs"), "evidence_refs", minimum=0, maximum=12))
        if rating == REVIEW:
            confidence = data.get("confidence", 0)
            texts = [rationale, *requested]
            _guard(texts, snapshot)
            return JudgeOutput(REVIEW, confidence if isinstance(confidence, int) and not isinstance(confidence, bool) else 0,
                               str(data.get("band") or ""), "", _text(data.get("deciding_claim"), "deciding_claim", required=False),
                               _text(data.get("weakest_claim"), "weakest_claim", required=False), rationale, catalysts, conditions, requested, refs)
        deciding = _text(data.get("deciding_claim"), "deciding_claim")
        weakest = _text(data.get("weakest_claim"), "weakest_claim")
        band = ConfidenceScale(line).validate(data.get("confidence"), data.get("band"), data.get("band_justification"), refs, snapshot)
        if rating == "APPROVE_WITH_CONDITIONS" and not conditions:
            raise OutputError("conditions: an approval with conditions must list them")
        justification = _text(data.get("band_justification"), "band_justification", limit=700)
        _guard([rationale, deciding, weakest, justification, *catalysts, *conditions, *requested], snapshot)
        return JudgeOutput(rating, data["confidence"], band.name, justification, deciding, weakest, rationale, catalysts, conditions, requested, refs)

    def to_dict(self) -> dict[str, Any]:
        return {"rating": self.rating, "confidence": self.confidence, "band": self.band, "band_justification": self.band_justification,
                "deciding_claim": self.deciding_claim, "weakest_claim": self.weakest_claim, "rationale": self.rationale,
                "catalysts": list(self.catalysts), "conditions": list(self.conditions), "evidence_requested": list(self.evidence_requested),
                "evidence_refs": list(self.evidence_refs)}


@dataclass(frozen=True)
class JudgeResult:
    rating: str
    output: JudgeOutput | None
    flags: tuple[str, ...] = ()
    order: tuple[str, str] = ("bull", "bear")
    degraded: bool = False
    error: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)
    gate: DoubleReviewResult | None = None

    def to_plantel(self) -> JudgeVerdict:
        """The verdict in the shape the plantel declares for the seat."""
        text = self.output.rationale if self.output else (self.error or "; ".join(self.flags) or "no verdict")
        return JudgeVerdict(self.rating, text, self.output.evidence_requested if self.output else ())


def _review(rating_flags: tuple[str, ...], error: str, order: tuple[str, str], evidence: Mapping[str, Any], gate: DoubleReviewResult | None,
            *, degraded: bool = False, rating: str = REVIEW) -> JudgeResult:
    return JudgeResult(rating, None, rating_flags, order, degraded, error, evidence, gate)


def requirements(case: JudgeCase) -> tuple[str, ...]:
    """Reasons the tribunal cannot judge yet. A line that needs a backtest and has none, or a debate with a missing side."""
    reasons: list[str] = []
    if case.bull is None or case.bear is None:
        reasons.append("debate_incomplete")
    if case.line in REQUIRES_BACKTEST and not case.backtest:
        reasons.append("backtest_required")
    return tuple(reasons)


def judge_snapshot(case: JudgeCase, evidence: Mapping[str, Any]) -> dict[str, Any]:
    """What the judge may quote: the snapshot of the case, the realistic reference, the scale, the tests counted, the backtest and the
    evidence the validation module returned. Every number of the verdict must be found here."""
    snap: dict[str, Any] = dict(case.snapshot)
    snap["reference"] = dict(REPLICATION_REFERENCE)
    snap["scale"] = ConfidenceScale(case.line).as_snapshot()
    if case.backtest:
        snap["backtest"] = dict(case.backtest)
    if case.n_tests is not None:
        snap.setdefault("evidence", {})
        snap["evidence"] = {**dict(snap["evidence"]), "tests": count_disclosure(case.n_tests)}
    if evidence:
        snap["evidence"] = {**dict(snap.get("evidence", {})), **dict(evidence)}
    return snap


def apply_rules(output: JudgeOutput, case: JudgeCase, gate: DoubleReviewResult | None) -> tuple[str, tuple[str, ...]]:
    """The rating after the rules of code. Each rule only makes the verdict safer."""
    rating, flags = output.rating, []
    if case.signal_source == "candles":
        costs = resolve_ref(case.snapshot, "costs")
        portfolio = resolve_ref(case.snapshot, "portfolio")
        if costs is MISSING or portfolio is MISSING:
            rating = "REJECT"
            flags.append("candle_signal_without_costs_and_portfolio")
    if rating in APPROVALS and case.line in CATALYST_LINES and not output.catalysts:
        rating = REVIEW
        flags.append("approval_without_catalyst")
    if rating in APPROVALS and gate is not None and not gate.approved:
        rating = REVIEW
        flags.append("double_review_not_approved")
    return rating, tuple(flags)


def judge_user_message(case: JudgeCase, snapshot: Mapping[str, Any], order: tuple[str, str]) -> str:
    parts = {"bull": f"Argument in favor:\n{case.bull.render() if case.bull else '(missing)'}",
             "bear": f"Argument against:\n{case.bear.render() if case.bear else '(missing)'}"}
    return (f"Case: {case.symbol}, line {case.line}.\n\nAnalyst reports:\n{reports_block(case.reports)}\n\n"
            f"{parts[order[0]]}\n\n{parts[order[1]]}\n\nDebate summary: {case.summary or '(none)'}\n"
            f"Refuted arguments:\n{case.ledger.lines()}\n\nSnapshot (the only evidence you may quote):\n{render_snapshot(snapshot)}")


def judge(model: ChatModel, case: JudgeCase, *, evidence_requests: Sequence[EvidenceRequest] = (), evidence_box: Toolbox | None = None,
          gate: DoubleReviewResult | None = None, kit_root: Path | None = None, composer: PromptComposer | None = None,
          language: str | None = None) -> JudgeResult:
    """Amos judges. Fails closed: whatever cannot be judged, or does not validate after one retry, is REVIEW.

    1. Code checks what the line needs (a backtest for DT and SWC, both arguments). Missing: REVIEW, no model call.
    2. Evidence phase (only if asked): validation functions by `quant_call`. The judge never computes them.
    3. Judgment: no tools, the phrase of TA-34 in the prompt, the anchored scale of the line, the arguments in an order fixed by code.
    4. Code applies its rules to the verdict."""
    order = argument_order(case)
    if reasons := requirements(case):
        return _review(reasons, "; ".join(reasons), order, {}, gate)
    evidence: dict[str, Any] = {}
    if evidence_requests:
        evidence = gather_evidence(evidence_box or evidence_toolbox(), evidence_requests)
    snapshot = judge_snapshot(case, evidence)
    scale = ConfidenceScale(case.line)
    ratings = ", ".join((*rating_scale(case.line), REVIEW))
    system = seat_prompt(JUDGE, line=case.line, desk_id=case.desk_id, as_of=case.as_of, kit_root=kit_root, composer=composer, language=language,
                         extra=[f"## No external tools\n{NO_EXTERNAL_TOOLS}", scale.prompt_text(), f"## Rating scale of line {case.line}\n{ratings}"])
    user = judge_user_message(case, snapshot, order)
    try:
        output = _ask(model, system, user, lambda text: JudgeOutput.from_dict(text, line=case.line, snapshot=snapshot), judge_toolbox())
    except ToolNotAllowed as exc:
        return _review(("tool_requested",), str(exc), order, evidence, gate, degraded=True)
    except OutputError as exc:
        return _review(("verdict_not_valid",), str(exc), order, evidence, gate, degraded=True)
    rating, flags = apply_rules(output, case, gate)
    return JudgeResult(rating, output, flags, order, False, "", evidence, gate)

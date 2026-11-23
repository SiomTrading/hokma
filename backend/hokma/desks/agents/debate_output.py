"""Output types of the tribunal advocates and the checks that validate them (SPEC-019: B9, moved by B13).

`BullOutput` (Jonah, A08) and `BearOutput` (Simon, A09) live here, in a module that does not import the plantel, so that the plantel can name
them as the output type of the two seats and the debate module can import the plantel without a circular import. `hokma.desks.agents.debate`
re-exports every name below, so nothing that imported them from there changes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from hokma.desks.core.numbers import unverified_numbers
from hokma.desks.core.output import SchemaError, validate_schema
from hokma.desks.core.signal import contains_execution_terms
from hokma.llm.jsonx import LLMParseError, extract_json

SEVERITIES = ("low", "medium", "high")
MAX_POINTS = 5
MAX_CATALYSTS = 5
MAX_DISPROVE = 6
MAX_TEXT = 600
FORBIDDEN_DECISION_KEYS = frozenset({"decision", "rating", "verdict", "recommendation", "action", "size", "order", "stop", "target"})
_WORD = re.compile(r"[a-z0-9]+")
MISSING = object()


class DebateError(ValueError):
    """A debate that cannot run, or an answer that does not hold."""


class OutputError(DebateError):
    """An answer of an agent that does not validate. The message says which field and why."""


# ---- snapshot references and numbers -------------------------------------------------------------------------------

def resolve_ref(snapshot: Mapping[str, Any], ref: str) -> Any:
    """The value a dotted reference points to in the snapshot (list items by index), or MISSING. A null value is missing."""
    if not isinstance(ref, str) or not ref.strip() or len(ref) > 160:
        return MISSING
    node: Any = snapshot
    parts = ref.strip().split(".")
    if len(parts) > 10:
        return MISSING
    for part in parts:
        if isinstance(node, Mapping) and part in node:
            node = node[part]
        elif isinstance(node, (list, tuple)) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return MISSING
    return MISSING if node is None else node


def check_numbers(texts: Sequence[str], snapshot: Any) -> list[str]:
    """Numbers written in `texts` that are not in the snapshot. Empty means every number has a source."""
    return [c.raw for c in unverified_numbers("\n".join(t for t in texts if t), snapshot)]


@dataclass(frozen=True)
class EvidenceRef:
    ref: str
    note: str

    def to_dict(self) -> dict[str, str]:
        return {"ref": self.ref, "note": self.note}


def _text(value: Any, where: str, *, required: bool = True, limit: int = MAX_TEXT) -> str:
    if value is None or value == "":
        if required:
            raise OutputError(f"{where}: required text is missing")
        return ""
    if not isinstance(value, str):
        raise OutputError(f"{where}: expected text")
    body = " ".join(value.split())
    if required and not body:
        raise OutputError(f"{where}: required text is empty")
    if len(body) > limit:
        raise OutputError(f"{where}: text is longer than {limit} characters")
    return body


def _list(value: Any, where: str, *, minimum: int, maximum: int) -> list[Any]:
    if value is None:
        value = []
    if not isinstance(value, (list, tuple)):
        raise OutputError(f"{where}: expected a list")
    if not minimum <= len(value) <= maximum:
        raise OutputError(f"{where}: needs {minimum} to {maximum} items, got {len(value)}" if minimum != maximum
                          else f"{where}: needs exactly {minimum} items, got {len(value)}")
    return list(value)


def _refs(value: Any, where: str, snapshot: Mapping[str, Any]) -> tuple[EvidenceRef, ...]:
    items = _list(value, where, minimum=1, maximum=6)
    out: list[EvidenceRef] = []
    for index, item in enumerate(items):
        place = f"{where}[{index}]"
        if not isinstance(item, Mapping) or set(item) - {"ref", "note"}:
            raise OutputError(f"{place}: expected an object with ref and note")
        ref = _text(item.get("ref"), f"{place}.ref", limit=160)
        if resolve_ref(snapshot, ref) is MISSING:
            raise OutputError(f"{place}.ref: {ref!r} is not in the snapshot; an objection or a claim without data is rejected")
        out.append(EvidenceRef(ref, _text(item.get("note"), f"{place}.note", limit=300)))
    return tuple(out)


def _guard(texts: Sequence[str], snapshot: Mapping[str, Any]) -> None:
    for body in texts:
        if (found := contains_execution_terms(body)) is not None:
            raise OutputError(f"an advocate does not decide or size anything; remove {found!r}")
    if bad := check_numbers(texts, snapshot):
        raise OutputError(f"numbers without a source (unverified): {', '.join(bad[:6])}; use only numbers that are in the snapshot")


def _object(data: Any, allowed: set[str], who: str) -> Mapping[str, Any]:
    if isinstance(data, str):
        try:
            data = extract_json(data)
        except LLMParseError as exc:
            raise OutputError(f"{who}: the answer holds no JSON object") from exc
    if not isinstance(data, Mapping):
        raise OutputError(f"{who}: the answer must be an object")
    if (found := sorted(set(map(str, data)) & FORBIDDEN_DECISION_KEYS)):
        raise OutputError(f"{who}: an advocate does not decide; field(s) {found} are not allowed")
    if extra := sorted(set(map(str, data)) - allowed):
        raise OutputError(f"{who}: unknown field(s) {extra}")
    return data


# ---- outputs ---------------------------------------------------------------------------------------------------------

_EVIDENCE_SCHEMA = {"type": "array", "items": {"type": "object", "required": ["ref", "note"],
                                                 "properties": {"ref": {"type": "string"}, "note": {"type": "string"}}}}
BULL_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["points"],
    "properties": {
        "rebuttal": {"type": "string"},
        "points": {"type": "array", "minItems": 1, "maxItems": MAX_POINTS,
                   "items": {"type": "object", "required": ["claim", "evidence"],
                             "properties": {"claim": {"type": "string"}, "evidence": _EVIDENCE_SCHEMA}}},
        "catalysts": {"type": "array", "maxItems": MAX_CATALYSTS, "items": {"type": "string"}},
    },
}
BEAR_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["concerns", "most_likely_failure", "would_disprove"],
    "properties": {
        "rebuttal": {"type": "string"},
        "concerns": {"type": "array", "minItems": 3, "maxItems": 3,
                     "items": {"type": "object", "required": ["claim", "severity", "evidence", "mitigation"],
                               "properties": {"claim": {"type": "string"}, "severity": {"type": "string", "enum": list(SEVERITIES)},
                                              "evidence": _EVIDENCE_SCHEMA, "mitigation": {"type": "string"}}}},
        "most_likely_failure": {"type": "object", "required": ["concern", "reason"],
                                "properties": {"concern": {"type": "integer", "minimum": 1, "maximum": 3}, "reason": {"type": "string"}}},
        "would_disprove": {"type": "array", "minItems": 1, "maxItems": MAX_DISPROVE, "items": {"type": "string"}},
    },
}
MODERATOR_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["refuted", "central_disagreement", "another_round"],
    "properties": {
        "refuted": {"type": "array", "items": {"type": "object", "required": ["side", "claim", "reason"],
                                               "properties": {"side": {"type": "string", "enum": ["bull", "bear"]},
                                                              "claim": {"type": "string"}, "reason": {"type": "string"}}}},
        "central_disagreement": {"type": "string"}, "another_round": {"type": "boolean"},
    },
}


def _schema_check(data: Mapping[str, Any], schema: dict[str, Any], who: str) -> None:
    try:
        validate_schema(data, schema)
    except SchemaError as exc:
        raise OutputError(f"{who}: {exc}") from exc


@dataclass(frozen=True)
class Point:
    claim: str
    evidence: tuple[EvidenceRef, ...]

    @property
    def refs(self) -> tuple[str, ...]:
        return tuple(e.ref for e in self.evidence)


@dataclass(frozen=True)
class Concern:
    claim: str
    severity: str
    evidence: tuple[EvidenceRef, ...]
    mitigation: str

    @property
    def refs(self) -> tuple[str, ...]:
        return tuple(e.ref for e in self.evidence)


@dataclass(frozen=True)
class BullOutput:
    """Jonah: the case in favor, point by point, with the answer to the other side and the catalysts. No decision."""

    rebuttal: str
    points: tuple[Point, ...]
    catalysts: tuple[str, ...] = ()
    side: str = "bull"

    @staticmethod
    def from_dict(data: Any, *, snapshot: Mapping[str, Any], opening_turn: bool) -> "BullOutput":
        body = _object(data, {"rebuttal", "points", "catalysts"}, "bull")
        _schema_check(body, BULL_SCHEMA, "bull")
        rebuttal = _text(body.get("rebuttal"), "rebuttal", required=False)
        _check_rebuttal(rebuttal, opening_turn)
        points = []
        for index, item in enumerate(_list(body.get("points"), "points", minimum=1, maximum=MAX_POINTS)):
            if not isinstance(item, Mapping) or set(item) - {"claim", "evidence"}:
                raise OutputError(f"points[{index}]: expected an object with claim and evidence")
            points.append(Point(_text(item.get("claim"), f"points[{index}].claim"), _refs(item.get("evidence"), f"points[{index}].evidence", snapshot)))
        _distinct([p.claim for p in points], "points")
        catalysts = tuple(_text(c, "catalysts[]", limit=300) for c in _list(body.get("catalysts"), "catalysts", minimum=0, maximum=MAX_CATALYSTS))
        out = BullOutput(rebuttal, tuple(points), catalysts)
        _guard(out.texts(), snapshot)
        return out

    def texts(self) -> list[str]:
        return [self.rebuttal, *(p.claim for p in self.points), *(e.note for p in self.points for e in p.evidence), *self.catalysts]

    def claims(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        return tuple((p.claim, p.refs) for p in self.points)

    def render(self) -> str:
        lines = [f"- {p.claim} [{', '.join(p.refs)}]" for p in self.points]
        extra = [f"Catalysts: {'; '.join(self.catalysts)}"] if self.catalysts else []
        head = [f"Rebuttal: {self.rebuttal}"] if self.rebuttal else []
        return "\n".join([*head, *lines, *extra])

    def to_dict(self) -> dict[str, Any]:
        return {"rebuttal": self.rebuttal, "points": [{"claim": p.claim, "evidence": [e.to_dict() for e in p.evidence]} for p in self.points],
                "catalysts": list(self.catalysts)}


@dataclass(frozen=True)
class BearOutput:
    """Simon: exactly three concerns with severity, evidence and mitigation, the most likely failure point and what would
    disprove the thesis (SKL-005). No decision."""

    rebuttal: str
    concerns: tuple[Concern, ...]
    most_likely_failure: int            # position of the concern, 1 to 3
    failure_reason: str
    would_disprove: tuple[str, ...]
    side: str = "bear"

    @staticmethod
    def from_dict(data: Any, *, snapshot: Mapping[str, Any], opening_turn: bool) -> "BearOutput":
        body = _object(data, {"rebuttal", "concerns", "most_likely_failure", "would_disprove"}, "bear")
        _schema_check(body, BEAR_SCHEMA, "bear")
        rebuttal = _text(body.get("rebuttal"), "rebuttal", required=False)
        _check_rebuttal(rebuttal, opening_turn)
        concerns = []
        for index, item in enumerate(_list(body.get("concerns"), "concerns", minimum=3, maximum=3)):
            place = f"concerns[{index}]"
            if not isinstance(item, Mapping) or set(item) - {"claim", "severity", "evidence", "mitigation"}:
                raise OutputError(f"{place}: expected claim, severity, evidence and mitigation")
            severity = item.get("severity")
            if severity not in SEVERITIES:
                raise OutputError(f"{place}.severity: must be one of {SEVERITIES}")
            concerns.append(Concern(_text(item.get("claim"), f"{place}.claim"), severity,
                                    _refs(item.get("evidence"), f"{place}.evidence", snapshot),
                                    _text(item.get("mitigation"), f"{place}.mitigation")))
        _distinct([c.claim for c in concerns], "concerns")
        failure = body.get("most_likely_failure")
        if not isinstance(failure, Mapping) or set(failure) - {"concern", "reason"}:
            raise OutputError("most_likely_failure: expected concern and reason; a thesis is never left without its most likely failure point")
        position = failure.get("concern")
        if isinstance(position, bool) or not isinstance(position, int) or not 1 <= position <= 3:
            raise OutputError("most_likely_failure.concern: must be the position 1, 2 or 3 of one of the concerns")
        reason = _text(failure.get("reason"), "most_likely_failure.reason")
        disprove = tuple(_text(c, "would_disprove[]", limit=300) for c in _list(body.get("would_disprove"), "would_disprove", minimum=1, maximum=MAX_DISPROVE))
        out = BearOutput(rebuttal, tuple(concerns), position, reason, disprove)
        _guard(out.texts(), snapshot)
        return out

    def texts(self) -> list[str]:
        return [self.rebuttal, self.failure_reason, *self.would_disprove,
                *(t for c in self.concerns for t in (c.claim, c.mitigation, *(e.note for e in c.evidence)))]

    def claims(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        return tuple((c.claim, c.refs) for c in self.concerns)

    def render(self) -> str:
        lines = [f"- ({c.severity}) {c.claim} [{', '.join(c.refs)}]; mitigation: {c.mitigation}" for c in self.concerns]
        head = [f"Rebuttal: {self.rebuttal}"] if self.rebuttal else []
        tail = [f"Most likely failure: concern {self.most_likely_failure}: {self.failure_reason}",
                f"Would disprove the thesis: {'; '.join(self.would_disprove)}"]
        return "\n".join([*head, *lines, *tail])

    def to_dict(self) -> dict[str, Any]:
        return {"rebuttal": self.rebuttal,
                "concerns": [{"claim": c.claim, "severity": c.severity, "evidence": [e.to_dict() for e in c.evidence], "mitigation": c.mitigation}
                             for c in self.concerns],
                "most_likely_failure": {"concern": self.most_likely_failure, "reason": self.failure_reason},
                "would_disprove": list(self.would_disprove)}


def _check_rebuttal(rebuttal: str, opening_turn: bool) -> None:
    if opening_turn and rebuttal:
        raise OutputError("rebuttal: the other side has not spoken, so there is nothing to rebut; leave it empty and open with your own evidence")
    if not opening_turn and len(rebuttal) < 10:
        raise OutputError("rebuttal: answer the last argument of the other side before you add anything new")


def _distinct(claims: Sequence[str], where: str) -> None:
    for i, a in enumerate(claims):
        for b in claims[i + 1:]:
            if similarity(a, b) >= 0.9:
                raise OutputError(f"{where}: two entries say the same thing")


def _tokens(text: str) -> frozenset[str]:
    return frozenset(w for w in _WORD.findall((text or "").lower()) if len(w) > 2)


def similarity(a: str, b: str) -> float:
    """Share of words two claims have in common (0 to 1). Used to tell that an argument came back."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)

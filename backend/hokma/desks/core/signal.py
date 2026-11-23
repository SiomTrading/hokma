"""Signal contract shared by every desk source of views (SPEC-017 section 4.8: AH-01, AH-02, AH-04).

A signal is a view: a direction and a conviction in [-1, +1] with the facts behind it. It never carries how much to
commit, where to leave or when to act: those belong to other roles, and the contract has no field for them.
Abstaining is a first class outcome: a failed call or a missing required input is an abstention, never a neutral view.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import InitVar, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Iterable, Literal, Mapping, Protocol, Sequence, get_args, runtime_checkable

Line = Literal["RF", "DT", "SWC", "SWO", "LP"]
Horizon = Literal["intraday", "days", "weeks", "months", "years"]
LINES: tuple[str, ...] = get_args(Line)
HORIZONS: tuple[str, ...] = get_args(Horizon)
CONTRACT_VERSION = "1"
MIN_SENTENCES, MAX_SENTENCES = 2, 4

# Text a view must never contain: how much to commit, where to leave, a price objective or when to enter.
EXECUTION_TERMS = re.compile(
    r"\b(?:position|trade|order|lot|bet)\s+siz\w*|\bsizing\b|\bsize\s+(?:the|this|your)\s+(?:position|trade|bet)"
    r"|\bstops?\b|\bstop[- ]?loss\b|\btake[- ]profit\b|\bprofit\s+target\b|\b(?:price\s+target|target\s+price)\b"
    r"|\bentry\s+(?:point|price|timing|level|zone|trigger)\b|\benter\s+(?:now|at|on|when)\b"
    r"|\b(?:buy|sell)\s+(?:now|at)\b|\bhow\s+many\s+(?:shares|contracts|lots)\b"
    r"|tama(?:n|\u00f1)o\s+de\s+(?:la\s+)?posici(?:o|\u00f3)n|\bprecio\s+objetivo\b|\bobjetivo\s+de\s+precio\b"
    r"|\bentrar\s+(?:ahora|en|a)\b",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"[.!?\u2026]+(?=\s|$)")


def contains_execution_terms(text: str) -> str | None:
    """The first sizing, exit, objective or entry-timing phrase found in a text, or None."""
    found = EXECUTION_TERMS.search((text or "").replace("_", " "))
    return found.group(0) if found else None


def count_sentences(text: str) -> int:
    """Sentences in a text: pieces closed by . ! ? or an ellipsis, plus a trailing piece without a closing mark."""
    body = (text or "").strip()
    if not body:
        return 0
    pieces = [p for p in _SENTENCE_END.split(body) if p.strip()]
    return len(pieces)


def _aware_utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{label} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must carry a timezone (UTC cut-off)")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class Criterion:
    """One number a module computed. The model reads it; it never produces it."""

    name: str                    # e.g. "roe_10y_min"
    value: float | None          # computed by a SVC module, never by the model
    threshold: float | None
    passed: bool | None          # None when value is None
    source: str                  # registry name or data call that produced it

    def __post_init__(self) -> None:
        if not self.name or not isinstance(self.name, str):
            raise ValueError("a criterion needs a name")
        if self.value is not None and (isinstance(self.value, bool) or not isinstance(self.value, (int, float)) or not math.isfinite(self.value)):
            raise ValueError(f"criterion {self.name}: value must be a finite number or None")
        if self.value is None and self.passed is not None:
            raise ValueError(f"criterion {self.name}: passed must be None when value is None")
        if self.threshold is not None and not math.isfinite(self.threshold):
            raise ValueError(f"criterion {self.name}: threshold must be finite or None")
        if bad := contains_execution_terms(f"{self.name} {self.source}"):
            raise ValueError(f"criterion {self.name}: a signal does not speak of sizing, exits or timing ({bad!r})")

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "value": self.value, "threshold": self.threshold, "passed": self.passed, "source": self.source}

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "Criterion":
        allowed = {"name", "value", "threshold", "passed", "source"}
        if extra := set(data) - allowed:
            raise ValueError(f"unknown criterion field(s): {sorted(extra)}")
        return Criterion(**{k: data[k] for k in allowed if k in data})


@dataclass(frozen=True)
class Signal:
    """A view on one symbol at one cut-off. Fields are exactly the contract; there is nothing else to fill.

    Three construction-time arguments are checked and not stored: `confidence_cap` (the cap of the profile that
    emits it), `required` (names of criteria that must have a value) and `snapshot_at` (when the inputs were frozen).
    """

    source: str                  # agent code, e.g. "A16"
    desk_id: int
    line: Line
    symbol: str
    as_of: datetime              # UTC cut-off of every input used
    horizon: Horizon
    value: float | None          # in [-1.0, +1.0]; None if and only if abstained
    confidence: int              # 0..100, anchored scale; capped per profile (e.g. 69 for A22)
    abstained: bool
    criteria: tuple[Criterion, ...]
    data_missing: tuple[str, ...]
    reason: str                  # 2 to 4 sentences, in the owner's language
    snapshot_hash: str           # desks.core.hashing.content_hash of the inputs (AH-47)
    contract_version: str = CONTRACT_VERSION
    confidence_cap: InitVar[int] = 100
    required: InitVar[Sequence[str]] = ()
    snapshot_at: InitVar[datetime | None] = None

    def __post_init__(self, confidence_cap: int, required: Sequence[str], snapshot_at: datetime | None) -> None:
        if not self.source or not self.symbol:
            raise ValueError("a signal needs a source and a symbol")
        if self.line not in LINES:
            raise ValueError(f"line must be one of {LINES}")
        if self.horizon not in HORIZONS:
            raise ValueError(f"horizon must be one of {HORIZONS}")
        if isinstance(self.desk_id, bool) or not isinstance(self.desk_id, int):
            raise ValueError("desk_id must be an integer")
        object.__setattr__(self, "as_of", _aware_utc(self.as_of, "as_of"))
        object.__setattr__(self, "criteria", tuple(self.criteria))
        object.__setattr__(self, "data_missing", tuple(self.data_missing))
        if not all(isinstance(c, Criterion) for c in self.criteria):
            raise ValueError("criteria must be Criterion objects")
        # Invariant 7: anti-lookahead.
        if snapshot_at is not None and self.as_of > _aware_utc(snapshot_at, "snapshot_at"):
            raise ValueError("as_of is later than the snapshot time (lookahead)")
        # Invariant 3: confidence range and the cap of the profile.
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, int) or not 0 <= self.confidence <= 100:
            raise ValueError("confidence must be an integer in [0, 100]")
        if not 0 <= confidence_cap <= 100:
            raise ValueError("confidence_cap must be in [0, 100]")
        if self.confidence > confidence_cap:
            raise ValueError(f"confidence {self.confidence} is above the profile cap {confidence_cap}")
        # Invariant 4: a required criterion without a value makes the signal abstain and names the gap.
        wanted = tuple(required)
        by_name = {c.name: c for c in self.criteria}
        gaps = [n for n in wanted if n not in by_name or by_name[n].value is None]
        if gaps:
            merged = tuple(dict.fromkeys((*self.data_missing, *gaps)))
            object.__setattr__(self, "data_missing", merged)
            object.__setattr__(self, "value", None)
            object.__setattr__(self, "abstained", True)
        # Invariant 2: value range.
        if self.value is not None:
            if isinstance(self.value, bool) or not isinstance(self.value, (int, float)) or not math.isfinite(self.value):
                raise ValueError("value must be a finite number or None")
            if not -1.0 <= float(self.value) <= 1.0:
                raise ValueError(f"value {self.value} is outside [-1, +1]")
            object.__setattr__(self, "value", float(self.value))
        # Invariant 1: abstained if and only if value is None.
        if self.abstained != (self.value is None):
            raise ValueError("abstained must be true if and only if value is None")
        # Invariant 5: two to four sentences.
        sentences = count_sentences(self.reason)
        if not MIN_SENTENCES <= sentences <= MAX_SENTENCES:
            raise ValueError(f"reason must have {MIN_SENTENCES} to {MAX_SENTENCES} sentences, it has {sentences}")
        # Invariant 6: no sizing, exit, objective or entry timing anywhere in the text.
        for label, text in (("reason", self.reason), ("data_missing", " ".join(self.data_missing)), ("source", self.source)):
            if bad := contains_execution_terms(text):
                raise ValueError(f"{label} speaks of sizing, exits or timing ({bad!r}); a signal is a view only")
        if not self.snapshot_hash:
            raise ValueError("snapshot_hash is required")

    @staticmethod
    def abstain(source: str, desk_id: int, line: Line, symbol: str, as_of: datetime, horizon: Horizon,
                missing: Iterable[str], reason: str, snapshot_hash: str) -> "Signal":
        """An explicit abstention: value None, confidence 0 and the missing inputs listed."""
        return Signal(source=source, desk_id=desk_id, line=line, symbol=symbol, as_of=as_of, horizon=horizon,
                      value=None, confidence=0, abstained=True, criteria=(), data_missing=tuple(missing),
                      reason=reason, snapshot_hash=snapshot_hash)

    # ---- serialization (invariant 8) ----
    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source, "desk_id": self.desk_id, "line": self.line, "symbol": self.symbol,
            "as_of": self.as_of.isoformat(), "horizon": self.horizon, "value": self.value,
            "confidence": self.confidence, "abstained": self.abstained,
            "criteria": [c.to_dict() for c in self.criteria], "data_missing": list(self.data_missing),
            "reason": self.reason, "snapshot_hash": self.snapshot_hash, "contract_version": self.contract_version,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "Signal":
        names = {f.name for f in fields(Signal)}
        if extra := set(data) - names:
            raise ValueError(f"unknown signal field(s): {sorted(extra)}")
        missing = {n for n in names if n != "contract_version"} - set(data)
        if missing:
            raise ValueError(f"missing signal field(s): {sorted(missing)}")
        raw = dict(data)
        raw["as_of"] = datetime.fromisoformat(raw["as_of"]) if isinstance(raw["as_of"], str) else raw["as_of"]
        raw["criteria"] = tuple(Criterion.from_dict(c) for c in raw["criteria"])
        raw["data_missing"] = tuple(raw["data_missing"])
        return Signal(**raw)

    @staticmethod
    def from_json(text: str) -> "Signal":
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("a signal is a JSON object")
        return Signal.from_dict(data)


@dataclass(frozen=True)
class SignalContext:
    """Everything a source of views receives: who asks, for what, as of when, the facts and the computed criteria."""

    source: str
    desk_id: int
    line: Line
    symbol: str
    as_of: datetime
    horizon: Horizon
    snapshot_hash: str
    facts: Mapping[str, Any] = field(default_factory=dict)
    criteria: tuple[Criterion, ...] = ()
    required: tuple[str, ...] = ()
    confidence_cap: int = 100
    snapshot_at: datetime | None = None

    def missing_required(self) -> tuple[str, ...]:
        by_name = {c.name: c for c in self.criteria}
        return tuple(n for n in self.required if n not in by_name or by_name[n].value is None)

    def abstain(self, reason: str, missing: Iterable[str] = ()) -> Signal:
        return Signal.abstain(self.source, self.desk_id, self.line, self.symbol, self.as_of, self.horizon, missing, reason, self.snapshot_hash)


@runtime_checkable
class AlphaModel(Protocol):
    """Code signal: a deterministic view with no model call."""

    def signal(self, ctx: SignalContext) -> Signal: ...


@runtime_checkable
class QuantModel(Protocol):
    """Model reasoning over facts already computed by the modules."""

    def signal(self, ctx: SignalContext) -> Signal: ...

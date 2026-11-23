"""Base of a persona: a name plus a voice (SPEC-017: AH-03).

A persona never decides how much to commit or when to act. It receives criteria already computed by the modules and facts
frozen at a cut-off time, and returns a `Signal`. The base class owns the machinery: prompt assembly from the common rules,
a cache so an unchanged snapshot never pays for a second model call, and the failure contract: a failed or unreadable model
answer is an abstention, never a neutral view. A desk defines its own personas by subclassing and setting `code`, `name`
and `voice`.
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping, Protocol

from hokma.desks.core.hashing import content_hash
from hokma.desks.core.prompts import PromptComposer
from hokma.desks.core.signal import Criterion, Signal, SignalContext, count_sentences, MAX_SENTENCES

OUTPUT_SCHEMA = (
    'Answer with one JSON object and nothing else, in exactly this shape: '
    '{"signal": "bullish" | "bearish" | "neutral", "confidence": <integer 0-100>, '
    '"reasoning": "<your reasons in your own voice, two to four sentences>"}'
)
_SIGN = {"bullish": 1.0, "neutral": 0.0, "bearish": -1.0}
_SENTENCE = re.compile(r"[^.!?\u2026]+[.!?\u2026]*(?:\s+|$)")


class TextModel(Protocol):
    """What a persona needs from a language model: one call, a system text and a user text, text back."""

    def complete(self, system: str, user: str) -> str: ...


class PromptCache:
    """Answers already parsed, by the hash of the exact prompts. One cache per agent instance, never global."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}

    def get(self, key: str) -> dict[str, Any] | None:
        return self._data.get(key)

    def put(self, key: str, value: dict[str, Any]) -> None:
        self._data[key] = value

    def __len__(self) -> int:
        return len(self._data)


def extract_json(text: str) -> dict[str, Any]:
    """The first JSON object in an answer, with or without a code fence. Raises ValueError when there is none."""
    body = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", body, re.S)
    candidate = fenced.group(1) if fenced else body[body.find("{"): body.rfind("}") + 1] if "{" in body and "}" in body else ""
    if not candidate:
        raise ValueError("no JSON object in the answer")
    data = json.loads(candidate)
    if not isinstance(data, dict):
        raise ValueError("the answer is not a JSON object")
    return data


def first_sentences(text: str, limit: int = MAX_SENTENCES) -> str:
    parts = [m.group(0).strip() for m in _SENTENCE.finditer(text or "") if m.group(0).strip()]
    return " ".join(parts[:limit])


def render_criteria(criteria: tuple[Criterion, ...]) -> str:
    if not criteria:
        return "(none)"
    rows = []
    for c in criteria:
        value = "not available" if c.value is None else f"{c.value:.6g}"
        verdict = "unknown" if c.passed is None else ("passed" if c.passed else "failed")
        limit = "" if c.threshold is None else f", threshold {c.threshold:.6g}"
        rows.append(f"- {c.name}: {value}{limit}, {verdict} (source: {c.source})")
    return "\n".join(rows)


class PersonaAgent:
    """A source of views that reasons in a voice. Subclass and set `code`, `name` and `voice`."""

    code: str = ""
    name: str = ""
    voice: str = ""
    confidence_cap: int = 100
    lacks: str | None = None            # what this profile does not have; defaults to what its line lacks

    def __init__(self, model: TextModel, *, composer: PromptComposer | None = None, cache: PromptCache | None = None,
                 mode: str = "live", language: str | None = None, model_name: str = "") -> None:
        if not (self.code and self.name and self.voice.strip()):
            raise ValueError("a persona needs a code, a name and a voice")
        self._model = model
        self._composer = composer or PromptComposer()
        self._cache = cache if cache is not None else PromptCache()
        self._mode, self._language, self._model_name = mode, language, model_name or type(model).__name__

    # ---- prompts ----
    def system_prompt(self, ctx: SignalContext) -> str:
        cap = min(self.confidence_cap, ctx.confidence_cap)
        persona = f"{self.voice.strip()}\nYour confidence never goes above {cap}."
        base = self._composer.compose(role="investor", line=ctx.line, desk_id=ctx.desk_id, as_of=ctx.as_of, mode=self._mode,
                                      language=self._language, lacks=self.lacks, persona=persona)
        return f"{base}\n## Answer format\n{OUTPUT_SCHEMA}\n"

    def user_prompt(self, ctx: SignalContext) -> str:
        facts = json.dumps(ctx.facts, sort_keys=True, ensure_ascii=False, default=str, indent=1) if ctx.facts else "(none)"
        return (f"Symbol: {ctx.symbol}\nHorizon: {ctx.horizon}\nCut-off (UTC): {ctx.as_of.isoformat()}\n"
                f"Snapshot hash: {ctx.snapshot_hash}\n\nComputed criteria:\n{render_criteria(ctx.criteria)}\n\nFacts:\n{facts}\n")

    # ---- the view ----
    def signal(self, ctx: SignalContext) -> Signal:
        missing = ctx.missing_required()
        if missing:
            return self._abstain(ctx, missing, "A required input is missing. No view is formed.")
        system, user = self.system_prompt(ctx), self.user_prompt(ctx)
        key = content_hash({"agent": self.code, "model": self._model_name, "system": system, "user": user})
        parsed = self._cache.get(key)
        if parsed is None:
            try:
                answer = self._model.complete(system, user)
            except Exception:
                return self._abstain(ctx, ("model_call",), "The model call failed. No view is formed.")
            try:
                parsed = self._parse(answer)
            except (ValueError, TypeError, KeyError):
                return self._abstain(ctx, ("model_answer",), "The model answer could not be read. No view is formed.")
            self._cache.put(key, parsed)
        return self._to_signal(ctx, parsed)

    def _parse(self, answer: str) -> dict[str, Any]:
        data = extract_json(answer)
        sign = str(data.get("signal", "")).strip().lower()
        if sign not in _SIGN:
            raise ValueError("invalid signal")
        confidence = data.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 100:
            raise ValueError("invalid confidence")
        reason = first_sentences(str(data.get("reasoning", "")))
        return {"signal": sign, "confidence": int(round(confidence)), "reason": reason}

    def _to_signal(self, ctx: SignalContext, parsed: Mapping[str, Any]) -> Signal:
        cap = min(self.confidence_cap, ctx.confidence_cap)
        confidence = min(parsed["confidence"], cap)
        if count_sentences(parsed["reason"]) < 2:
            return self._abstain(ctx, ("reason",), "The model gave too little reasoning. No view is formed.")
        try:
            return Signal(source=ctx.source, desk_id=ctx.desk_id, line=ctx.line, symbol=ctx.symbol, as_of=ctx.as_of,
                          horizon=ctx.horizon, value=_SIGN[parsed["signal"]] * confidence / 100.0, confidence=confidence,
                          abstained=False, criteria=ctx.criteria, data_missing=(), reason=parsed["reason"],
                          snapshot_hash=ctx.snapshot_hash, confidence_cap=cap, required=ctx.required, snapshot_at=ctx.snapshot_at)
        except ValueError:
            return self._abstain(ctx, ("reason_content",), "The model answer broke the signal contract. No view is formed.")

    def _abstain(self, ctx: SignalContext, missing: tuple[str, ...], reason: str) -> Signal:
        return ctx.abstain(reason, missing)

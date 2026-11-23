"""The eight investor profiles as the company filter of a stock options desk (SPEC-019: B8, A16 to A22 and AH-16).

Each profile (Owen, Nate, Gabe, Paul, Ray, Noah, Ruth) is a source of views that reasons over criteria which code has already
computed: `hokma.quant.fundamentals.criteria` is asked through `registry.call` (the single path of every calculation), the answer becomes the
criteria of a `Signal`, and the model only reads them. The model never calls a library and never writes a number that the criteria,
the facts of the run or a tool result did not return: an answer with such a number is asked again once and then it is an abstention.

What code decides and the model cannot change (nothing the agent declares decides a control):

- the cap of confidence of the profile (Ruth never goes above 69, the series depth lowers the cap of the others) and the anchored
  scale (under 10 is no view, so it abstains);
- a failed call, a missing required criterion or a missing input is an abstention that names the gap, never a neutral view;
- a profile whose default is to wait or to doubt (Nate, Ray, Noah, Ruth) cannot be bullish unless the criteria lean bullish, and Noah
  cannot be bullish without a dated catalyst that is still ahead;
- a model that disagrees with the lean of the criteria has to say so (the signal then carries a criterion that records it).

On a stock options desk the signals go to `company_filter`: `portfolio.blend_signals` mixes them, leaves the abstentions out and merges the
profiles whose past views correlate above 0.8 in `desk_signals`. A company the filter rejects does not reach the debate. Every signal is
stored in `desk_signals` in the same transaction that checks the seat.

No profile says how much to commit, where to leave or when to act: `Signal` has no field for it and rejects the words.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import pandas as pd

from hokma.db import Database, get_database
from hokma.db.timeutil import to_db_time
from hokma.desks.agents import analyst_kit as kit
from hokma.desks.core import numbers
from hokma.desks.core.graph import ChatModel, ModelReply
from hokma.desks.core.hashing import content_hash
from hokma.desks.core.persona import PromptCache, extract_json, render_criteria
from hokma.desks.core.prompts import PromptComposer
from hokma.desks.core.signal import (
    Criterion, Horizon, Line, Signal, SignalContext, contains_execution_terms, count_sentences,
)
from hokma.desks.core.state import Message
from hokma.desks.core.tool_audit import ToolAuditLog
from hokma.desks.core.tools import ToolFailure, Toolbox
from hokma.desks.plantel import SEATS, Seat
from hokma.quant import registry
from hokma.quant.errors import BadInput

INVESTOR_CODES: tuple[str, ...] = ("A16", "A17", "A18", "A19", "A20", "A21", "A22")
PEAD_CODE = "A23"
SIGNAL_SOURCES: tuple[str, ...] = (*INVESTOR_CODES, PEAD_CODE)
MIN_CONFIDENCE = 10                    # 0 to 9 on the anchored scale is no view: it abstains
RUTH_CAP = 69
DEFAULT_TOOL_ROUNDS = 3
DEFAULT_REJECT_BELOW = 0.0             # a blended view under this value rejects the company (not calibrated: printed in the result)
CORRELATION_THRESHOLD = 0.8
HISTORY_LIMIT = 5000
STANCES = ("bullish", "bearish", "neutral", "abstain")
_SIGN = {"bullish": 1, "neutral": 0, "bearish": -1}
DISAGREES = "model_disagrees_with_criteria"
# Inputs that a run binds from real data: the model can never choose them.
BOUND = frozenset({"statements", "candles", "regime", "as_of", "symbol", "catalyst"})
HORIZON_BY_LINE: Mapping[str, Horizon] = {"SWO": "weeks", "SWC": "weeks", "LP": "months", "DT": "intraday", "RF": "months"}
# Names a mandate may use in the params of a profile, mapped to the input of the module.
PARAM_ALIASES = {"veto": "veto_enabled"}

SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["stance", "confidence", "reason", "disagrees_with_criteria"],
    "properties": {
        "stance": {"type": "string", "enum": list(STANCES)},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        "reason": {"type": "string", "minLength": 1, "maxLength": 1500},
        "disagrees_with_criteria": {"type": "boolean"},
    },
}


class InvestorError(ValueError):
    """A profile, a seat or a signal row that cannot be used."""


# ---- the profiles ---------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Profile:
    """What code knows about one investor profile: where its criteria come from and which rules the code keeps for it."""

    code: str
    function: str                              # registry name of its criteria
    required: tuple[str, ...]                  # criteria that must have a value, or the signal abstains
    lacks: str                                 # what the profile does not have (scope honesty, AH-15)
    confidence_cap: int = 100
    positive_needs_lean: bool = False          # a positive view only when the criteria lean positive (default is to wait or doubt)
    positive_needs: tuple[str, ...] = ()       # keys of the module result that must hold for a positive view
    optional_data: tuple[str, ...] = ()        # data that counts only when it arrives; listed as missing when it did not
    neutral_when: str | None = None            # key of the module result that forces neutral or abstention when true

    @property
    def seat(self) -> Seat:
        return SEATS[self.code]

    @property
    def name(self) -> str:
        return self.seat.name


PROFILES: Mapping[str, Profile] = {
    "A16": Profile("A16", "fundamentals.quality_owner", ("roe_average", "roe_minimum", "debt_to_equity", "margin_of_safety"),
                   "a competitive moat beyond sustained margins and return on equity, the honesty of management, and any price or option data not in the facts"),
    "A17": Profile("A17", "fundamentals.inversion_veto", (),
                   "the real incentives of management and the governance of the company: only their trace in the numbers",
                   confidence_cap=89, positive_needs_lean=True, neutral_when="too_hard"),
    "A18": Profile("A18", "fundamentals.safety_margin", ("eps_positive",),
                   "assets outside the balance sheet and dividends beyond the depth of the data"),
    "A19": Profile("A19", "fundamentals.fair_growth", ("eps_growth_cagr", "peg"),
                   "knowing a business from daily life and the qualitative story, and insiders or institutional data that did not arrive",
                   optional_data=("insider_transactions", "institutional_ownership")),
    "A20": Profile("A20", "fundamentals.inflection", ("revenue_growth_acceleration", "multiple_vs_own_history", "price_move_63_bars"),
                   "monetary policy, liquidity, bonds, currencies, commodities and any say on how much to commit",
                   positive_needs_lean=True),
    "A21": Profile("A21", "fundamentals.contrarian_gap", ("value_gap", "price_to_value"),
                   "the notes of annual reports, issuance documents, sentiment and short positioning",
                   positive_needs_lean=True, positive_needs=("catalyst_valid",)),
    "A22": Profile("A22", "fundamentals.disruption", ("theme_in_curated_table", "revenue_growth_latest"),
                   "the size of the market, the first mover edge and the vision of the team",
                   confidence_cap=RUTH_CAP, positive_needs_lean=True),
}


def profile_of(code: str) -> Profile:
    if code not in PROFILES:
        raise InvestorError(f"{code!r} is not an investor profile; the profiles are {', '.join(PROFILES)}")
    return PROFILES[code]


@dataclass(frozen=True)
class CompanyData:
    """The real inputs a run binds for the profiles. The model cannot replace any of them (a guard overwrites its arguments)."""

    statements: Any                            # point in time statements frame (column filed per period)
    candles: Any = None                        # daily candles of the symbol (Ray)
    regime: str | None = None                  # regime of the desk module (Ray)
    catalyst: Mapping[str, Any] | None = None  # dated catalyst {date, description} (Noah)
    insiders: Any = None                       # insider transactions when a provider delivered them (Paul)
    institutional: Any = None                  # institutional ownership when a provider delivered it (Paul)


def _cut_day(as_of: datetime) -> str:
    return as_of.astimezone(timezone.utc).date().isoformat()


def _closed_days_only(candles: Any, as_of: datetime) -> Any:
    """Candles of days before the day of the cut-off: the bar of the cut-off day may still be open and is never read."""
    if not isinstance(candles, pd.DataFrame) or not isinstance(candles.index, pd.DatetimeIndex):
        return candles
    index = candles.index.tz_localize(None) if candles.index.tz is not None else candles.index
    day = pd.Timestamp(as_of.astimezone(timezone.utc).date())
    return candles.set_axis(index)[index.normalize() < day]


def module_params(profile: Profile, params: Mapping[str, Any] | None) -> dict[str, Any]:
    """The parameters of the mandate for a profile, with the names of the module. A name that is bound by the run is refused."""
    clean: dict[str, Any] = {}
    for key, value in (params or {}).items():
        name = PARAM_ALIASES.get(str(key), str(key))
        if name in BOUND:
            raise InvestorError(f"{profile.name}: {key!r} is bound by the run and cannot be a parameter")
        clean[name] = value
    return clean


def compute_criteria(profile: Profile, symbol: str, as_of: datetime, data: CompanyData, params: Mapping[str, Any] | None = None):
    """The criteria module of a profile, run through the registry with the data of the run. Returns a QuantResult or an Exception."""
    registry.load_all()
    spec = registry.describe(profile.function)
    values = {"statements": data.statements, "as_of": _cut_day(as_of), "symbol": symbol, "regime": data.regime,
              "candles": _closed_days_only(data.candles, as_of), "catalyst": None if data.catalyst is None else dict(data.catalyst)}
    kwargs = {k: v for k, v in values.items() if k in spec.inputs and (v is not None or k == "regime")}
    kwargs.update(module_params(profile, params))
    try:
        return registry.call(profile.function, **kwargs)
    except Exception as exc:                                     # a failed call is an abstention, never a neutral view
        return exc


def _needs_candles(profile: Profile) -> bool:
    registry.load_all()
    declared = registry.describe(profile.function).inputs.get("candles")
    return isinstance(declared, dict) and bool(declared.get("required"))


def _missing_from(result: Any) -> tuple[str, ...]:
    if isinstance(result, InvestorError):
        return ("candles",)
    if isinstance(result, Exception):
        return ("quant_call_failed",)
    out: list[str] = []
    for warning in result.warnings:
        text = str(warning)
        if text.startswith(("data_missing:", "not_applicable:", "peg_not_defined:")):
            out.append(text.split(":", 1)[1].strip() or text)
    return tuple(dict.fromkeys(out)) or ("criteria_not_computable",)


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


def build_context(profile: Profile, *, desk_id: int, line: Line, symbol: str, as_of: datetime, data: CompanyData,
                  params: Mapping[str, Any] | None = None, snapshot_at: datetime | None = None) -> SignalContext:
    """The context of one profile: criteria already computed by the module, the facts around them, the required names and the cap.

    A module that could not give a value leaves the criteria empty and the context abstains with the gap named."""
    horizon = HORIZON_BY_LINE[line]
    result = compute_criteria(profile, symbol, as_of, data, params)
    if data.candles is None and _needs_candles(profile):
        result = InvestorError("candles")
    base_hash = content_hash({"profile": profile.code, "symbol": symbol, "as_of": _cut_day(as_of), "regime": data.regime,
                              "candles": _closed_days_only(data.candles, as_of), "catalyst": data.catalyst,
                              "inputs": "" if isinstance(result, Exception) else result.inputs_hash,
                              "params": {} if isinstance(result, Exception) else _json_safe(result.params)})
    if isinstance(result, Exception) or result.value is None or not isinstance(result.value, dict):
        gaps = _missing_from(result)
        why = str(result)[:200] if isinstance(result, Exception) else "; ".join(result.warnings)[:300]
        return SignalContext(source=profile.code, desk_id=desk_id, line=line, symbol=symbol, as_of=as_of, horizon=horizon,
                             snapshot_hash=base_hash, facts={"abstain": True, "missing": list(gaps), "why": why},
                             criteria=(), required=profile.required, confidence_cap=profile.confidence_cap, snapshot_at=snapshot_at)
    value = dict(result.value)
    criteria = tuple(Criterion.from_dict(c) for c in value.pop("criteria"))
    cap = min(profile.confidence_cap, int(value.get("confidence_cap", 100)))
    facts = _json_safe({**value, "params": result.params, "warnings": list(result.warnings), "piece_ids": list(result.piece_ids)})
    declared = [str(x) for x in (value.get("data_missing") or ())]
    absent = [name for name in profile.optional_data if getattr(data, {"insider_transactions": "insiders", "institutional_ownership": "institutional"}[name]) is None]
    facts["declared_missing"] = declared + absent
    facts["confidence_cap"] = cap
    if profile.optional_data:
        facts["optional_data_arrived"] = [n for n in profile.optional_data if n not in absent]
    return SignalContext(source=profile.code, desk_id=desk_id, line=line, symbol=symbol, as_of=as_of, horizon=horizon,
                         snapshot_hash=base_hash, facts=facts, criteria=criteria, required=profile.required, confidence_cap=cap,
                         snapshot_at=snapshot_at)


# ---- the agent --------------------------------------------------------------------------------------------------------

def _bound_values(data: CompanyData, symbol: str, as_of: datetime) -> dict[str, Any]:
    return {"statements": data.statements, "candles": _closed_days_only(data.candles, as_of), "regime": data.regime,
            "catalyst": None if data.catalyst is None else dict(data.catalyst), "as_of": _cut_day(as_of), "symbol": symbol}


def _guard(data: CompanyData, symbol: str, as_of: datetime) -> Callable[[str, dict], dict]:
    """A guard that overwrites every input the run owns: the model chooses assumptions, never the data or the date."""
    bound = _bound_values(data, symbol, as_of)

    def guard(name: str, params: dict) -> dict:
        try:
            spec = registry.describe(name)
        except Exception as exc:
            raise ToolFailure("bad_input", f"there is no quant function named {name!r}") from exc
        clean = {k: v for k, v in params.items() if k not in BOUND}
        for key, value in bound.items():
            if key in spec.inputs and value is not None:
                clean[key] = value
        return clean

    return guard


def _quant_handler(name: str, params: Mapping[str, Any]) -> Any:
    """Every call of a profile goes through registry.call, so the size limits and the time limit of the registry apply."""
    from hokma.kit import quant_tools

    spec = registry.describe(name)
    converted: dict[str, Any] = {}
    for key, value in params.items():
        declared = spec.inputs.get(key)
        kind = declared.get("type", "") if isinstance(declared, dict) else ""
        converted[key] = value if key in BOUND else quant_tools.to_input(value, kind)
    try:
        return registry.call(name, **converted).to_dict()
    except BadInput as exc:
        raise ToolFailure("bad_input", str(exc)[:400]) from exc


@dataclass
class Verdict:
    """What one run of an investor produced, for the audit: the signal and how it came out."""

    signal: Signal
    calls: tuple[str, ...] = ()
    retried: bool = False
    problems: tuple[str, ...] = ()
    cached: bool = False


class InvestorAgent:
    """One investor profile as a `QuantModel`: it receives a context with criteria and answers with a `Signal`.

    `model` is the chat model of the seat. Without bound data the quant tool is not offered; with it, every call is
    guarded and sent through the registry."""

    def __init__(self, code: str, model: ChatModel, *, kit_root: Path | None = None, composer: PromptComposer | None = None,
                 cache: PromptCache | None = None, mode: str = "live", language: str | None = None, model_name: str = "",
                 audit: ToolAuditLog | None = None, max_tool_rounds: int = DEFAULT_TOOL_ROUNDS) -> None:
        self.profile = profile_of(code)
        self._model, self._kit_root, self._composer = model, kit_root, composer or PromptComposer()
        self._cache = cache if cache is not None else PromptCache()
        self._mode, self._language, self._audit = mode, language, audit
        self._model_name = model_name or type(model).__name__
        self._max_rounds = max_tool_rounds
        self.last: Verdict | None = None

    @property
    def code(self) -> str:
        return self.profile.code

    # ---- prompts ----
    def system_prompt(self, ctx: SignalContext) -> str:
        seat_file = kit.load_seat_file(self.code, self._kit_root)
        if ctx.line not in seat_file.seat.lines or ctx.line not in seat_file.lines:
            raise kit.AnalystKitError(f"{self.profile.name} does not work on a {ctx.line} desk")
        cap = min(self.profile.confidence_cap, ctx.confidence_cap)
        persona = f"{seat_file.body}\n\nYour confidence never goes above {cap}."
        base = self._composer.compose(role="investor", line=ctx.line, desk_id=ctx.desk_id, as_of=ctx.as_of, mode=self._mode,
                                      language=self._language, lacks=self.profile.lacks, persona=persona)
        blocks = [base.rstrip("\n"), *(kit.skill_block(s) for s in seat_file.skills), f"## Numbers\n{kit.NUMBERS_RULE}",
                  kit.answer_format(SCHEMA)]
        return "\n\n".join(blocks) + "\n"

    def user_prompt(self, ctx: SignalContext) -> str:
        facts = json.dumps(ctx.facts, sort_keys=True, ensure_ascii=False, default=str, indent=1) if ctx.facts else "(none)"
        lean = ctx.facts.get("lean")
        return (f"Symbol: {ctx.symbol}\nLine: {ctx.line}\nHorizon: {ctx.horizon}\nCut-off (UTC): {ctx.as_of.isoformat()}\n"
                f"Snapshot hash: {ctx.snapshot_hash}\n\nComputed criteria:\n{render_criteria(ctx.criteria)}\n\n"
                f"Lean derived by code from the criteria: {lean}\n\nFacts:\n{facts}\n")

    # ---- the view ----
    def signal(self, ctx: SignalContext, data: CompanyData | None = None) -> Signal:
        return self.run(ctx, data).signal

    def run(self, ctx: SignalContext, data: CompanyData | None = None) -> Verdict:
        if ctx.source != self.code:
            raise InvestorError(f"context of {ctx.source} given to {self.code}")
        missing = ctx.missing_required()
        if ctx.facts.get("abstain") or missing:
            gaps = tuple(ctx.facts.get("missing") or missing)
            self.last = Verdict(ctx.abstain("A required input is missing. No view is formed.", gaps))
            return self.last
        system, user = self.system_prompt(ctx), self.user_prompt(ctx)
        bound_hash = "" if data is None else content_hash({"s": data.statements, "c": data.candles, "r": data.regime})
        key = content_hash({"agent": self.code, "model": self._model_name, "system": system, "user": user, "data": bound_hash})
        cached = self._cache.get(key)
        if cached is not None:
            verdict = self._finish(ctx, cached, (), False)
            self.last = Verdict(verdict, (), False, (), True)
            return self.last
        evidence = kit.EvidenceLog(None, {"facts": ctx.facts, "criteria": [c.to_dict() for c in ctx.criteria],
                                          "confidence_cap": ctx.confidence_cap})
        toolbox = self._toolbox(ctx, data)
        run_tool = evidence.runner(toolbox) if toolbox is not None else None
        messages: list[Message] = [Message("user", user, id="u0")]
        schemas = toolbox.schemas() if toolbox is not None else []
        rounds, retried, serial, problems_seen = 0, False, 0, ()
        while True:
            offered = schemas if rounds < self._max_rounds else []
            try:
                reply: ModelReply = self._model.chat(system, messages, offered)
            except Exception:                                       # a failed model call is an abstention
                self.last = Verdict(ctx.abstain("The model call failed. No view is formed.", ("model_call",)), evidence.tools_called(), retried)
                return self.last
            if reply.tool_calls and offered and run_tool is not None:
                serial += 1
                messages.append(Message("assistant", reply.content or "", id=f"a{serial}", tool_calls=tuple(reply.tool_calls)))
                for call in reply.tool_calls:
                    messages.append(Message("tool", run_tool(call), tool_call_id=call.id, name=call.name))
                rounds += 1
                continue
            parsed, problems = self._read(ctx, reply.content or "", evidence)
            if not problems:
                self._cache.put(key, parsed)
                self.last = Verdict(self._finish(ctx, parsed, problems, retried), evidence.tools_called(), retried)
                return self.last
            problems_seen = tuple(problems)
            if retried:
                self.last = Verdict(ctx.abstain("The model answer broke the rules of the profile. No view is formed.", ("model_answer",)),
                                    evidence.tools_called(), True, problems_seen)
                return self.last
            retried = True
            messages.append(Message("assistant", reply.content or "", id="a-final"))
            messages.append(Message("user", "That answer was not accepted: " + "; ".join(problems) + ". Answer again with only the JSON object of the schema.",
                                    id="u-retry"))
            rounds = self._max_rounds

    # ---- pieces ----
    def _toolbox(self, ctx: SignalContext, data: CompanyData | None) -> Toolbox | None:
        if data is None:
            return None
        guard = _guard(data, ctx.symbol, ctx.as_of)
        return kit.build_toolbox(self.code, guard=guard, handlers={"quant_call": _quant_handler}, audit=self._audit)

    def _read(self, ctx: SignalContext, text: str, evidence: kit.EvidenceLog) -> tuple[dict[str, Any], list[str]]:
        problems: list[str] = []
        try:
            parsed = extract_json(text)
        except ValueError as exc:
            return {}, [f"the answer is not a JSON object ({exc})"]
        try:
            from hokma.desks.core.output import SchemaError, validate_schema

            validate_schema(parsed, SCHEMA)
        except SchemaError as exc:
            return {}, [f"schema: {exc}"]
        stance, reason = parsed["stance"], parsed["reason"].strip()
        lean = ctx.facts.get("lean")
        if not 2 <= count_sentences(reason) <= 4:
            problems.append(f"the reason has {count_sentences(reason)} sentences and it needs two to four")
        if bad := contains_execution_terms(reason):
            problems.append(f"the reason speaks of sizing, exits or timing ({bad!r}); a view says none of that")
        for check in numbers.unverified_numbers(reason, evidence.values()):
            problems.append(f"the number {check.raw} is not in the criteria, the facts or a tool result of this run")
        if stance == "abstain":
            return {**parsed, "reason": reason}, problems
        if stance != "neutral" and lean == 0 and ctx.facts.get(self.profile.neutral_when or "") is True:
            problems.append("the series is too inconsistent to judge and the criteria show no strong failure: the view is neutral or an abstention")
        if stance == "bullish":
            if self.profile.positive_needs_lean and lean != 1:
                problems.append("a bullish view is not allowed: the criteria do not lean bullish and this profile waits or doubts by default")
            for key in self.profile.positive_needs:
                if ctx.facts.get(key) is not True:
                    problems.append(f"a bullish view is not allowed without {key.replace('_', ' ')}")
        differs = isinstance(lean, int) and _SIGN[stance] != lean
        if differs and not parsed["disagrees_with_criteria"]:
            problems.append("your stance differs from the lean of the criteria: say so in the reason and mark disagrees_with_criteria")
        return {**parsed, "reason": reason, "differs": bool(differs)}, problems

    def _finish(self, ctx: SignalContext, parsed: Mapping[str, Any], problems: Sequence[str], retried: bool) -> Signal:
        if parsed["stance"] == "abstain":
            return ctx.abstain(parsed["reason"], ("model_abstained",))
        cap = min(self.profile.confidence_cap, ctx.confidence_cap)
        confidence = min(int(parsed["confidence"]), cap)
        if confidence < MIN_CONFIDENCE:
            return ctx.abstain("The conviction is under the anchored scale. No view is emitted.", ("no_view",))
        criteria = ctx.criteria
        if parsed.get("differs"):
            criteria = (*criteria, Criterion(DISAGREES, 1.0, None, False, "desks.agents.investors"))
        declared = tuple(str(x) for x in ctx.facts.get("declared_missing", ()))
        try:
            return Signal(source=ctx.source, desk_id=ctx.desk_id, line=ctx.line, symbol=ctx.symbol, as_of=ctx.as_of, horizon=ctx.horizon,
                          value=_SIGN[parsed["stance"]] * confidence / 100.0, confidence=confidence, abstained=False, criteria=criteria,
                          data_missing=declared, reason=parsed["reason"], snapshot_hash=ctx.snapshot_hash, confidence_cap=cap,
                          required=ctx.required, snapshot_at=ctx.snapshot_at)
        except ValueError:
            return ctx.abstain("The model answer broke the signal contract. No view is formed.", ("reason_content",))


# ---- persistence of the views (desk_signals) ---------------------------------------------------------------------------

def persist_signal(signal: Signal, *, run_id: int | None = None, db: Database | None = None) -> int:
    """Store a signal in `desk_signals`. The seat is checked in the same transaction that writes: it has to be an active seat of the
    desk. The same view (desk, seat, symbol, cut-off and snapshot) is stored once and the existing row id comes back."""
    if signal.source not in SIGNAL_SOURCES:
        raise InvestorError(f"{signal.source!r} is not a seat that emits stored signals")
    database = db or get_database()
    stamp = to_db_time(signal.as_of)
    with database.write() as u:
        seat = u.session.one("SELECT active, line FROM desk_seats WHERE desk_id = :d AND seat_code = :s",
                             {"d": signal.desk_id, "s": signal.source})
        if seat is None or not seat["active"]:
            raise InvestorError(f"{signal.source} is not an active seat of desk {signal.desk_id}")
        if seat["line"] != signal.line:
            raise InvestorError(f"{signal.source} sits on a {seat['line']} desk, the signal says {signal.line}")
        if run_id is not None and u.session.one("SELECT id FROM run_records WHERE id = :r AND desk_id = :d", {"r": run_id, "d": signal.desk_id}) is None:
            raise InvestorError(f"run {run_id} does not belong to desk {signal.desk_id}")
        same = u.session.one("SELECT id FROM desk_signals WHERE desk_id = :d AND seat_code = :s AND symbol = :y AND as_of = :a AND snapshot_hash = :h",
                             {"d": signal.desk_id, "s": signal.source, "y": signal.symbol, "a": stamp, "h": signal.snapshot_hash})
        if same is not None:
            return int(same["id"])
        return int(u.session.insert("desk_signals", {
            "desk_id": signal.desk_id, "run_id": run_id, "seat_code": signal.source, "symbol": signal.symbol, "as_of": stamp,
            "value": signal.value, "confidence": signal.confidence, "abstained": 1 if signal.abstained else 0,
            "signal_json": signal.to_json(), "snapshot_hash": signal.snapshot_hash}))


def load_signals(desk_id: int, *, symbol: str | None = None, db: Database | None = None) -> list[Signal]:
    """The stored signals of a desk (and a symbol), oldest first, read back from their JSON."""
    database = db or get_database()
    sql, args = "SELECT signal_json FROM desk_signals WHERE desk_id = :d", {"d": desk_id}
    if symbol is not None:
        sql, args["y"] = sql + " AND symbol = :y", symbol
    with database.read() as u:
        rows = u.session.query(sql + " ORDER BY id", args)
    return [Signal.from_json(r["signal_json"]) for r in rows]


def load_signal_history(desk_id: int, before: datetime, *, db: Database | None = None, limit: int = HISTORY_LIMIT) -> pd.DataFrame:
    """Past views of the profiles of a desk as a long table (source, symbol, as_of, value, abstained), the shape `blend_signals` reads.

    Only views with a cut-off strictly before `before` enter: the history never contains the present or the future."""
    database = db or get_database()
    with database.read() as u:
        rows = u.session.query("SELECT seat_code AS source, symbol, as_of, value, abstained FROM desk_signals "
                               "WHERE desk_id = :d AND as_of < :b ORDER BY as_of DESC, id DESC LIMIT :n",
                               {"d": desk_id, "b": to_db_time(before), "n": int(limit)})
    frame = pd.DataFrame(rows, columns=["source", "symbol", "as_of", "value", "abstained"])
    frame["abstained"] = frame["abstained"].astype(bool)
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    return frame.iloc[::-1].reset_index(drop=True)


# ---- the company filter (AH-16) -----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class FilterResult:
    """The verdict of the company filter. `passes` is False when the company does not reach the debate."""

    passes: bool
    reason_code: str                       # passed, rejected_net_bearish, rejected_by_veto, no_view
    blended: float | None
    confidence: float | None
    n_used: int
    abstained: tuple[str, ...]
    merged: tuple[tuple[str, ...], ...]    # groups of profiles counted as one (correlation above 0.8)
    vetoed_by: str | None
    reject_below: float
    warnings: tuple[str, ...] = ()
    signals: tuple[Signal, ...] = field(default=(), repr=False)

    def summary(self) -> str:
        if self.reason_code == "no_view":
            return "No profile formed a view: the company does not pass the filter."
        head = "The company passes the filter" if self.passes else "The company does not pass the filter"
        return f"{head}: blended view {self.blended:+.2f} at confidence {self.confidence:.0f} from {self.n_used} profile(s); {self.reason_code}."


def veto_enabled(mandate: Any) -> bool:
    """True when a strategy of the mandate gives Nate (A17) the param veto. Nothing but the mandate turns the veto on."""
    for strategy in getattr(mandate, "strategies", ()) or ():
        for model in strategy.models:
            if model.name == "A17" and bool(model.params.get("veto") or model.params.get("veto_enabled")):
                return True
    return False


def mandate_weights(mandate: Any, strategy: str | None = None) -> dict[str, float]:
    """Weight per seat of the models of a strategy of the mandate (the first strategy when none is named)."""
    for item in getattr(mandate, "strategies", ()) or ():
        if strategy is None or item.name == strategy:
            return {m.name: float(m.weight) for m in item.models}
    return {}


def company_filter(signals: Sequence[Signal], *, history: Any = None, weights: Mapping[str, float] | None = None, veto: bool = False,
                   reject_below: float = DEFAULT_REJECT_BELOW, correlation_threshold: float = CORRELATION_THRESHOLD) -> FilterResult:
    """Mix the views with `portfolio.blend_signals` (AH-16) and decide whether the company reaches the debate.

    Abstentions are left out by the mixer; profiles whose past views correlate above the threshold in `history` count as one.
    Rejected: a veto from Nate when the mandate turns it on, or a blended view under `reject_below`. No view at all is also a
    rejection (the filter fails closed), with its own code so the cycle can tell it from a bearish company."""
    chosen = tuple(signals)
    used_weights = None
    if weights:
        names = {s.source for s in chosen}
        used_weights = {k: float(v) for k, v in weights.items() if k in names}
    vetoed = next((s.source for s in chosen if veto and s.source == "A17" and not s.abstained and s.value is not None and s.value < 0), None)
    if not chosen:
        return FilterResult(False, "no_view", None, None, 0, (), (), None, reject_below, ("no signals reached the filter",), ())
    registry.load_all()
    result = registry.call("portfolio.blend_signals", signals=[s.to_dict() for s in chosen], history=history, weights=used_weights or None,
                           correlation_threshold=correlation_threshold)
    if result.value is None:
        return FilterResult(False, "no_view", None, None, 0, tuple(s.source for s in chosen if s.abstained), (), vetoed, reject_below,
                            tuple(result.warnings), chosen)
    value = result.value
    merged = tuple(tuple(g["sources"]) for g in value["groups"] if len(g["sources"]) > 1)
    blended = float(value["value"])
    if vetoed is not None:
        code, passes = "rejected_by_veto", False
    elif blended < reject_below:
        code, passes = "rejected_net_bearish", False
    else:
        code, passes = "passed", True
    return FilterResult(passes, code, blended, float(value["confidence"]), int(value["n_used"]), tuple(value["abstained"]), merged, vetoed,
                        reject_below, tuple(result.warnings), chosen)


# ---- one filter pass of a desk -----------------------------------------------------------------------------------------

def desk_investor_seats(desk_id: int, *, db: Database | None = None) -> tuple[str, ...]:
    """The signal seats that a desk carries and that are on (read from desk_seats)."""
    database = db or get_database()
    with database.read() as u:
        rows = u.session.query("SELECT seat_code FROM desk_seats WHERE desk_id = :d AND active = 1 ORDER BY seat_code", {"d": desk_id})
    return tuple(r["seat_code"] for r in rows if r["seat_code"] in SIGNAL_SOURCES)


def run_profiles(desk_id: int, *, symbol: str, as_of: datetime, line: Line, models: Mapping[str, ChatModel], data: CompanyData,
                 params: Mapping[str, Mapping[str, Any]] | None = None, run_id: int | None = None, kit_root: Path | None = None,
                 mode: str = "live", language: str | None = None, db: Database | None = None, persist: bool = True,
                 pead_signal: Signal | None = None) -> list[Signal]:
    """Every investor seat of the desk forms its view of one company; each signal is stored in desk_signals.

    `models` maps a seat code to its chat model. A seat of the desk with no model abstains (no model, no view).
    Drew (A23) is code and comes in as `pead_signal`; without it, a PEAD desk records his abstention."""
    symbol = symbol.strip().upper()
    out: list[Signal] = []
    for code in desk_investor_seats(desk_id, db=db):
        if code == PEAD_CODE:
            from hokma.desks.agents import pead

            signal = pead_signal or pead.abstention(desk_id, symbol, as_of, ("earnings_event",))
        else:
            profile = profile_of(code)
            ctx = build_context(profile, desk_id=desk_id, line=line, symbol=symbol, as_of=as_of, data=data, params=(params or {}).get(code))
            chat = models.get(code)
            if chat is None:
                signal = ctx.abstain("No model is attached to this seat. No view is formed.", ("model",))
            else:
                agent = InvestorAgent(code, chat, kit_root=kit_root, mode=mode, language=language)
                signal = agent.signal(ctx, data)
        if persist:
            persist_signal(signal, run_id=run_id, db=db)
        out.append(signal)
    return out


def evaluate_company(desk_id: int, *, symbol: str, as_of: datetime, line: Line, models: Mapping[str, ChatModel], data: CompanyData,
                     mandate: Any = None, strategy: str | None = None, params: Mapping[str, Mapping[str, Any]] | None = None,
                     run_id: int | None = None, kit_root: Path | None = None, mode: str = "live", language: str | None = None,
                     db: Database | None = None, pead_signal: Signal | None = None,
                     reject_below: float = DEFAULT_REJECT_BELOW) -> FilterResult:
    """The filter pass of a desk (step three of the cycle): the views, their history, the mix and the verdict.

    The history read is only what was stored before the cut-off, so the mix of today never sees today or the future."""
    signals = run_profiles(desk_id, symbol=symbol, as_of=as_of, line=line, models=models, data=data, params=params, run_id=run_id,
                           kit_root=kit_root, mode=mode, language=language, db=db, pead_signal=pead_signal)
    history = load_signal_history(desk_id, as_of, db=db)
    return company_filter(signals, history=history if len(history) else None, weights=mandate_weights(mandate, strategy),
                          veto=veto_enabled(mandate), reject_below=reject_below)

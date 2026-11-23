"""Shared base of the analysts of a stock options desk (SPEC-019: B7, TA-01 to TA-04 and their pieces).

Each analyst (Adam, Sam, Tim, Ezra, Caleb) is a small module that declares its seat, its output schema and the checks
that belong to it; everything else lives here and is the same for the five:

- the system prompt: the common rules and the line brief of `hokma.desks.core.prompts`, the body of the agent file of the
  kit as the profile and, one by one, the skills that the file declares. A seat that is asked for a line its file does not
  carry is refused, so a skill written for another line never reaches the prompt;
- the tools: only the tools of the seat (`plantel.Seat.tools`), every call to a quant module scoped to the modules of the
  seat and sent through `hokma.quant.registry.call`; a guard can overwrite an argument that a control owns;
- the evidence: whatever the snapshot and the tools returned, so that every number of an answer can be traced;
- the check of the answer: the schema, no field and no phrase of an order, a size or an exit, and every number either in the
  evidence or marked `unverified`. An answer with an unverified number does not validate.

Nothing here calculates: the numbers come from the modules, the data tools or the snapshot.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from hokma.desks.core import numbers
from hokma.desks.core.graph import ChatModel, ModelReply
from hokma.desks.core.output import SchemaError, StructuredResult, parse_structured, validate_schema
from hokma.desks.core.prompts import PromptComposer
from hokma.desks.core.signal import contains_execution_terms
from hokma.desks.core.skills import Skill, SkillError, parse_skill, split_frontmatter
from hokma.desks.core.state import Message, ToolCall
from hokma.desks.core.tools import ToolCatalog, ToolFailure, ToolSpec, Toolbox
from hokma.desks.core.tool_audit import ToolAuditLog
from hokma.desks.plantel import SEATS, AnalystReport, Seat
from hokma.kit.integrity import app_root

MAX_FILE_BYTES = 200_000
MAX_SKILLS = 12
MAX_FACTS_CHARS = 60_000
MAX_PARAMS = 60
DEFAULT_TOOL_ROUNDS = 4

# Keys that no analyst answer may hold, at any depth: they are the fields of an order, a size, an exit or a signal.
FORBIDDEN_KEYS = frozenset({
    "order", "orders", "size", "sizing", "quantity", "qty", "contracts", "shares", "lots", "stop", "stop_loss", "stoploss",
    "entry", "entry_price", "entry_level", "entry_zone", "target", "target_price", "price_target", "take_profit", "exit",
    "exit_price", "exit_trigger", "exit_level", "signal", "trade_window", "trading_window", "recommendation", "action_price",
})

NUMBERS_RULE = (
    "Every number you write is copied from a tool result or from the snapshot of this run. You do not calculate, round, "
    "convert or estimate. Write counts under ten as words and copy labels such as quarters and form names exactly as a tool returned them. A number that is not in the evidence is marked unverified and "
    "your report is rejected."
)


class AnalystKitError(ValueError):
    """The seat file, a skill or the line asked for cannot be used."""


# ---- the kit ------------------------------------------------------------------------------------------------------

def default_kit_root() -> Path:
    return app_root() / "kit" / "template" / "desk"


def _read(path: Path) -> str:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            raise AnalystKitError(f"{path.name}: missing, linked or too large")
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise AnalystKitError(f"{path.name}: unreadable") from exc


@dataclass(frozen=True)
class SeatFile:
    seat: Seat
    meta: Mapping[str, Any]
    body: str
    lines: tuple[str, ...]
    skills: tuple[Skill, ...]


def load_seat_file(code: str, kit_root: Path | None = None) -> SeatFile:
    """The agent file of a seat with the skills it declares. Anything missing or malformed raises AnalystKitError."""
    seat = SEATS[code]
    root = kit_root or default_kit_root()
    try:
        meta, body = split_frontmatter(_read(root / "agents" / f"{seat.slug}.md"), seat.kit_file)
    except SkillError as exc:
        raise AnalystKitError(str(exc)) from exc
    lines = meta.get("lines")
    if not isinstance(lines, list) or not lines or not all(isinstance(x, str) for x in lines):
        raise AnalystKitError(f"{seat.kit_file}: lines must be a list")
    names = meta.get("skills") or []
    if not isinstance(names, list) or len(names) > MAX_SKILLS or len(set(map(str, names))) != len(names):
        raise AnalystKitError(f"{seat.kit_file}: skills must be a list of distinct names")
    skills: list[Skill] = []
    for name in names:
        try:
            skill = parse_skill(_read(root / "skills" / str(name) / "SKILL.md"), f"{name}/SKILL.md")
        except SkillError as exc:
            raise AnalystKitError(str(exc)) from exc
        if skill.name != name:
            raise AnalystKitError(f"skill {name}: the name inside the file differs")
        skills.append(skill)
    return SeatFile(seat, meta, body.strip(), tuple(lines), tuple(skills))


def skill_block(skill: Skill) -> str:
    do = "\n".join(f"- {p}" for p in skill.patterns)
    avoid = "\n".join(f"- {p}" for p in skill.avoid)
    return f"## Skill: {skill.name}\n{skill.identity}\n\nDo:\n{do}\n\nAvoid:\n{avoid}\n\n{skill.body}"


def answer_format(schema: Mapping[str, Any]) -> str:
    return ("## Answer format\nAnswer with one JSON object and nothing else, with exactly these fields. Use null where a value "
            "does not exist and list what is missing in data_gaps.\n" + json.dumps(schema, ensure_ascii=False, sort_keys=True))


def base_prompt(code: str, *, line: str, desk_id: int, as_of: datetime, kit_root: Path | None = None,
                composer: PromptComposer | None = None, mode: str = "live", language: str | None = None,
                lacks: str | Sequence[str] | None = None) -> str:
    """The prompt without skills: common rules, line brief and the body of the agent file as the profile."""
    seat_file = load_seat_file(code, kit_root)
    _check_line(seat_file, line)
    comp = composer or PromptComposer()
    return comp.compose(role="analyst", line=line, desk_id=desk_id, as_of=as_of, mode=mode, language=language, lacks=lacks,
                        persona=seat_file.body)


def _check_line(seat_file: SeatFile, line: str) -> None:
    if line not in seat_file.seat.lines or line not in seat_file.lines:
        raise AnalystKitError(f"{seat_file.seat.name} does not work on a {line} desk")


def system_prompt(code: str, schema: Mapping[str, Any], *, line: str, desk_id: int, as_of: datetime, kit_root: Path | None = None,
                  composer: PromptComposer | None = None, mode: str = "live", language: str | None = None,
                  lacks: str | Sequence[str] | None = None) -> str:
    """Base prompt, the skills the file declares and the answer format. Only skills of this seat file are ever added."""
    seat_file = load_seat_file(code, kit_root)
    _check_line(seat_file, line)
    base = base_prompt(code, line=line, desk_id=desk_id, as_of=as_of, kit_root=kit_root, composer=composer, mode=mode,
                       language=language, lacks=lacks)
    blocks = [base.rstrip("\n"), *(skill_block(s) for s in seat_file.skills), f"## Numbers\n{NUMBERS_RULE}", answer_format(schema)]
    return "\n\n".join(blocks) + "\n"


# ---- tools --------------------------------------------------------------------------------------------------------

_CODES = {"no_data": "no_data", "unavailable": "unavailable", "stale": "stale", "bad_input": "bad_input", "blocked": "blocked"}
Guard = Callable[[str, dict], dict]
Source = Callable[[str, datetime], Any]


def _as_failure(exc: Exception) -> ToolFailure:
    code = getattr(exc, "code", "bad_input")
    return ToolFailure(_CODES.get(code, "bad_input"), str(getattr(exc, "message", "") or exc)[:500])


def _mcp_tools() -> dict[str, tuple]:
    from hokma.kit import data_tools, exec_tools, quant_tools

    return {**data_tools.TOOLS, **exec_tools.TOOLS, **quant_tools.TOOLS}


def _mcp_spec(name: str, handlers: Mapping[str, Callable[..., Any]]) -> ToolSpec:
    table = _mcp_tools()
    if name not in table:
        raise AnalystKitError(f"tool {name!r} does not exist")
    description, props, required, fn, writes = table[name]
    if writes:
        raise AnalystKitError(f"tool {name!r} writes: an analyst does not get it")
    override = handlers.get(name)

    def handler(**args: Any) -> Any:
        unknown = sorted(set(args) - set(props))
        if unknown:
            raise ToolFailure("bad_input", f"{name}: unknown argument(s) {unknown}")
        if override is not None:
            return override(**args)
        try:
            return json.loads(fn(None, None, args))
        except ValueError as exc:
            if hasattr(exc, "code"):
                raise _as_failure(exc) from None
            raise

    return ToolSpec(name, description, {"type": "object", "properties": props, "required": required}, handler)


def _quant_spec(seat: Seat, guard: Guard | None, handlers: Mapping[str, Callable[..., Any]]) -> ToolSpec:
    from hokma.quant import registry

    allowed = seat.quant_modules

    def handler(name: str, params: Mapping[str, Any] | None = None) -> Any:
        registry.load_all()
        try:
            spec = registry.describe(str(name))
        except Exception as exc:                                              # an unknown name is a bad input
            raise ToolFailure("bad_input", f"there is no quant function named {name!r}") from exc
        if spec.module not in allowed:
            raise ToolFailure("blocked", f"{seat.name} may use the modules {', '.join(allowed)}; {name} belongs to {spec.module}")
        if params is not None and not isinstance(params, Mapping):
            raise ToolFailure("bad_input", "params must be an object")
        clean = dict(params or {})
        if len(clean) > MAX_PARAMS:
            raise ToolFailure("bad_input", f"params has more than {MAX_PARAMS} entries")
        if guard is not None:
            clean = guard(str(name), clean)
        override = handlers.get("quant_call")
        if override is not None:
            return override(name=name, params=clean)
        from hokma.kit import quant_tools

        try:
            return json.loads(quant_tools.quant_call(None, None, {"name": name, "params": clean}))
        except ValueError as exc:
            if hasattr(exc, "code"):
                raise _as_failure(exc) from None
            raise

    return ToolSpec("quant_call", f"Run one calculation of the modules {', '.join(allowed)} by name, for example {_example(allowed)}. "
                    "The answer has the value, unit, method, parameters and warnings.",
                    {"type": "object", "properties": {"name": {"type": "string"}, "params": {"type": "object"}}, "required": ["name"]},
                    handler)


def _example(modules: Sequence[str]) -> str:
    return {"indicators": "indicators.screens.weekly_structure", "stats": "stats.sentiment_score", "options": "options.vol_richness",
            "fundamentals": "fundamentals.profitability"}.get(modules[0] if modules else "", "module.function")


def _snapshot_spec(categories: Sequence[str], sources: Mapping[str, Source]) -> ToolSpec:
    from hokma.kit import data_tools

    def call(symbol: str, as_of: str, categories: Sequence[str]) -> Any:
        import hokma.data as data

        cats = list(categories or [])
        if not cats or len(cats) > 10 or not set(cats) <= set(allowed):
            raise ToolFailure("bad_input", f"categories must be some of: {', '.join(allowed)}")
        try:
            snap = data.snapshot(data_tools._symbol(symbol), as_of=data_tools._when(as_of, "as_of"), categories=cats, sources=dict(sources))
        except ValueError as exc:
            raise ToolFailure("bad_input", str(exc)) from None
        payload = snap.to_dict()
        if all(s["status"] != "ok" for s in payload["sections"].values()):
            first = next(iter(payload["sections"].values()))
            raise ToolFailure(data_tools.STATUS_CODE.get(first["status"], "unavailable"), f"{first['source']}: {first['reason']}")
        return data_tools._clean(payload)

    allowed = tuple(categories)
    return ToolSpec("data_snapshot", f"A verified, hashed picture of a symbol at as_of for these categories: {', '.join(allowed)}. "
                    "A section that could not be read says why; it is not an empty section.",
                    {"type": "object", "properties": {"symbol": {"type": "string"}, "as_of": {"type": "string"},
                                                      "categories": {"type": "array", "items": {"type": "string", "enum": list(allowed)}}},
                     "required": ["symbol", "as_of", "categories"]}, call)


def build_toolbox(code: str, *, snapshot_categories: Sequence[str] = (), sources: Mapping[str, Source] | None = None,
                  guard: Guard | None = None, handlers: Mapping[str, Callable[..., Any]] | None = None,
                  audit: ToolAuditLog | None = None) -> Toolbox:
    """The toolbox of one seat: exactly the tools of the plantel. `handlers` replace a tool body (tests, recorded data)."""
    seat = SEATS[code]
    given = dict(handlers or {})
    catalog = ToolCatalog()
    for name in seat.tools:
        if name == "quant_call":
            spec = _quant_spec(seat, guard, given)
        elif name == "data_snapshot" and snapshot_categories:
            spec = _snapshot_spec(snapshot_categories, sources or {})
            if "data_snapshot" in given:
                spec = ToolSpec(spec.name, spec.description, spec.input_schema, given["data_snapshot"])
        else:
            spec = _mcp_spec(name, given)
        catalog.register(spec)
    catalog.assign(code, seat.tools)
    return catalog.toolbox_for(code, audit=audit)


# ---- evidence and the check of an answer --------------------------------------------------------------------------

@dataclass
class RunContext:
    desk_id: int
    symbol: str
    as_of: datetime
    line: str
    facts: Mapping[str, Any] = field(default_factory=dict)


class EvidenceLog:
    """What the snapshot, the derived facts of the desk code and the tools returned in one run."""

    def __init__(self, snapshot: Any = None, facts: Any = None) -> None:
        self.snapshot, self.facts = snapshot, facts
        self.results: list[Any] = []
        self.calls: list[tuple[str, dict]] = []

    def runner(self, toolbox: Toolbox) -> Callable[[ToolCall], str]:
        def run(call: ToolCall) -> str:
            text = toolbox.run(call)
            try:
                self.results.append(json.loads(text))
            except ValueError:
                self.results.append(text)
            self.calls.append((call.name, dict(call.arguments)))
            return text
        return run

    def tools_called(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.calls)

    def quant_called(self) -> tuple[str, ...]:
        return tuple(str(a.get("name")) for n, a in self.calls if n == "quant_call")

    def values(self) -> list[float]:
        return numbers.snapshot_values([self.snapshot, self.facts, self.results])

    def strings(self) -> set[str]:
        """Every text the snapshot, the facts and the tools returned, whole: the identifiers an answer may name."""
        return {leaf for _, _, leaf in _walk([self.snapshot, self.facts, self.results]) if isinstance(leaf, str)}


@dataclass(frozen=True)
class OutputSpec:
    """What one analyst answers: the schema, the keys whose numbers are not measurements, and the checks of its own."""

    schema: Mapping[str, Any]
    exempt_keys: frozenset[str] = frozenset()
    id_keys: frozenset[str] = frozenset()               # keys that hold an identifier: it has to be a text a tool returned
    extra_forbidden: frozenset[str] = frozenset()
    checks: tuple[Callable[[dict, RunContext, EvidenceLog], list[str]], ...] = ()
    required_gaps: Callable[[RunContext, EvidenceLog], tuple[str, ...]] | None = None


@dataclass(frozen=True)
class ReportCheck:
    valid: bool
    problems: tuple[str, ...] = ()
    unverified: tuple[str, ...] = ()


def _walk(node: Any, path: str = "") -> Iterable[tuple[str, str, Any]]:
    """(path, key, leaf) of every leaf of a value."""
    if isinstance(node, Mapping):
        for key, item in node.items():
            yield from _walk(item, f"{path}.{key}" if path else str(key))
    elif isinstance(node, (list, tuple)):
        for index, item in enumerate(node):
            yield from _walk(item, f"{path}[{index}]")
    else:
        key = path.rsplit(".", 1)[-1].split("[", 1)[0]
        yield path, key, node


def _all_keys(node: Any) -> Iterable[str]:
    if isinstance(node, Mapping):
        for key, item in node.items():
            yield str(key)
            yield from _all_keys(item)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _all_keys(item)


def check_value(value: Mapping[str, Any], spec: OutputSpec, ctx: RunContext, evidence: EvidenceLog) -> ReportCheck:
    """Schema, forbidden fields and phrases, numbers against the evidence, and the checks of the seat."""
    problems: list[str] = []
    unverified: list[str] = []
    try:
        validate_schema(value, dict(spec.schema))
    except SchemaError as exc:
        return ReportCheck(False, (f"schema: {exc}",))
    banned = FORBIDDEN_KEYS | spec.extra_forbidden
    for key in _all_keys(value):
        if key.strip().lower().replace(" ", "_") in banned:
            problems.append(f"the field {key!r} is not allowed: an analyst reports, it does not order, size or exit")
    known = evidence.values()
    returned = evidence.strings() if spec.id_keys else set()
    for path, key, leaf in _walk(value):
        if isinstance(leaf, str) and key in spec.id_keys:
            if leaf not in returned:
                problems.append(f"{path}: {leaf!r} is not an identifier that a tool returned in this run")
        elif isinstance(leaf, str):
            if (bad := contains_execution_terms(leaf)):
                problems.append(f"{path}: speaks of sizing, exits or timing ({bad!r})")
            for check in numbers.unverified_numbers(leaf, known):
                unverified.append(check.raw)
                problems.append(f"{path}: the number {check.raw} is not in the evidence (unverified)")
        elif isinstance(leaf, (int, float)) and not isinstance(leaf, bool) and key not in spec.exempt_keys:
            if not numbers.is_in_snapshot(float(leaf), "number", known, rel_tol=1e-6):
                unverified.append(str(leaf))
                problems.append(f"{path}: the number {leaf} is not in the evidence (unverified)")
    for extra in spec.checks:
        problems.extend(extra(dict(value), ctx, evidence))
    return ReportCheck(not problems, tuple(problems), tuple(unverified))


def mark_text(text: str, evidence: EvidenceLog, only: Iterable[str] | None = None) -> str:
    """`text` with ` [unverified]` after each number that is not in the evidence. With `only`, just the numbers written that way
    (the parameters an answer repeats, such as a window of days, are not marked)."""
    wanted = None if only is None else set(only)
    out, last = [], 0
    for item in numbers.check_text(text, evidence.values()):
        if item.status != "unverified" or (wanted is not None and item.raw not in wanted):
            continue
        out.append(text[last:item.end])
        out.append(" [unverified]")
        last = item.end
    out.append(text[last:])
    return "".join(out)


@dataclass(frozen=True)
class AnalystResult:
    report: AnalystReport
    value: dict | None
    check: ReportCheck
    degraded: bool = False
    warnings: tuple[str, ...] = ()
    calls: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return self.check.valid and not self.degraded

    @property
    def text(self) -> str:
        """The report as the graph keeps it: summary, findings and what is missing."""
        parts = [self.report.summary, *(f"- {f}" for f in self.report.findings)]
        if self.report.data_gaps:
            parts.append("Data gaps: " + "; ".join(self.report.data_gaps))
        return "\n".join(p for p in parts if p).strip()


Render = Callable[[dict], AnalystReport]


def finalize(structured: StructuredResult, spec: OutputSpec, render: Render, ctx: RunContext, evidence: EvidenceLog) -> AnalystResult:
    """From the parsed answer to a report and its check. A degraded answer keeps its text and never validates."""
    calls = evidence.tools_called()
    if structured.value is None:
        text = mark_text(structured.text, evidence) if structured.text else ""
        problem = structured.error or (structured.warnings[0] if structured.warnings else "the answer is not a valid object")
        return AnalystResult(AnalystReport(text, (), ("structured_output_degraded",)), None, ReportCheck(False, (problem,)), True,
                             structured.warnings, calls)
    value = dict(structured.value)
    check = check_value(value, spec, ctx, evidence)
    forced = spec.required_gaps(ctx, evidence) if spec.required_gaps else ()
    report = render(value)
    gaps = tuple(dict.fromkeys((*report.data_gaps, *forced)))
    marked = check.unverified
    report = AnalystReport(mark_text(report.summary, evidence, marked), tuple(mark_text(f, evidence, marked) for f in report.findings), gaps)
    return AnalystResult(report, value, check, False, structured.warnings, calls)


def user_prompt(ctx: RunContext, snapshot: Any = None, facts: Mapping[str, Any] | None = None) -> str:
    parts = [f"Symbol: {ctx.symbol}", f"Line: {ctx.line}", f"Cut-off (UTC): {ctx.as_of.astimezone(timezone.utc).isoformat()}"]
    if snapshot is not None:
        parts.append("Snapshot:\n" + _dump(snapshot))
    if facts:
        parts.append("Facts computed by the desk code:\n" + _dump(facts))
    return "\n".join(parts) + "\n"


def _dump(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, indent=1)
    return text if len(text) <= MAX_FACTS_CHARS else text[:MAX_FACTS_CHARS] + "\n[cut]"


@dataclass(frozen=True)
class AnalystAgent:
    """One analyst: seat, output and the hooks that belong to it. The module of each analyst holds one of these."""

    code: str
    key: str
    spec: OutputSpec
    render: Render
    snapshot_categories: tuple[str, ...] = ()
    sources: Callable[[], Mapping[str, Source]] | None = None
    guard_factory: Callable[[RunContext], Guard | None] | None = None
    facts_factory: Callable[[RunContext, Mapping[str, Any]], Mapping[str, Any]] | None = None

    @property
    def seat(self) -> Seat:
        return SEATS[self.code]

    def system_prompt(self, *, line: str, desk_id: int, as_of: datetime, **kwargs: Any) -> str:
        return system_prompt(self.code, self.spec.schema, line=line, desk_id=desk_id, as_of=as_of, **kwargs)

    def toolbox(self, ctx: RunContext | None = None, *, handlers: Mapping[str, Callable[..., Any]] | None = None,
                sources: Mapping[str, Source] | None = None, audit: ToolAuditLog | None = None) -> Toolbox:
        guard = self.guard_factory(ctx) if (self.guard_factory and ctx is not None) else None
        chosen = sources if sources is not None else (self.sources() if self.sources else {})
        return build_toolbox(self.code, snapshot_categories=self.snapshot_categories, sources=chosen, guard=guard,
                             handlers=handlers, audit=audit)

    def run(self, model: ChatModel, *, desk_id: int, symbol: str, as_of: datetime, line: str = "SWO", snapshot: Any = None,
            facts: Mapping[str, Any] | None = None, kit_root: Path | None = None, composer: PromptComposer | None = None,
            handlers: Mapping[str, Callable[..., Any]] | None = None, sources: Mapping[str, Source] | None = None,
            audit: ToolAuditLog | None = None, max_tool_rounds: int = DEFAULT_TOOL_ROUNDS, mode: str = "live",
            language: str | None = None) -> AnalystResult:
        """One report. The model calls the tools of the seat for up to `max_tool_rounds` rounds and then answers with the
        object of the schema; an answer that is not valid is asked again once, and then comes back degraded."""
        ctx = RunContext(desk_id, symbol.strip().upper(), as_of, line)
        derived = dict(facts or {})
        if self.facts_factory:
            derived.update(self.facts_factory(ctx, derived))
        ctx.facts = derived
        evidence = EvidenceLog(snapshot, derived)
        toolbox = self.toolbox(ctx, handlers=handlers, sources=sources, audit=audit)
        run_tool = evidence.runner(toolbox)
        system = self.system_prompt(line=line, desk_id=desk_id, as_of=as_of, kit_root=kit_root, composer=composer, mode=mode,
                                    language=language)
        messages: list[Message] = [Message("user", user_prompt(ctx, snapshot, derived), id="u0")]
        schemas = toolbox.schemas()
        rounds, retried, serial = 0, False, 0
        while True:
            offered = schemas if rounds < max_tool_rounds else []
            reply: ModelReply = model.chat(system, messages, offered)
            if reply.tool_calls and offered:
                serial += 1
                messages.append(Message("assistant", reply.content or "", id=f"a{serial}", tool_calls=tuple(reply.tool_calls)))
                for call in reply.tool_calls:
                    messages.append(Message("tool", run_tool(call), tool_call_id=call.id, name=call.name))
                rounds += 1
                continue
            parsed = parse_structured(reply.content or "", dict(self.spec.schema))
            if parsed.degraded and not retried:
                retried = True
                messages.append(Message("assistant", reply.content or "", id="a-final"))
                messages.append(Message("user", "That answer was not valid: " + "; ".join(parsed.warnings)
                                        + ". Answer again with only the JSON object of the schema.", id="u-retry"))
                rounds = max_tool_rounds
                continue
            return finalize(parsed, self.spec, self.render, ctx, evidence)


def merge_results(sections: Mapping[str, Any], *, as_of: datetime) -> Any:
    """One `DataResult` out of several: ok when any part is ok (each part keeps its own status inside the value), otherwise the
    first part that failed, so a source that did not answer is never read as an empty one."""
    from hokma.data import sentinels

    cut = sentinels.parse_time(as_of)
    parts = {name: result for name, result in sections.items()}
    good = {name: r for name, r in parts.items() if r.ok}
    if not good:
        return next(iter(parts.values()))
    value = {name: r.to_dict() for name, r in parts.items()}
    warnings = tuple(f"{name}: {w}" for name, r in parts.items() for w in r.warnings)
    return sentinels.ok(value, source="+".join(sorted(good)), as_of=cut, received_at=sentinels.utcnow(), warnings=warnings)


def lines_text(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(i for i in (str(x).strip() for x in items) if i)

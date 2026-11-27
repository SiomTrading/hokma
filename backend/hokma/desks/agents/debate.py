"""The debate of the desk tribunal: Jonah (A08) for, Simon (A09) against, a moderator and a counter cut (SPEC-019: B9).

Code decides what a model must not decide: how many rounds are played (`should_continue`), whether an answer is valid
(`BullOutput.from_dict`, `BearOutput.from_dict`), whether a refuted argument came back without new data
(`revived_claims`) and which arguments the moderator may call refuted (only arguments that a side really made).

Rules this module enforces:
- The advocates have no tools. `advocate_toolbox` is empty, and a reply that asks for a tool fails.
- Every point cites a key of the snapshot, and the reference must exist there. A point without data is rejected.
- Every number in an answer must be in the snapshot (or in the output of a module the snapshot carries). The others are
  `unverified` and the answer does not validate.
- An advocate never decides: an answer that holds a sizing, exit, objective or entry phrase is rejected.
- The first speaker gets the opening marker and cannot invent a rebuttal of a side that has not spoken.
- The state does not grow with the rounds: each round is folded into a rolling summary of bounded size and the ledger of
  arguments is bounded.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from hokma.desks.agents.debate_output import (  # noqa: F401  (re-exported: the tribunal modules and the tests import them from here)
    BEAR_SCHEMA, BULL_SCHEMA, FORBIDDEN_DECISION_KEYS, MAX_CATALYSTS, MAX_DISPROVE, MAX_POINTS, MAX_TEXT, MISSING, MODERATOR_SCHEMA, SEVERITIES, BearOutput,
    BullOutput, Concern, DebateError, EvidenceRef, OutputError, Point, _check_rebuttal, _distinct, _EVIDENCE_SCHEMA, _guard, _list, _object, _refs,
    _schema_check, _text, _tokens, _WORD, check_numbers, resolve_ref, similarity,
)
from hokma.desks.core.graph import ChatModel
from hokma.desks.core.prompts import PromptComposer
from hokma.desks.core.signal import LINES
from hokma.desks.core.skills import SkillError, parse_skill, split_frontmatter
from hokma.desks.core.state import (
    DebateState, Message, clip, new_debate, opponent_or_opening, record_round,
)
from hokma.desks.core.tools import ToolCatalog, Toolbox
from hokma.desks.plantel import SEATS, seat_file
from hokma.kit.integrity import app_root
from hokma.llm.jsonx import LLMParseError, extract_json

# ---- round profile of each line (TA-18) ----------------------------------------------------------------------------

LINE_ROUNDS: Mapping[str, int] = MappingProxyType({"RF": 2, "DT": 1, "SWC": 2, "SWO": 3, "LP": 3})
HARD_MAX_ROUNDS = 5               # no line, no override and no model goes past this
BULL, BEAR = "A08", "A09"
JUDGE = "A10"
TRIBUNAL_SEATS = (BULL, BEAR, JUDGE)
MAX_REFUTED = 12                  # bound of the ledger: the state never grows with the rounds
MAX_REPORT_CHARS = 1500
MAX_SNAPSHOT_CHARS = 16_000
MAX_FILE_BYTES = 200_000
SIMILARITY = 0.7
DEFAULT_KIT = ("kit", "template", "desk")
_LINE_BLOCK = re.compile(r"^### Line (RF|DT|SWC|SWO|LP)\s*$")


class PromptScopeError(DebateError):
    """A seat file that is not written for the line asked, or a seat that is not of the tribunal."""


# ---- profile and counter cut (TA-18) -------------------------------------------------------------------------------

def round_cap(line: str, override: int | None = None) -> int:
    """Rounds of the line profile (SWO and LP three, DT one). An override may raise or lower it but never pass the hard cap."""
    if line not in LINES:
        raise DebateError(f"line must be one of {LINES}")
    if override is None:
        return LINE_ROUNDS[line]
    if isinstance(override, bool) or not isinstance(override, int) or override < 1:
        raise DebateError("max_rounds must be a whole number of at least 1")
    return min(override, HARD_MAX_ROUNDS)


def should_continue(rounds_done: int, *, line: str, moderator_wants_more: bool = True, max_rounds: int | None = None) -> bool:
    """The cut of the debate. Code decides: at least one round is always played, the cap of the line is never passed, and
    the moderator can only shorten what the cap allows. A model that asks to go on does not pass the cap."""
    cap = round_cap(line, max_rounds)
    if rounds_done < 1:
        return True
    if rounds_done >= cap:
        return False
    return bool(moderator_wants_more)


# ---- opening marker (TA-30) ----------------------------------------------------------------------------------------

def opening(opponent: str) -> str:
    """The marker the first speaker reads where the other side's argument would be. It never holds an argument."""
    return opponent_or_opening("", opponent)


def opponent_argument_or_opening(text: str | None, opponent: str) -> str:
    """What the other side said, or the opening marker when it has not spoken."""
    return opponent_or_opening(text, opponent)


def parse_bull(data: Any, *, snapshot: Mapping[str, Any], opening_turn: bool) -> BullOutput:
    return BullOutput.from_dict(data, snapshot=snapshot, opening_turn=opening_turn)


def parse_bear(data: Any, *, snapshot: Mapping[str, Any], opening_turn: bool) -> BearOutput:
    return BearOutput.from_dict(data, snapshot=snapshot, opening_turn=opening_turn)


# ---- claim identity, ledger and revival (HFC-008) ---------------------------------------------------------------------

@dataclass(frozen=True)
class Claim:
    side: str
    claim: str
    refs: tuple[str, ...]


@dataclass(frozen=True)
class RefutedArgument:
    side: str
    claim: str
    reason: str
    refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class DebateLedger:
    """What the moderator tracks: claims still standing and claims that died. Both are bounded."""

    live: tuple[Claim, ...] = ()
    refuted: tuple[RefutedArgument, ...] = ()
    rounds: int = 0

    def lines(self) -> str:
        return "\n".join(f"- [{r.side}] {clip(r.claim, 160)} ({clip(r.reason, 120)})" for r in self.refuted) or "(none)"


def revived_claims(side: str, claims: Sequence[tuple[str, Sequence[str]]], ledger: DebateLedger) -> list[str]:
    """Claims of `side` that repeat a refuted argument of the same side without data the refuted version did not have."""
    revived: list[str] = []
    for claim, refs in claims:
        for dead in ledger.refuted:
            if dead.side == side and similarity(claim, dead.claim) >= SIMILARITY and set(refs) <= set(dead.refs):
                revived.append(claim)
                break
    return revived


def _fold_live(ledger: DebateLedger, bull: BullOutput | None, bear: BearOutput | None) -> list[Claim]:
    live = list(ledger.live)
    for side, output in (("bull", bull), ("bear", bear)):
        if output is not None:
            live += [Claim(side, c, tuple(r)) for c, r in output.claims()]
    return live


# ---- moderator (HFC-008) ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ModeratorNote:
    refuted: tuple[RefutedArgument, ...]
    central_disagreement: str
    another_round: bool
    discarded: tuple[str, ...] = ()       # claims the moderator named that no side made

    def to_dict(self) -> dict[str, Any]:
        return {"refuted": [{"side": r.side, "claim": r.claim, "reason": r.reason} for r in self.refuted],
                "central_disagreement": self.central_disagreement, "another_round": self.another_round}


def parse_moderator(data: Any, *, snapshot: Mapping[str, Any], ledger: DebateLedger, bull: BullOutput | None, bear: BearOutput | None
                    ) -> tuple[ModeratorNote, DebateLedger]:
    """The note of the moderator and the ledger after it. A refuted argument must be one a side really made: the others are
    dropped and reported in `discarded`. The references of a refuted argument come from the argument, not from the moderator."""
    if isinstance(data, str):
        try:
            data = extract_json(data)
        except LLMParseError as exc:
            raise OutputError("moderator: the answer holds no JSON object") from exc
    if not isinstance(data, Mapping) or set(map(str, data)) - {"refuted", "central_disagreement", "another_round"}:
        raise OutputError("moderator: expected refuted, central_disagreement and another_round")
    _schema_check(data, MODERATOR_SCHEMA, "moderator")
    central = _text(data.get("central_disagreement"), "central_disagreement", limit=400)
    if not isinstance(data.get("another_round"), bool):
        raise OutputError("another_round: expected true or false")
    pool = _fold_live(ledger, bull, bear)
    kept: list[RefutedArgument] = []
    discarded: list[str] = []
    for index, item in enumerate(_list(data.get("refuted"), "refuted", minimum=0, maximum=MAX_REFUTED)):
        if not isinstance(item, Mapping) or set(item) - {"side", "claim", "reason"}:
            raise OutputError(f"refuted[{index}]: expected side, claim and reason")
        side, claim, reason = item.get("side"), _text(item.get("claim"), f"refuted[{index}].claim"), _text(item.get("reason"), f"refuted[{index}].reason", limit=300)
        match = max((c for c in pool if c.side == side), key=lambda c: similarity(c.claim, claim), default=None)
        if match is None or similarity(match.claim, claim) < SIMILARITY:
            discarded.append(claim)
            continue
        kept.append(RefutedArgument(match.side, match.claim, reason, match.refs))
    _guard([central, *(r.reason for r in kept)], snapshot)
    dead_keys = {(r.side, r.claim) for r in kept}
    live = tuple(c for c in pool if (c.side, c.claim) not in dead_keys)[-MAX_REFUTED:]
    refuted = tuple(_dedupe([*ledger.refuted, *kept])[-MAX_REFUTED:])
    note = ModeratorNote(tuple(kept), central, data["another_round"], tuple(discarded))
    return note, DebateLedger(live, refuted, ledger.rounds + 1)


def _dedupe(items: Sequence[RefutedArgument]) -> list[RefutedArgument]:
    out: list[RefutedArgument] = []
    for item in items:
        if not any(o.side == item.side and similarity(o.claim, item.claim) >= 0.9 for o in out):
            out.append(item)
    return out


# ---- prompts -----------------------------------------------------------------------------------------------------------

def default_kit_root() -> Path:
    return app_root().joinpath(*DEFAULT_KIT)


def _read_kit_file(path: Path) -> str:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            raise PromptScopeError(f"kit file {path.name} is missing or not a plain file")
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PromptScopeError(f"kit file {path.name} cannot be read") from exc


def only_line_blocks(body: str, line: str) -> str:
    """The agent text with the `### Line X` blocks of the other lines removed: a line never reads the variant of another."""
    out: list[str] = []
    skipping = False
    for text in body.splitlines():
        found = _LINE_BLOCK.match(text)
        if found:
            skipping = found.group(1) != line
        elif text.startswith("#"):
            skipping = False
        if not skipping:
            out.append(text)
    return "\n".join(out).strip()


def skill_block(text: str, name: str) -> str:
    skill = parse_skill(text, f"{name}/SKILL.md")
    patterns = "\n".join(f"- {p}" for p in skill.patterns)
    avoid = "\n".join(f"- {a}" for a in skill.avoid)
    return f"## Skill: {skill.name}\n{skill.identity}\n\nDo:\n{patterns}\n\nAvoid:\n{avoid}\n\n{skill.body}"


def seat_prompt(code: str, *, line: str, desk_id: int, as_of: datetime, kit_root: Path | None = None,
                composer: PromptComposer | None = None, language: str | None = None, extra: Sequence[str] = ()) -> str:
    """The system prompt of a tribunal seat: the common rules of the role, the seat file with the variant of this line only,
    and the skills that seat file declares. A seat file that does not carry `line` is refused, so the text written for
    one line never reaches a desk of another."""
    if code not in TRIBUNAL_SEATS:
        raise PromptScopeError(f"{code} is not a seat of the tribunal")
    if line not in LINES:
        raise PromptScopeError(f"line must be one of {LINES}")
    seat = SEATS[code]
    root = kit_root if kit_root is not None else default_kit_root()
    text = _read_kit_file(seat_file(root, seat.slug))
    try:
        meta, body = split_frontmatter(text, seat.kit_file)
    except SkillError as exc:
        raise PromptScopeError(str(exc)) from exc
    lines = meta.get("lines")
    if not isinstance(lines, list) or line not in lines:
        raise PromptScopeError(f"{seat.slug} is not written for a {line} desk")
    names = meta.get("skills") or []
    if not isinstance(names, list) or not all(isinstance(n, str) and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", n) for n in names):
        raise PromptScopeError(f"{seat.slug}: bad skills list")
    blocks = [skill_block(_read_kit_file(root / "skills" / n / "SKILL.md"), n) for n in names]
    role = "judge" if code == JUDGE else "researcher"
    composed = (composer or PromptComposer()).compose(role=role, line=line, desk_id=desk_id, as_of=as_of, language=language)
    parts = [composed.rstrip(), f"## Seat: {seat.name} ({seat.code})\n{only_line_blocks(body, line)}", *blocks, *extra]
    return "\n\n".join(parts) + "\n"


def advocate_toolbox(code: str) -> Toolbox:
    """The tools of an advocate: none. Asking for any tool raises `ToolNotAllowed`."""
    if code not in (BULL, BEAR):
        raise DebateError(f"{code} is not an advocate")
    seat = SEATS[code]
    catalog = ToolCatalog()
    catalog.assign(seat.name, seat.tools)
    return catalog.toolbox_for(seat.name)


def render_snapshot(snapshot: Mapping[str, Any]) -> str:
    """The snapshot as JSON for a prompt, within a size budget: whole top-level keys, in order, until the budget is used."""
    kept: dict[str, Any] = {}
    for key, value in snapshot.items():
        trial = {**kept, key: value}
        if len(json.dumps(trial, ensure_ascii=False, default=str)) > MAX_SNAPSHOT_CHARS:
            break
        kept = trial
    text = json.dumps(kept, ensure_ascii=False, default=str, indent=1)
    omitted = [k for k in snapshot if k not in kept]
    return text + (f"\n(omitted for size: {', '.join(map(str, omitted))})" if omitted else "")


def reports_block(reports: Mapping[str, str]) -> str:
    return "\n\n".join(f"### {key}\n{clip(str(text), MAX_REPORT_CHARS)}" for key, text in reports.items()) or "(no analyst reports)"


def turn_message(*, side: str, round_index: int, cap: int, reports: Mapping[str, str], snapshot: Mapping[str, Any],
                 state: DebateState, ledger: DebateLedger, opponent_text: str | None) -> str:
    """What a speaker reads each turn: the reports, the snapshot, the rolling summary (never the history), the refuted
    arguments and the last argument of the other side, or the opening marker."""
    other = "bear" if side == "bull" else "bull"
    return (f"Analyst reports:\n{reports_block(reports)}\n\nSnapshot:\n{render_snapshot(snapshot)}\n\n"
            f"Debate so far (summary): {state['summary'] or '(none)'}\n\n"
            f"Arguments already refuted (they may return only with new data):\n{ledger.lines()}\n\n"
            f"Round {round_index} of at most {cap}. You speak for the {side} side.\n\n"
            f"Last argument of the {other} side:\n{opponent_argument_or_opening(opponent_text, other)}")


# ---- the run ------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class DebateResult:
    rounds: int
    stop_reason: str                      # "cap", "settled", "invalid_output" or "revival"
    bull: BullOutput | None
    bear: BearOutput | None
    summary: str
    state: DebateState
    ledger: DebateLedger
    note: ModeratorNote | None = None
    error: str = ""
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _ask(model: ChatModel, system: str, user: str, parse: Callable[[Any], Any], toolbox: Toolbox) -> Any:
    """One turn with one retry. A reply that asks for a tool fails (ToolNotAllowed); an answer that does not validate is
    asked again once with the reason, then raises OutputError."""
    message = user
    for attempt in range(2):
        reply = model.chat(system, [Message("user", message)], ())
        for call in reply.tool_calls:
            toolbox.call(call.name, call.arguments)          # an empty toolbox raises ToolNotAllowed
        try:
            return parse(reply.content or "")
        except OutputError as exc:
            if attempt == 1:
                raise
            message = f"{user}\n\nYour previous answer was rejected: {exc}. Answer again with the corrected JSON object only."
    raise OutputError("unreachable")


def moderate(model: ChatModel, *, ledger: DebateLedger, bull: BullOutput | None, bear: BearOutput | None, line: str, desk_id: int,
             as_of: datetime, snapshot: Mapping[str, Any], rounds_done: int, cap: int, kit_root: Path | None = None,
             composer: PromptComposer | None = None, language: str | None = None, system: str | None = None
             ) -> tuple[ModeratorNote, DebateLedger]:
    """Close a round: the judge seat acts as moderator, with no tools, and lists what died, the central disagreement and
    whether another round would add information. Code applies the cap; this only says if more would help."""
    prompt = system or seat_prompt(JUDGE, line=line, desk_id=desk_id, as_of=as_of, kit_root=kit_root, composer=composer, language=language,
                                   extra=[MODERATOR_BRIEF])
    user = (f"Close round {rounds_done} of at most {cap}.\n\nSnapshot:\n{render_snapshot(snapshot)}\n\n"
            f"Arguments already refuted:\n{ledger.lines()}\n\n"
            f"Argument of the bull side this round:\n{bull.render() if bull else '(none)'}\n\n"
            f"Argument of the bear side this round:\n{bear.render() if bear else '(none)'}")
    box = judge_toolbox()
    return _ask(model, prompt, user, lambda text: parse_moderator(text, snapshot=snapshot, ledger=ledger, bull=bull, bear=bear), box)


MODERATOR_BRIEF = (
    "## Moderator mode\nYou are closing a round of the debate, not judging the case. Answer with one JSON object and nothing else: "
    '{"refuted": [{"side": "bull" | "bear", "claim": "the claim as its side wrote it", "reason": "why it fell"}], '
    '"central_disagreement": "one question the evidence can settle", "another_round": true | false}. '
    "List only arguments that a side really made and the other side answered with data. Do not rate the case.")


def judge_toolbox() -> Toolbox:
    """The tools of the judge while it judges or moderates: none (TA-34). The evidence phase has its own set in judge.py."""
    catalog = ToolCatalog()
    catalog.assign("judgment", ())
    return catalog.toolbox_for("judgment")


def run_debate(bull_model: ChatModel, bear_model: ChatModel, moderator_model: ChatModel, *, line: str, desk_id: int, as_of: datetime,
               snapshot: Mapping[str, Any], reports: Mapping[str, str], max_rounds: int | None = None, kit_root: Path | None = None,
               composer: PromptComposer | None = None, language: str | None = None) -> DebateResult:
    """Jonah against Simon, with a moderator at the close of each round and the counter cut of the line.

    Each round: the bull speaks (the first speaker gets the opening marker), the bear answers, the moderator closes. Code
    decides whether another round follows. An answer that does not validate after one retry, or an argument that comes back
    refuted without new data after one retry, ends the debate with the reason; what was valid before it is kept."""
    cap = round_cap(line, max_rounds)
    bull_system = seat_prompt(BULL, line=line, desk_id=desk_id, as_of=as_of, kit_root=kit_root, composer=composer, language=language)
    bear_system = seat_prompt(BEAR, line=line, desk_id=desk_id, as_of=as_of, kit_root=kit_root, composer=composer, language=language)
    mod_system = seat_prompt(JUDGE, line=line, desk_id=desk_id, as_of=as_of, kit_root=kit_root, composer=composer, language=language,
                             extra=[MODERATOR_BRIEF])
    bull_box, bear_box = advocate_toolbox(BULL), advocate_toolbox(BEAR)
    state, ledger = new_debate(), DebateLedger()
    bull: BullOutput | None = None
    bear: BearOutput | None = None
    note: ModeratorNote | None = None
    done, reason, error = 0, "cap", ""
    while True:
        index = done + 1
        try:
            def parse_bull_turn(text: str, _l: DebateLedger = ledger, _first: bool = bull is None) -> BullOutput:
                out = parse_bull(text, snapshot=snapshot, opening_turn=_first)
                if dead := revived_claims("bull", out.claims(), _l):
                    raise OutputError(f"refuted argument came back without new data: {dead[0]!r}")
                return out
            bull = _ask(bull_model, bull_system, turn_message(side="bull", round_index=index, cap=cap, reports=reports, snapshot=snapshot,
                                                              state=state, ledger=ledger, opponent_text=bear.render() if bear else None),
                        lambda text: parse_bull_turn(text), bull_box)
            state = record_round(state, "bull", bull.render())

            def parse_bear_turn(text: str, _l: DebateLedger = ledger) -> BearOutput:
                out = parse_bear(text, snapshot=snapshot, opening_turn=False)
                if dead := revived_claims("bear", out.claims(), _l):
                    raise OutputError(f"refuted argument came back without new data: {dead[0]!r}")
                return out
            bear = _ask(bear_model, bear_system, turn_message(side="bear", round_index=index, cap=cap, reports=reports, snapshot=snapshot,
                                                              state=state, ledger=ledger, opponent_text=bull.render()),
                        lambda text: parse_bear_turn(text), bear_box)
            state = record_round(state, "bear", bear.render())
            note, ledger = moderate(moderator_model, ledger=ledger, bull=bull, bear=bear, line=line, desk_id=desk_id, as_of=as_of,
                                    snapshot=snapshot, rounds_done=index, cap=cap, system=mod_system)
        except OutputError as exc:
            reason = "revival" if "came back" in str(exc) else "invalid_output"
            error = str(exc)
            break
        done = index
        if not should_continue(done, line=line, moderator_wants_more=note.another_round, max_rounds=max_rounds):
            reason = "cap" if done >= cap else "settled"
            break
    return DebateResult(done, reason, bull, bear, state["summary"], state, ledger, note, error)

"""The plantel: the fixed registry of the seats a desk can carry (SPEC-019: section 4.2).

A seat is a code (A01..A23), a proper name, the kit file of its agent, the nearest role of `desk_agents.role`, the lines
whose desks carry it, the MCP tools it may call and its default model. Only data lives here: no seat runs anything.

Seats A03 and A05 are reserved codes that no desk carries yet, so they are not in `SEATS`. The investors (A16 to A23)
answer with the `Signal` contract; every other seat answers with one of the small output types below. The agent code of
each seat (`code_module`) builds its prompt and parses its answer on top of these types.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping

from hokma.db.repos.desks import MODELS, ROLES
from hokma.desks.agents.debate_output import BearOutput, BullOutput
from hokma.desks.core.signal import LINES, Line, Signal

KIT_AGENTS_DIR = "kit/template/agents"       # one file per seat, in the main agents folder of the kit: what a desk adds is state (model, on or off, tasks), never a copy
CODE_PACKAGE = "backend/hokma/desks/agents"
SWO_VARIANTS = ("traditional", "pead")
PEAD_SEAT = "A23"
Model = Literal["haiku", "sonnet", "opus"]

# Seats that need no tool at all: they argue or judge from what the graph hands them.
NO_TOOLS: tuple[str, ...] = ()


# ---- structured outputs --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class AnalystReport:
    """What an analyst hands to the graph: a readable report and the gaps it could not close."""

    summary: str
    findings: tuple[str, ...] = ()
    data_gaps: tuple[str, ...] = ()


@dataclass(frozen=True)
class JudgeVerdict:
    """The judge rating on the anchored scale; `REVIEW` when the case is ambiguous."""

    rating: str
    rationale: str
    evidence_requested: tuple[str, ...] = ()


@dataclass(frozen=True)
class TraderPlan:
    """The trader hands over a typed proposal (its fields follow the line schema) and the reason for it."""

    proposal: Mapping[str, Any] | None
    rationale: str


@dataclass(frozen=True)
class Evidence:
    """One figure a risk voice stands on: where it came from (`quant:<function>`, `book` or `context`), what it is and its value as written."""

    source: str
    name: str
    value: str


@dataclass(frozen=True)
class RiskPoint:
    """One claim of a risk voice. A claim with no evidence is not accepted."""

    claim: str
    evidence: tuple[Evidence, ...] = ()


@dataclass(frozen=True)
class RiskArgument:
    """One voice of the risk committee (Jake or Glen): a stance and the points that carry it. It has no limit and no decision."""

    stance: Literal["aggressive", "conservative"]
    summary: str
    points: tuple[RiskPoint, ...] = ()
    data_gaps: tuple[str, ...] = ()


@dataclass(frozen=True)
class RiskVerdict:
    """The verdict of the arbiter (Reed): the risk of the proposal and the limits it asks for. It does not decide the order."""

    risk_level: Literal["low", "moderate", "high", "severe"]
    summary: str
    points: tuple[RiskPoint, ...] = ()
    limits: Mapping[str, Any] = field(default_factory=dict)
    open_questions: tuple[str, ...] = ()


@dataclass(frozen=True)
class ManagerReport:
    """The desk manager closing note: final rating, a grade for the run and the lessons it proposes."""

    rating: str
    grade: str
    summary: str
    lessons: tuple[str, ...] = ()


# ---- seats ---------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Seat:
    code: str                          # "A01".."A23"
    name: str                          # "Adam"
    slug: str                          # "market-analyst"
    desk_role: str                     # nearest value of desk_agents.role
    lines: frozenset[Line]             # lines whose desks carry this seat
    tools: tuple[str, ...]             # MCP tools the seat may call; anything else fails (H1 desks.core.tools)
    output: type                       # structured output schema (H1 desks.core.output)
    default_model: Model
    code_module: str = ""              # python file with the prompt and the parsing of the seat
    quant_modules: tuple[str, ...] = ()  # when "quant_call" is a tool: the modules of the registry the seat may reach

    def __post_init__(self) -> None:
        if self.desk_role not in ROLES:
            raise ValueError(f"{self.code}: desk_role {self.desk_role!r} is not a desk_agents role")
        if self.default_model not in MODELS:
            raise ValueError(f"{self.code}: default_model {self.default_model!r} is not a model")
        if not self.lines or not set(self.lines) <= set(LINES):
            raise ValueError(f"{self.code}: lines must be a non empty set of {LINES}")
        if self.quant_modules and "quant_call" not in self.tools:
            raise ValueError(f"{self.code}: quant_modules need the quant_call tool")

    @property
    def kit_file(self) -> str:
        """Path of the seat agent file in the kit, relative to the repository root."""
        return f"{KIT_AGENTS_DIR}/{self.slug}.md"


def seat_file(root: Path, slug: str) -> Path:
    """The kit file of the agent of a seat. `root` is the desk part of the kit (`kit/template/desk`): the agent is in `agents/` beside it, the one folder where
    every agent of the kit lives. A kit of a test may carry its own `agents/` inside `root`, and that one wins."""
    own = root / "agents" / f"{slug}.md"
    return own if own.is_file() else root.parent / "agents" / f"{slug}.md"


def _seat(code: str, name: str, slug: str, role: str, lines: str, tools: tuple[str, ...], output: type, model: Model,
          module: str, quant: tuple[str, ...] = ()) -> Seat:
    return Seat(code, name, slug, role, frozenset(lines.split()), tools, output, model, f"{CODE_PACKAGE}/{module}.py", quant)


_ALL = "RF DT SWC SWO LP"
_FIVE = "DT SWC SWO LP"
_INVESTOR_TOOLS = ("quant_call",)

_SEAT_LIST: tuple[Seat, ...] = (
    _seat("A01", "Adam", "market-analyst", "research", _FIVE, ("data_bars", "data_snapshot", "quant_call"),
          AnalystReport, "sonnet", "market", ("indicators", "stats")),
    _seat("A02", "Sam", "sentiment-analyst", "research", _FIVE, ("data_snapshot", "quant_call"),
          AnalystReport, "haiku", "sentiment", ("stats",)),
    _seat("A04", "Tim", "news-macro-analyst", "research", _ALL,
          ("data_snapshot", "data_macro", "data_filings", "calendar_events"), AnalystReport, "sonnet", "news"),
    _seat("A06", "Ezra", "fundamentals-analyst", "research", "SWO LP",
          ("data_fundamentals", "data_filings", "quant_call"), AnalystReport, "sonnet", "fundamentals", ("fundamentals",)),
    _seat("A07", "Caleb", "options-analyst", "research", "SWO LP", ("quant_call", "contract_get"),
          AnalystReport, "sonnet", "options", ("options", "stats")),
    _seat("A08", "Jonah", "bull-advocate", "bull", _FIVE, NO_TOOLS, BullOutput, "sonnet", "debate"),
    _seat("A09", "Simon", "bear-advocate", "bear", _FIVE, NO_TOOLS, BearOutput, "sonnet", "debate"),
    _seat("A10", "Amos", "desk-judge", "judge", _ALL, ("quant_call",), JudgeVerdict, "opus", "judge", ("validation",)),
    _seat("A11", "Isaac", "trader", "execution", _ALL, ("exec_propose", "exec_status", "contract_get"),
          TraderPlan, "sonnet", "trader"),
    _seat("A12", "Jake", "risk-bold", "risk", _FIVE, ("quant_call", "book_snapshot"), RiskArgument, "sonnet", "risk", ("risk",)),
    _seat("A13", "Glen", "risk-careful", "risk", _FIVE, ("quant_call", "book_snapshot"), RiskArgument, "sonnet", "risk", ("risk",)),
    _seat("A14", "Reed", "risk-arbiter", "risk", _ALL, ("quant_call", "book_snapshot", "desk_configure"),
          RiskVerdict, "opus", "risk", ("risk", "options")),
    _seat("A15", "Grant", "desk-manager", "chief", _ALL, ("book_pnl", "lessons_propose", "schedule_run"),
          ManagerReport, "sonnet", "manager"),
    _seat("A16", "Owen", "quality-owner", "signals", "SWO LP", _INVESTOR_TOOLS, Signal, "sonnet", "investors", ("fundamentals",)),
    _seat("A17", "Nate", "inversion-veto", "signals", "SWO LP", _INVESTOR_TOOLS, Signal, "sonnet", "investors", ("fundamentals",)),
    _seat("A18", "Gabe", "safety-margin", "signals", "SWO LP", _INVESTOR_TOOLS, Signal, "sonnet", "investors", ("fundamentals",)),
    _seat("A19", "Paul", "fair-growth", "signals", "SWO LP", _INVESTOR_TOOLS, Signal, "sonnet", "investors", ("fundamentals",)),
    _seat("A20", "Ray", "inflection-hunter", "signals", "SWC SWO LP", _INVESTOR_TOOLS, Signal, "sonnet", "investors",
          ("fundamentals", "stats")),
    _seat("A21", "Noah", "contrarian-catalyst", "signals", "SWC SWO LP", _INVESTOR_TOOLS, Signal, "sonnet", "investors",
          ("fundamentals",)),
    _seat("A22", "Ruth", "disruption-builder", "signals", "SWO LP", _INVESTOR_TOOLS, Signal, "sonnet", "investors",
          ("fundamentals",)),
    _seat("A23", "Drew", "earnings-drift", "signals", "SWO", ("quant_call", "data_filings"), Signal, "sonnet", "pead",
          ("indicators", "stats")),
)

SEATS: Mapping[str, Seat] = MappingProxyType({seat.code: seat for seat in _SEAT_LIST})


def seats_for(line: Line, variant: str | None) -> tuple[Seat, ...]:
    """The seats a desk of `line` carries, by code. SWO traditional: every seat of the line but A23; SWO pead: all of them.

    A `variant` is only meaningful for SWO (`traditional` when None, or `pead`); any other line takes none."""
    if line not in LINES:
        raise ValueError(f"line must be one of {LINES}")
    if line == "SWO":
        chosen = "traditional" if variant is None else variant
        if chosen not in SWO_VARIANTS:
            raise ValueError(f"variant of an SWO desk must be one of {SWO_VARIANTS}")
    elif variant is not None:
        raise ValueError(f"a {line} desk takes no variant yet")
    else:
        chosen = None
    return tuple(s for s in SEATS.values() if line in s.lines and (s.code != PEAD_SEAT or chosen == "pead"))

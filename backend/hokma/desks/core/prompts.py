"""Prompt pieces for the agents of a desk: one source of instructions (SPEC-017: OA-20, TA-42, TA-41, AH-14, AH-15).

A prompt is assembled from pieces chosen by role and line. The common rules live as files under
`kit/template/desk/rules/`; role and line briefs live here. A piece that belongs to one desk never enters the prompt of
another desk, and a composer holds no global state: two desks never share one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

import yaml

from hokma import i18n
from hokma.desks.core.signal import LINES, contains_execution_terms
from hokma.desks.core.skills import split_frontmatter
from hokma.kit.integrity import app_root

ROLES: tuple[str, ...] = ("analyst", "researcher", "judge", "trader", "risk", "investor", "chief")
# Roles whose job includes sizing, exits and objectives: only they may hold such words in their own brief.
EXECUTION_ROLES = frozenset({"trader", "risk"})

DESK_KIT = ("kit", "template", "desk")
COMMON_RULES: tuple[str, ...] = ("desk-base", "collaboration", "citations", "source-policy", "no-lookahead", "scope-honesty")
VALIDATIONS_FILE = "validations.yaml"
LANGUAGE_NAMES = {"es": "Spanish", "en": "English", "pt": "Portuguese", "fr": "French", "de": "German", "it": "Italian"}

ROLE_BRIEFS: Mapping[str, str] = {
    "analyst": "You are an analyst of one data domain. You read the data your tools return, compute what the modules offer and write one report for the team. You report findings and their limits. You do not recommend an order.",
    "researcher": "You argue one side of the case in front of the desk judge, using only the reports of the analysts. Answer the other side's last argument point by point. If the other side has not spoken, open with your strongest evidence and do not invent a rebuttal.",
    "judge": "You weigh both sides of the case and the evidence the modules computed, then give a verdict with the reasons that decided it. Name the claim that carried the most weight and the claim that was weakest.",
    "trader": "You turn the verdict into an order proposal by assembling the pieces the modules compute: size, stop and objective come from the sizing module and the rules of the desk, never from your own estimate. You run the pre-trade check before proposing anything.",
    "risk": "You voice one perspective in the risk committee (aggressive, conservative or neutral) and argue it with the numbers of the risk module. The neutral voice balances the others and sets the limits that the code applies live.",
    "investor": "You form a view on one symbol in the voice of your profile. You receive criteria already computed by the modules and you read them; you never compute or invent them. You answer with a direction, a conviction and reasons, nothing else.",
    "chief": "You coordinate the desk. You read the mandate and the rules, split the work into tasks of one responsibility each and check what comes back before trusting it. You never do the specialists' work yourself.",
}

LINE_BRIEFS: Mapping[str, str] = {
    "RF": "Line RF (fixed income). Horizon: months to years. A value near +1 means you expect the price of the instrument to rise (yields to fall) over the horizon. Near -1 means the opposite.",
    "DT": "Line DT (day trading). Horizon: within the session. A value near +1 means strong upward pressure within the session, near -1 strong downward pressure. Data is intraday; sentiment is only an alarm.",
    "SWC": "Line SWC (swing on CFDs). Horizon: days to weeks. A value near +1 means you expect a higher price over the horizon, near -1 a lower price. Data is daily bars plus news and macro.",
    "SWO": "Line SWO (swing on options, event driven and traditional). Horizon: days to weeks around the event. A value near +1 means you expect the underlying to move up after the event, near -1 down.",
    "LP": "Line LP (long term portfolio of stocks and ETFs). Horizon: months to years. A value near +1 means you expect the business to compound value, near -1 that its value will erode.",
}

ANCHORED_SCALE = (
    "Confidence is an integer from 0 to 100 on an anchored scale: 90-100 unmistakable evidence from several independent "
    "sources; 70-89 solid evidence with small gaps; 40-69 mixed or early evidence; 10-39 little or no edge; 0-9 no view."
)
SCOPE_DEFAULT_LACKS: Mapping[str, str] = {
    "RF": "tick data, order book and any macro series that was not fetched in this run",
    "DT": "fundamentals, filings and any data outside the session window",
    "SWC": "tick data, order book and filings",
    "SWO": "any option chain or event date that was not fetched in this run",
    "LP": "intraday data and order book",
}


def kit_dir(*parts: str, root: Path | None = None) -> Path:
    return (root or app_root()).joinpath(*DESK_KIT, *parts)


def owner_language(language: str | None = None) -> str:
    """Name of the language the owner reads. `language` may be a code (es, en) or a name; default is the app language."""
    chosen = (language or i18n.language() or "en").strip()
    return LANGUAGE_NAMES.get(chosen.lower(), chosen[:1].upper() + chosen[1:])


def language_instruction(language: str | None = None) -> str:
    """Final language rule: the answer is in the owner's language, the reasoning before it is free."""
    return (f"Write your entire final answer in {owner_language(language)}. "
            "You may reason in any language before the answer. Keep ticker symbols, field names and numbers unchanged.")


def _strip_title(text: str) -> str:
    lines = text.strip().splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return "\n".join(lines).strip()


def load_rule(key: str, *, root: Path | None = None) -> str:
    """Body of a common rule file (without its title). Raises FileNotFoundError when the file is missing."""
    path = kit_dir("rules", f"{key}.md", root=root)
    return _strip_title(path.read_text(encoding="utf-8"))


def _section(text: str, heading: str) -> str:
    match = re.search(rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)", text, re.S | re.M)
    if not match:
        raise ValueError(f"section {heading!r} not found")
    return match.group(1).strip()


def no_lookahead_clause(mode: str, as_of: datetime, *, root: Path | None = None) -> str:
    """Anti-lookahead clause: the backtest form, or in live mode the fixed present moment (AH-14)."""
    if mode not in ("backtest", "live"):
        raise ValueError("mode must be 'backtest' or 'live'")
    if as_of.tzinfo is None:
        raise ValueError("as_of must carry a timezone")
    stamp = as_of.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body = _section(load_rule("no-lookahead", root=root), "Backtest" if mode == "backtest" else "Live")
    return body.replace("[AS_OF]", stamp)


def scope_honesty_clause(lacks: str | Sequence[str], *, root: Path | None = None) -> str:
    """Honesty-of-scope clause plus the declaration of what this role does not have (AH-15)."""
    text = load_rule("scope-honesty", root=root)
    clause, declaration = _section(text, "Clause"), _section(text, "Declaration")
    items = [lacks] if isinstance(lacks, str) else list(lacks)
    items = [i.strip() for i in items if i and i.strip()]
    if not items:
        raise ValueError("a role must declare what it does not have")
    return f"{clause}\n\n{declaration.replace('[LACKS]', '; '.join(items) + '.')}"


def load_validations(*, root: Path | None = None) -> dict:
    data = yaml.safe_load(kit_dir("rules", VALIDATIONS_FILE, root=root).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("validations"), list) or not isinstance(data.get("collaboration"), list):
        raise ValueError("validations.yaml must hold 'validations' and 'collaboration' lists")
    return data


def validations_for(role: str, *, root: Path | None = None) -> list[str]:
    """Validation rules of one role, then the collaboration rules every role follows."""
    data = load_validations(root=root)
    own = [v["rule"] for v in data["validations"] if role in v.get("applies_to", [])]
    return own + [c["rule"] for c in data["collaboration"]]


@dataclass(frozen=True)
class Recipe:
    name: str
    lines: tuple[str, ...]
    summary: str
    body: str


def load_recipes(*, root: Path | None = None) -> dict[str, Recipe]:
    folder = kit_dir("recipes", root=root)
    found: dict[str, Recipe] = {}
    for path in sorted(folder.glob("*.md")):
        meta, body = split_frontmatter(path.read_text(encoding="utf-8"), str(path))
        lines = tuple(meta.get("lines") or ())
        bad = [x for x in lines if x not in LINES]
        if not meta.get("name") or not lines or bad:
            raise ValueError(f"recipe {path.name}: needs a name and lines from {LINES}")
        found[meta["name"]] = Recipe(meta["name"], lines, str(meta.get("summary", "")), body.strip())
    return found


@dataclass(frozen=True)
class Piece:
    """One block of instructions. `desk_id` None means common to every desk; otherwise it belongs to that desk only."""

    key: str
    text: str
    roles: frozenset[str] | None = None
    lines: frozenset[str] | None = None
    desk_id: int | None = None

    def applies(self, role: str, line: str, desk_id: int) -> bool:
        return ((self.desk_id is None or self.desk_id == desk_id)
                and (self.roles is None or role in self.roles)
                and (self.lines is None or line in self.lines))


class PromptComposer:
    """Builds the system prompt of an agent from pieces. Holds its own pieces: no state is shared between instances."""

    def __init__(self, *, root: Path | None = None) -> None:
        self._root = root
        self._pieces: dict[tuple[int | None, str], Piece] = {}

    def add_piece(self, piece: Piece) -> None:
        if not piece.key or not piece.text.strip():
            raise ValueError("a piece needs a key and a text")
        if piece.roles is not None and not piece.roles <= set(ROLES):
            raise ValueError(f"unknown role in piece {piece.key}")
        if piece.lines is not None and not piece.lines <= set(LINES):
            raise ValueError(f"unknown line in piece {piece.key}")
        self._pieces[(piece.desk_id, piece.key)] = piece

    def pieces_for(self, role: str, line: str, desk_id: int) -> list[Piece]:
        return [p for p in self._pieces.values() if p.applies(role, line, desk_id)]

    def compose(self, *, role: str, line: str, desk_id: int, as_of: datetime, mode: str = "live", language: str | None = None,
                lacks: str | Sequence[str] | None = None, recipe: str | None = None, persona: str = "") -> str:
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}")
        if line not in LINES:
            raise ValueError(f"unknown line {line!r}")
        blocks: list[str] = [f"## Role\n{ROLE_BRIEFS[role]}", f"## Line\n{LINE_BRIEFS[line]}"]
        if persona.strip():
            blocks.append(f"## Profile\n{persona.strip()}")
        if role in ("investor", "judge"):
            blocks.append(f"## Confidence scale\n{ANCHORED_SCALE}")
        for piece in self.pieces_for(role, line, desk_id):
            blocks.append(f"## Piece: {piece.key}\n{piece.text.strip()}")
        for key in COMMON_RULES:
            if key == "no-lookahead":
                body = no_lookahead_clause(mode, as_of, root=self._root)
            elif key == "scope-honesty":
                body = scope_honesty_clause(lacks or SCOPE_DEFAULT_LACKS[line], root=self._root)
            else:
                body = load_rule(key, root=self._root)
            blocks.append(f"## Rule: {key}\n{body}")
        checks = validations_for(role, root=self._root)
        blocks.append("## Rule: validations\n" + "\n".join(f"- {c}" for c in checks))
        if recipe:
            blocks.append(self._recipe_block(recipe, line))
        blocks.append(f"## Language\n{language_instruction(language)}")
        return "\n\n".join(blocks) + "\n"

    def _recipe_block(self, name: str, line: str) -> str:
        recipes = load_recipes(root=self._root)
        if name not in recipes:
            raise ValueError(f"unknown recipe {name!r}")
        item = recipes[name]
        if line not in item.lines:
            raise ValueError(f"recipe {name!r} is not for line {line}")
        return f"## Recipe: {item.name}\n{item.body}"


def prompt_integrity_problems(*, root: Path | None = None, composer: PromptComposer | None = None) -> list[str]:
    """Checks on the prompts themselves: every rule file exists and reaches every role, recipes reach their lines, and no
    role other than the execution roles holds sizing, exit or timing words. Returns readable problems; empty means sound."""
    problems: list[str] = []
    comp = composer or PromptComposer(root=root)
    as_of = datetime(2026, 1, 2, 15, 0, tzinfo=timezone.utc)
    for key in COMMON_RULES:
        if not kit_dir("rules", f"{key}.md", root=root).is_file():
            problems.append(f"rule file missing: {key}.md")
    if not kit_dir("rules", VALIDATIONS_FILE, root=root).is_file():
        problems.append(f"rule file missing: {VALIDATIONS_FILE}")
    if problems:
        return problems
    try:
        recipes = load_recipes(root=root)
    except (ValueError, OSError) as exc:
        return [f"recipes: {exc}"]
    for role in ROLES:
        for line in LINES:
            text = comp.compose(role=role, line=line, desk_id=0, as_of=as_of)
            for key in (*COMMON_RULES, "validations"):
                if f"## Rule: {key}\n" not in text:
                    problems.append(f"{role}/{line}: rule {key} not injected")
            if role not in EXECUTION_ROLES and (bad := contains_execution_terms(text)):
                problems.append(f"{role}/{line}: prompt holds a sizing, exit or timing phrase: {bad!r}")
    for recipe in recipes.values():
        for line in LINES:
            try:
                text = comp.compose(role="analyst", line=line, desk_id=0, as_of=as_of, recipe=recipe.name)
            except ValueError:
                text = None
            if (line in recipe.lines) != (text is not None and f"## Recipe: {recipe.name}\n" in text):
                problems.append(f"recipe {recipe.name}: wrong injection for line {line}")
    return problems

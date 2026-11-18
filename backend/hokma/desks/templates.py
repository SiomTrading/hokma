"""Desk templates: which agents join, in which role and with which default model, and a suggested weekly budget.

Data lives in code; the texts are i18n keys the dashboard resolves. The suggested budgets are starting points that
have to be compared with real consumption.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Member:
    agent_slug: str
    role: str
    model: str


@dataclass(frozen=True, slots=True)
class DeskTemplate:
    key: str
    members: tuple[Member, ...]
    token_budget: int | None


_MINIMAL = (Member("desk-chief", "chief", "sonnet"), Member("hugo", "data", "haiku"), Member("risk", "risk", "haiku"))
_STANDARD = _MINIMAL + (Member("iris", "research", "sonnet"), Member("signals", "signals", "sonnet"),
                        Member("axel", "execution", "haiku"), Member("vera", "reviewer", "sonnet"))
_DEBATE = _STANDARD + (Member("bull-case", "bull", "sonnet"), Member("bear-case", "bear", "sonnet"),
                       Member("judge", "judge", "sonnet"))

TEMPLATES: dict[str, DeskTemplate] = {
    "minimal": DeskTemplate("minimal", _MINIMAL, 150_000),
    "standard": DeskTemplate("standard", _STANDARD, 300_000),
    "debate": DeskTemplate("debate", _DEBATE, 600_000),
    "custom": DeskTemplate("custom", _MINIMAL[:1], None),       # the chief alone; the owner builds the rest
}
CHIEF_SLUG = "desk-chief"


def get(key: str) -> DeskTemplate | None:
    return TEMPLATES.get(key)


def public(template: DeskTemplate) -> dict[str, Any]:
    return {"key": template.key, "name": f"desks.template.{template.key}.name",
            "description": f"desks.template.{template.key}.description",
            "suggested_budget": template.token_budget,
            "agents": [{"agent_slug": m.agent_slug, "role": m.role, "model": m.model} for m in template.members]}


def listing() -> list[dict[str, Any]]:
    return [public(t) for t in TEMPLATES.values()]

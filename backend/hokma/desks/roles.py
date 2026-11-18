"""What each desk role can and cannot do. Data only: the strings are i18n keys the dashboard resolves (es and en)."""
from __future__ import annotations

from typing import Any

from hokma.db.repos.desks import ROLES

CUSTOM_CANNOT = "desks.role.custom.cannot"      # "Define what this agent cannot do"

# The only roles a team may hold more than once, because they are open-ended.
REPEATABLE = ("custom",)


def can_key(role: str) -> str:
    return f"desks.role.{role}.can"


def cannot_key(role: str) -> str:
    return f"desks.role.{role}.cannot"


def name_key(role: str) -> str:
    return f"desks.role.{role}.name"


def describe(role: str) -> dict[str, Any]:
    """Keys for one role. Unknown roles fall back to `custom`, whose "cannot" asks the owner to define it."""
    chosen = role if role in ROLES else "custom"
    return {"role": chosen, "name": name_key(chosen), "can": can_key(chosen),
            "cannot": CUSTOM_CANNOT if chosen == "custom" else cannot_key(chosen)}


def all_roles() -> list[dict[str, Any]]:
    return [describe(r) for r in ROLES]

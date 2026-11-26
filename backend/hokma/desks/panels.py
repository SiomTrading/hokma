"""The panels of a desk screen (SPEC-025, delivery 3): which ones it shows and in which order.

The catalog is one list here and one list in `dashboard/src/screens/desks/panels.ts`; every delivery that adds a panel adds it to both.
`rules`, `agents` and `config` are fixed: they can move but never leave. The rest can be added and removed, by hand or by the chief
(the chief may do it without asking: a panel moves no money and no rule). `desks.layout` holds a JSON text `{"panels": [...]}`;
NULL means DEFAULT, the five panels of every desk before this delivery.
"""
from __future__ import annotations

import json
from typing import Any

from hokma.desks.errors import DeskError

CATALOG = ("terminal", "agents", "operations", "rules", "config", "company", "calendar", "analysis", "strategies", "option_detail")
FIXED = ("rules", "agents", "config")
DEFAULT = ("terminal", "agents", "operations", "rules", "config")


def resolve(raw: str | None) -> list[str]:
    """The panels to show, in order. Unknown names and repeats are dropped; a fixed panel that is missing goes back at the end, so a
    damaged value never leaves a desk without its rules, agents or config."""
    if not raw:
        return list(DEFAULT)
    try:
        value = json.loads(raw)
    except ValueError:
        return list(DEFAULT)
    items = value.get("panels") if isinstance(value, dict) else None
    if not isinstance(items, list):
        return list(DEFAULT)
    out: list[str] = []
    for item in items:
        if isinstance(item, str) and item in CATALOG and item not in out:
            out.append(item)
    out.extend(p for p in FIXED if p not in out)
    return out


def saved(raw: str | None) -> dict[str, dict[str, Any]]:
    """The strategies the owner saved from the Option detail panel (SPEC-028, 3339-B): strategy_key -> legs, kept next to the panels in the same JSON."""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    items = value.get("strategies") if isinstance(value, dict) else None
    if not isinstance(items, dict):
        return {}
    return {k: v for k, v in items.items() if isinstance(k, str) and isinstance(v, dict)}


def dump(panels: list[str], strategies: dict[str, dict[str, Any]] | None = None) -> str:
    body: dict[str, Any] = {"panels": panels}
    if strategies:
        body["strategies"] = strategies
    return json.dumps(body, separators=(",", ":"))


def check_order(panels: Any) -> list[str]:
    """A whole order sent by the screen: a list of catalog names, no repeats, with the three fixed panels in it."""
    if not isinstance(panels, list) or not all(isinstance(p, str) for p in panels):
        raise DeskError("bad_panels", 400)
    if len(set(panels)) != len(panels) or any(p not in CATALOG for p in panels):
        raise DeskError("bad_panels", 400)
    if any(p not in panels for p in FIXED):
        raise DeskError("panel_fixed", 409)
    return list(panels)


def with_added(current: list[str], panel: str) -> list[str]:
    """`panel` enters before the first fixed panel of the order (a panel that is already there stays where it is)."""
    if panel not in CATALOG:
        raise DeskError("bad_panels", 400)
    if panel in current:
        return list(current)
    at = next((i for i, p in enumerate(current) if p in FIXED), len(current))
    return [*current[:at], panel, *current[at:]]


def with_removed(current: list[str], panel: str) -> list[str]:
    if panel not in CATALOG:
        raise DeskError("bad_panels", 400)
    if panel in FIXED:
        raise DeskError("panel_fixed", 409)
    return [p for p in current if p != panel]

"""Stable markdown for a record of a desk (SPEC-018: TA-38). The headers are fixed, in a fixed order; a field that is absent is printed with its
name and the words `not provided`, so a reader can tell an absent value from one nobody asked for; two renders of the same record are the same
bytes (no clock, no random, mappings sorted). Text that comes from an agent has its angle brackets and ampersands escaped, so a report can
never carry markup."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from html import escape
from typing import Any

from hokma.exec.money import text as money_text

ABSENT = "not provided"
MAX_TEXT = 20_000
MAX_ITEMS = 200


@dataclass(frozen=True)
class Field:
    key: str
    label: str


PROPOSAL_FIELDS = (Field("action", "Action"), Field("reasoning", "Reasoning"), Field("entry_price", "Entry price"), Field("stop_loss", "Stop loss"),
                   Field("position_sizing", "Position sizing"))
DECISION_FIELDS = (Field("rating", "Rating"), Field("summary", "Executive summary"), Field("thesis", "Investment thesis"),
                   Field("price_target", "Price target"), Field("time_horizon", "Time horizon"))
CYCLE_FIELDS = (Field("desk_id", "Desk"), Field("kind", "Kind"), Field("status", "Status"), Field("entries", "Entries"), Field("outputs", "Outputs"),
                Field("decisions", "Decisions"), Field("order_ids", "Order ids"), Field("timings", "Timings"))


def _plain(value: Any, depth: int = 0) -> str:
    if depth > 6:
        return ABSENT
    if isinstance(value, Decimal):
        return money_text(value) or ABSENT
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float, str)):
        return escape(str(value)[:MAX_TEXT], quote=False)
    if isinstance(value, Mapping):
        return "; ".join(f"{escape(str(k), quote=False)}: {_plain(v, depth + 1)}" for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))[:MAX_ITEMS])
    if isinstance(value, (list, tuple)):
        return ", ".join(_plain(v, depth + 1) for v in value[:MAX_ITEMS])
    return escape(str(value)[:MAX_TEXT], quote=False)


def _missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or (isinstance(value, (list, tuple, Mapping)) and len(value) == 0)


def render_record(record: Mapping[str, Any], fields: Sequence[Field], *, title: str | None = None) -> str:
    """One `**Label**: value` block per field, in the order of `fields`. Every field is named, present or not."""
    parts: list[str] = [f"# {escape(title, quote=False)}", ""] if title else []
    for index, f in enumerate(fields):
        value = record.get(f.key)
        if index:
            parts.append("")
        parts.append(f"**{f.label}**: {ABSENT if _missing(value) else _plain(value)}")
    return "\n".join(parts) + "\n"


def render_proposal(record: Mapping[str, Any]) -> str:
    return render_record(record, PROPOSAL_FIELDS)


def render_decision(record: Mapping[str, Any]) -> str:
    return render_record(record, DECISION_FIELDS)


def render_cycle(record: Mapping[str, Any]) -> str:
    return render_record(record, CYCLE_FIELDS, title="Cycle record")


def section(header: str, body: str | None, *, level: int = 2) -> str:
    """A fixed header and its text; a stage with no text says so under its own header."""
    mark = "#" * max(1, min(level, 6))
    return f"{mark} {escape(header, quote=False)}\n\n{ABSENT if _missing(body) else escape(str(body).strip()[:MAX_TEXT], quote=False)}\n"

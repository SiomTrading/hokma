"""Privileges of the back office clerk by role (SPEC-018: AFS-003). The clerk is one service agent that works in four roles, each with its own tools:

* `orchestrator`: splits the work and checks what comes back. Control tools only (tasks, the journal). It never writes data.
* `statement_reader`: opens statements that come from a third party. It has no write tool and no access to the data of Hokma, so a statement that
  carries hostile text has nothing to act with. Its output is data for the others, never an order.
* `critic`: read only. It looks again at each break against the book (the book tools) and says confirmed, stale or unverifiable.
* `formatter`: the only role that writes: the reconciliation record, the report and the exports. A person signs the report.

A tool that is not in the set of a role fails with `RoleViolation`, whatever the role asks. The names are those of the Hokma tools plus
`statement_read`, the operation of `statements.py` that the server offers to the reader role.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

THIRD_PARTY = "third_party_read"
OWN_READ = "own_read"
WRITE = "write"
CONTROL = "control"


class RoleViolation(PermissionError):
    """A role asked for a tool that is not in its set."""

    code = "refused"


@dataclass(frozen=True)
class ToolInfo:
    name: str
    access: str
    note: str = ""


TOOLS: dict[str, ToolInfo] = {t.name: t for t in (
    ToolInfo("statement_read", THIRD_PARTY, "text and tables of a statement given as bytes: no data of Hokma comes in or goes out"),
    ToolInfo("book_positions", OWN_READ), ToolInfo("book_snapshot", OWN_READ), ToolInfo("book_pnl", OWN_READ), ToolInfo("recon_breaks", OWN_READ),
    ToolInfo("recon_run", WRITE, "records the reconciliation and its breaks"),
    ToolInfo("report_account", WRITE, "records the account report"), ToolInfo("report_weekly", WRITE, "records the weekly digest"),
    ToolInfo("export_book", WRITE, "creates a spreadsheet file"),
    ToolInfo("task_show", CONTROL), ToolInfo("task_list", CONTROL), ToolInfo("task_claim", CONTROL), ToolInfo("task_heartbeat", CONTROL),
    ToolInfo("task_complete", CONTROL), ToolInfo("task_block", CONTROL), ToolInfo("task_comment", CONTROL), ToolInfo("journal_write", CONTROL),
)}

ROLE_TOOLS: dict[str, frozenset[str]] = {
    "orchestrator": frozenset(n for n, t in TOOLS.items() if t.access == CONTROL),
    "statement_reader": frozenset({"statement_read"}),
    "critic": frozenset({"book_positions", "book_snapshot", "book_pnl", "recon_breaks"}),
    "formatter": frozenset({"recon_breaks", "recon_run", "report_account", "report_weekly", "export_book", "book_positions", "book_snapshot", "book_pnl"}),
}
ROLES = tuple(ROLE_TOOLS)


def tools_for(role: str) -> frozenset[str]:
    if role not in ROLE_TOOLS:
        raise RoleViolation(f"unknown role {role!r}")
    return ROLE_TOOLS[role]


def check(role: str, tool: str) -> ToolInfo:
    """The tool if the role may use it; otherwise `RoleViolation`."""
    if tool not in tools_for(role):
        raise RoleViolation(f"the {role} role has no tool {tool!r}")
    return TOOLS[tool]


def call(role: str, tool: str, handlers: dict[str, Callable[..., Any]], *args: Any, **kwargs: Any) -> Any:
    """Run a tool on behalf of a role: the role is checked before anything runs."""
    check(role, tool)
    if tool not in handlers:
        raise RoleViolation(f"no handler for {tool!r}")
    return handlers[tool](*args, **kwargs)


def self_check() -> list[str]:
    """Problems of the table itself (empty means sound): the reader and the critic have no write tool, the reader has no own data, only the formatter writes."""
    problems = []
    for role, names in ROLE_TOOLS.items():
        unknown = names - set(TOOLS)
        if unknown:
            problems.append(f"{role}: unknown tool {sorted(unknown)[0]}")
        writes = {n for n in names if TOOLS[n].access == WRITE}
        if writes and role != "formatter":
            problems.append(f"{role}: only the formatter may hold a write tool")
    if any(TOOLS[n].access != THIRD_PARTY for n in ROLE_TOOLS["statement_reader"]):
        problems.append("statement_reader: it may hold third party reading tools only")
    if any(TOOLS[n].access not in (OWN_READ,) for n in ROLE_TOOLS["critic"]):
        problems.append("critic: it may hold read tools of the book only")
    if "formatter" not in ROLE_TOOLS:
        problems.append("there is no formatter")
    return problems


__all__ = ["ROLES", "ROLE_TOOLS", "RoleViolation", "TOOLS", "call", "check", "self_check", "tools_for"]

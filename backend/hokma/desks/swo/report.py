"""The report of a cycle by seat and the report of the whole run, as the Analysis panel reads them (SPEC-028, delivery 4, piece A).

A cycle keeps in `run_records.outputs.agents` one entry per seat that took part, written by the nodes of `graph.py`:

    {seat, name, role, step, status: ok | absent | failed, summary, report, why, tools[], signal?}

`ok` is a report that arrived. `absent` is a report that was never going to arrive (the plan left the analyst out, its data did not come).
`failed` is a report that was expected and did not arrive or did not validate. An absent or failed seat is shown as an absence with its
cause, never as an empty finding. The text a seat wrote is cut to a fixed size; nothing here calls a model, a broker or the network.

`view` builds the report of a run from its record: the steps with their state, the grade and the reasons of the manager, the proposal and what
became of it at the door of execution, and the seats.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from hokma.desks.plantel import SEATS

# The eight steps the panel shows, in order. `learn` and `close` are not steps of the owner: they come after the order is out.
STEPS = ("plan", "analysts", "company_filter", "debate", "judge", "trader", "committee", "manager")
OK, ABSENT, FAILED = "ok", "absent", "failed"
STATUSES = (OK, ABSENT, FAILED)
MAX_SUMMARY = 600
MAX_REPORT = 6000
MAX_WHY = 1200
MAX_TOOLS = 20
MAX_BULLET = 500


def _clip(value: Any, limit: int) -> str:
    text = "" if value is None else str(value).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


def step_of(node: str) -> int | None:
    """The number (1 to 8) of a node among the steps of the panel; None for the steps after the order left."""
    return STEPS.index(node) + 1 if node in STEPS else None


def seat_entry(code: str, *, step: str, status: str, summary: str = "", report: str = "", why: str = "", tools: Sequence[str] = (),
               signal: Mapping[str, Any] | None = None, role: str | None = None) -> dict[str, Any]:
    """One seat of one step. `role` is the nearest role of the seat unless the step gives it another one (the moderator is the judge's seat)."""
    seat = SEATS[code]
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    entry: dict[str, Any] = {"seat": code, "name": seat.name, "role": role or seat.desk_role, "step": step, "status": status,
                             "summary": _clip(summary, MAX_SUMMARY), "report": _clip(report, MAX_REPORT), "why": _clip(why, MAX_WHY),
                             "tools": [str(t) for t in list(dict.fromkeys(tools))[:MAX_TOOLS]]}
    if signal is not None:
        entry["signal"] = dict(signal)
    return entry


def _step_states(out: Mapping[str, Any], status: str, live_trail: Sequence[str] | None) -> list[dict[str, Any]]:
    """Each step as `done`, `stopped` (the cycle ended there), `running`, `pending` (a live run that has not reached it) or `skipped` (a
    finished run that never reached it)."""
    trail = list(out.get("trail") or live_trail or [])
    stopped_at = out.get("stopped_at")
    live = status == "running"
    states: list[dict[str, Any]] = []
    for index, node in enumerate(STEPS, start=1):
        if node == stopped_at:
            state = "stopped"
        elif node in trail:
            state = "running" if live and node == trail[-1] else "done"
        else:
            state = "pending" if live else "skipped"
        states.append({"node": node, "step": index, "state": state})
    return states


def _bullets(out: Mapping[str, Any], seats: Sequence[Mapping[str, Any]]) -> list[str]:
    """The reasons of the manager in bullets: his own summary and thesis, what the judge, the committee and the filter said, and every
    report that did not arrive with its cause."""
    final = out.get("final") or {}
    judge = out.get("judge") or {}
    committee = out.get("committee") or {}
    items: list[str] = [final.get("summary"), final.get("thesis"), out.get("detail")]
    for seat in seats:
        if seat["step"] == "judge" and seat.get("report"):
            items.append(seat["report"])
    items.append(committee.get("summary"))
    items += list(final.get("would_change") or [])
    items += [f"{s['name']}: {s['why']}" for s in seats if s["step"] == "analysts" and s["status"] != OK and s.get("why")]
    seen: list[str] = []
    for item in items:
        text = _clip(item, MAX_BULLET)
        if text and text not in seen:
            seen.append(text)
    return seen


def view(record: Mapping[str, Any], *, live_trail: Sequence[str] | None = None, decision: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The report of one run from its record (`runs.get`). A run that is still going has no outputs yet: its steps come from `live_trail`.
    `decision` is the proposal row of the door of execution, when the cycle produced one."""
    body = record.get("record") or {}
    out = body.get("outputs") or {}
    entries = body.get("entries") or {}
    seats = [dict(s) for s in out.get("agents") or []]
    final = out.get("final") or {}
    trade = out.get("trade") or {}
    proposal = None
    if trade.get("structure"):
        proposal = {k: trade.get(k) for k in ("structure", "underlying", "expiration", "contracts", "net_premium", "max_loss_usd")}
    status = record["status"]
    return {"run_id": record["id"], "desk_id": record["desk_id"], "status": status, "symbol": entries.get("symbol"), "as_of": entries.get("as_of"),
            "started_at": record.get("started_at"), "ended_at": record.get("ended_at"),
            "steps": _step_states(out, status, live_trail), "plan": out.get("plan"),
            "rating": final.get("rating"), "grade": final.get("grade"), "summary": final.get("summary"), "why": _bullets(out, seats),
            "proposal": proposal, "decision": None if decision is None else dict(decision),
            "stopped_at": out.get("stopped_at"), "reason": out.get("reason"), "detail": out.get("detail"), "error": out.get("error"),
            "absent": out.get("absent") or [], "agents": seats, "level": final.get("level"),
            "exec_status": None if decision is None else decision.get("status")}


def brief(record: Mapping[str, Any]) -> dict[str, Any]:
    """One line of the list of analyses (`Anteriores`): what it was, when, how it ended."""
    body = record.get("record") or {}
    out = body.get("outputs") or {}
    entries = body.get("entries") or {}
    final = out.get("final") or {}
    return {"run_id": record["id"], "status": record["status"], "symbol": entries.get("symbol"), "as_of": entries.get("as_of"),
            "started_at": record.get("started_at"), "ended_at": record.get("ended_at"), "rating": final.get("rating"), "grade": final.get("grade"),
            "stopped_at": out.get("stopped_at"), "reason": out.get("reason"), "has_proposal": bool(out.get("orders")), "absent": out.get("absent") or []}

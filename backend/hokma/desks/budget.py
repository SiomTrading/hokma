"""Token budget of a desk (SPEC-014 P3): what the desk used this week, which state that puts it in, and the one
warning per week and level.

The week is the one `UsageRepo.week()` defines, the same one the home screen uses. Nothing is stored about the state:
`ok`, `warn` and `exhausted` are computed on every call, so when the week changes the used amount goes back to zero
and the desk starts again by itself. Only the last warning written (week and level) is kept, on the desk row, so the
journal gets each warning once.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from hokma.db import Actor, Database, NotFound, get_database
from hokma.desks.errors import DeskError
from hokma.i18n import t

WARN_PCT = 80
EXHAUSTED_PCT = 100
SYSTEM = Actor.system("desks")


def state_for(used: int, budget: int | None) -> str:
    """`ok` below 80% (and always without a budget), `warn` from 80%, `exhausted` from 100%."""
    if not budget:
        return "ok"
    if used * 100 >= budget * EXHAUSTED_PCT:
        return "exhausted"
    if used * 100 >= budget * WARN_PCT:
        return "warn"
    return "ok"


def level_for(state: str) -> int | None:
    return {"warn": WARN_PCT, "exhausted": EXHAUSTED_PCT}.get(state)


def _usage(u, desk, now: datetime | None) -> dict[str, Any]:
    week = u.usage.week(now=now)
    since, until = week.starts_at, week.ends_at
    total = u.desks.usage_total(desk.project_id, since=since, until=until)
    budget = desk.token_budget
    return {
        "desk_id": desk.id, "starts_at": since, "ends_at": until,
        "token_budget": budget, "used": total.tokens, "tokens_in": total.tokens_in, "tokens_out": total.tokens_out,
        "pct": round(total.tokens * 100.0 / budget, 1) if budget else None,
        "state": state_for(total.tokens, budget),
        "by_agent": [{"key": r.key, "tokens_in": r.tokens_in, "tokens_out": r.tokens_out}
                     for r in u.desks.usage_by_agent(desk.project_id, since=since, until=until)],
        "by_day": [{"key": r.key, "tokens_in": r.tokens_in, "tokens_out": r.tokens_out}
                   for r in u.desks.usage_by_day(desk.project_id, since=since, until=until)],
    }


def figures(u, desk, now: datetime | None = None) -> dict[str, Any]:
    """The same figures as `usage`, for a caller that already holds an open unit of work and the desk row."""
    return _usage(u, desk, now)


def usage(desk_id: int, *, db: Database | None = None, now: datetime | None = None) -> dict[str, Any]:
    """The week of one desk: used, budget, percent, state and the split by agent and by day."""
    with (db or get_database()).read() as u:
        try:
            desk = u.desks.get(desk_id)
        except NotFound as exc:
            raise DeskError("not_found", 404) from exc
        return _usage(u, desk, now)


def state(desk_id: int, *, db: Database | None = None, now: datetime | None = None) -> str:
    """Only the state (`ok`, `warn`, `exhausted`) of one desk this week."""
    return usage(desk_id, db=db, now=now)["state"]


def check_alert(desk_id: int, *, db: Database | None = None, now: datetime | None = None) -> int | None:
    """Write the journal warning when the desk crossed 80% or 100% and that level was not written this week.

    Returns the level written (80 or 100) or None. A warning is written once per week and level; reaching 100 after
    80 writes the 100 one; staying at 80 writes nothing again; a new week starts clean."""
    database = db or get_database()
    with database.write() as u:
        try:
            desk = u.desks.get(desk_id)
        except NotFound as exc:
            raise DeskError("not_found", 404) from exc
        info = _usage(u, desk, now)
        level = level_for(info["state"])
        if level is None:
            return None
        if desk.budget_alert_week == info["starts_at"] and (desk.budget_alert_level or 0) >= level:
            return None
        figures = {"name": desk.name, "pct": level, "used": info["used"], "budget": info["token_budget"]}
        title = (t("desks.budget.exhausted", **figures) if level == EXHAUSTED_PCT
                 else t("desks.budget.warn", **figures))
        u.journal.add("alert", title[:200], actor=SYSTEM, outcome="info", project_id=desk.project_id,
                      refs={"desk": desk.id, "level": level, "week": info["starts_at"], "used": info["used"],
                            "budget": info["token_budget"]})
        u.desks.set_alert(desk.id, info["starts_at"], level)
        return level

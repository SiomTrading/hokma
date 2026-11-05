"""Consumption: tokens the engine reported (chat messages and scheduled runs) in the current week against the cap
the owner set. The week is the one `usage.week()` defines (Monday to Monday in the profile's time zone), the same one
the home screen uses, so every screen shows the same number. Nothing here is estimated: the numbers are sums of what
the stream-json `result` lines carried."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from hokma.agents.service import AgentsError
from hokma.db import Database, DbError, get_database

MAX_CAP = 10**13


def weekly(db: Database | None = None, now: datetime | None = None) -> dict[str, Any]:
    db = db or get_database()
    with db.read() as u:
        week = u.usage.week(now=now)
        since, until = week.starts_at, week.ends_at
        total = u.usage.total(since=since, until=until)
        by_agent = u.usage.by_agent(since=since, until=until)
        by_model = u.usage.by_model(since=since, until=until)
        by_day = u.usage.by_day(since=since, until=until)
        profile = u.profile.get_or_default()
    cap = profile.weekly_token_cap
    return {
        "starts_at": since, "ends_at": until,
        "used": total.tokens, "tokens_in": total.tokens_in, "tokens_out": total.tokens_out,
        "messages": total.messages, "runs": total.runs,
        "cap": cap, "pct": round(total.tokens * 100.0 / cap, 1) if cap else None,
        "by_agent": [{"key": r.key, "tokens_in": r.tokens_in, "tokens_out": r.tokens_out} for r in by_agent],
        "by_model": [{"key": r.key, "tokens_in": r.tokens_in, "tokens_out": r.tokens_out} for r in by_model],
        "by_day": [{"key": r.key, "tokens_in": r.tokens_in, "tokens_out": r.tokens_out} for r in by_day],
        "claude": {"status": profile.claude_status, "plan": profile.claude_plan,
                   "checked_at": profile.claude_checked_at},
        "profile_exists": profile.exists,
    }


def set_cap(cap: int | None, db: Database | None = None) -> dict[str, Any]:
    db = db or get_database()
    if cap is not None and (isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= MAX_CAP):
        raise AgentsError("cap_invalid", 422)
    try:
        with db.write() as u:
            if u.profile.get() is None:
                raise AgentsError("profile_missing", 409)
            u.profile.save(weekly_token_cap=cap)
    except DbError as exc:
        raise AgentsError("cap_invalid", 422, str(exc)) from exc
    return weekly(db)

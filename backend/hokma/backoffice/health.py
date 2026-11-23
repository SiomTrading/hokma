"""Health of the routines of the desks (SPEC-018: OA-34).

What was taken from the piece of the origin (read whole before writing this): the health of a scheduled item is a live projection derived
from the authoritative stores (the schedule row and the latest run), never a field that anybody can write; the states are ordered so that a
run in progress is allowed to finish, a routine that cannot be dispatched is `blocked` and a past failure stays visible until a later
success; `inactive` means nothing is expected. Here the projection reads `scheduled_jobs` and the task of its latest run.

A routine of a desk is a scheduled job whose name starts with `desk-<id>` (`desk-3-premarket`). One that is ON and let two runs pass without
finishing raises one notice for the owner through the Telegram outbox (the notifier the orders already use). One that is OFF says nothing. The
same two missed runs never raise a second notice."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from hokma.db import Actor, Database, from_db_time, get_database
from hokma.scheduler import schedule

SYSTEM = Actor.system("backoffice")
MISSED_LIMIT = 2
GRACE = timedelta(seconds=120)
MAX_LOOKAHEAD = 200
JOB_NAME = re.compile(r"^desk-(\d+)(?:[-: _]|$)")
Notifier = Callable[[Any, str], bool]


@dataclass(frozen=True)
class RoutineHealth:
    job_id: int
    name: str
    desk_id: int
    state: str                  # inactive | blocked | running | failed | missed | due | healthy | not_started
    missed: int
    last_run_at: str | None
    message: str

    @property
    def needs_notice(self) -> bool:
        return self.state == "missed"


def desk_of(job_name: str) -> int | None:
    match = JOB_NAME.match(job_name or "")
    return int(match.group(1)) if match else None


def _expected(job: Any, since: datetime, until: datetime) -> int:
    """How many runs the schedule says should have started in (since, until]."""
    if job.schedule_kind == "once":
        return 0
    count, cursor = 0, since
    for _ in range(MAX_LOOKAHEAD):
        following = schedule.next_run(job.schedule_kind, job.schedule_expr, job.timezone, cursor)
        if following is None or following > until:
            break
        count += 1
        cursor = following
    return count


def project(job: Any, now: datetime, *, last_task_status: str | None = None) -> RoutineHealth | None:
    """The health of one routine of a desk, or None when the job is not a routine of a desk."""
    desk = desk_of(job.name)
    if desk is None:
        return None
    base = (job.name, desk, job.last_run_at)

    def made(state: str, missed: int, message: str) -> RoutineHealth:
        return RoutineHealth(job.id, base[0], desk, state, missed, base[2], message)

    if not job.is_active:
        return made("inactive", 0, "The routine is off: no run is expected.")
    if job.approval_id is None:
        return made("blocked", 0, "The routine is on but the owner never approved it, so it cannot run.")
    if last_task_status == "running":
        return made("running", 0, "A run is in progress.")
    reference = from_db_time(job.last_run_at) if job.last_run_at else from_db_time(job.created_at)
    missed = _expected(job, reference, now - GRACE)
    if missed >= MISSED_LIMIT:
        return made("missed", missed, f"{missed} runs were due and none finished since {reference.strftime('%Y-%m-%d %H:%M')} UTC.")
    if last_task_status == "failed":
        return made("failed", missed, "The latest run failed.")
    if job.next_run_at and from_db_time(job.next_run_at) <= now:
        return made("due", missed, "The routine is due and waits to be dispatched.")
    return made("healthy" if job.last_run_at else "not_started", missed, "The routine is on schedule.")


def queue_notice(u: Any, text: str) -> bool:
    """The existing notifier: the Telegram outbox, when the owner linked Telegram. False when there is no link."""
    from hokma.telegram.glue import enqueue

    if u.session.one("SELECT 1 AS x FROM telegram_link WHERE id = 1") is None:
        return False
    enqueue(u, text)
    return True


def check(*, db: Database | None = None, now: datetime | None = None, notifier: Notifier = queue_notice) -> list[RoutineHealth]:
    """The health of every routine of a desk. Each routine that is on and missed two runs raises one notice (once per stretch of missed
    runs: the key is the routine and the moment of its last run). Returns the projection of every routine."""
    database = db or get_database()
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    with database.read() as u:
        jobs = u.scheduled_jobs.list()
        statuses = {j.id: (u.session.scalar("SELECT status FROM tasks WHERE id = :i", {"i": j.last_task_id}) if j.last_task_id else None) for j in jobs}
        desks = {r["id"] for r in u.session.query("SELECT id FROM desks")}
    out: list[RoutineHealth] = []
    for job in jobs:
        health = project(job, moment, last_task_status=statuses[job.id])
        if health is None or health.desk_id not in desks:
            continue
        out.append(health)
        if health.needs_notice:
            _notice_once(database, job, health, notifier)
    return out


def _notice_once(database: Database, job: Any, health: RoutineHealth, notifier: Notifier) -> bool:
    key = f"health:{job.id}:{job.last_run_at or job.created_at}"
    with database.write() as u:
        if u.journal.count(kind="alert", search=key) > 0:
            return False
        if not notifier(u, f"Desk {health.desk_id}: the routine '{job.name}' is on and {health.missed} runs did not finish. {health.message}"):
            return False                                    # no channel yet: nothing is marked as told, the next check tries again
        u.journal.add("alert", f"Routine {job.name} missed {health.missed} runs", actor=SYSTEM, outcome="failure", body=f"{key}\n{health.message}",
                      refs={"scheduled_job": job.id, "desk_id": health.desk_id, "key": key})
    return True


__all__ = ["RoutineHealth", "check", "desk_of", "project", "queue_notice"]

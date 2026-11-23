"""Issues of the back office as tasks (SPEC-018: OA-35). A material reconciliation break or a failed run becomes a row of `tasks` with an
owner (the agent that must look at it) and a due date. The same break, or the same failed run, never creates a second open task: the task
carries the key of the issue and an open task with that key is returned instead of a new one.

What the board of the origin taught (read whole): the issue is a record separate from the live state of the thing it is about, a key makes the
write idempotent, and a failed run stays visible until a later success closes it. Here the key is derived from the break or the run, never
from what an agent says about it."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta, timezone, datetime
from decimal import Decimal
from typing import Any

from hokma.db import Actor, Database, get_database
from hokma.desks.core.safety import scrub_text
from hokma.db.repos.conversations import like
from hokma.exec.money import dec

SYSTEM = Actor.system("backoffice")
OPEN_STATES = ("triage", "todo", "ready", "running", "blocked", "review", "awaiting_approval")
MATERIAL_AMOUNT = Decimal("1.00")
DEFAULT_DUE_DAYS = 2
KEY_LINE = "issue-key: "


class IssueError(ValueError):
    """An issue cannot be raised as asked."""


@dataclass(frozen=True)
class IssueResult:
    task_id: int
    created: bool
    key: str


def is_material(bucket: str, diff: Any = None, *, threshold: Decimal = MATERIAL_AMOUNT) -> bool:
    """A break that must be looked at: a quantity that differs, or one side that has what the other lacks, or an amount of `threshold` or more.
    A timing difference is not material: it closes itself when the other side catches up."""
    if bucket in ("quantity", "one_side"):
        return True
    if bucket == "amount":
        return diff is not None and abs(dec(diff, "diff")) >= threshold
    return False


def _due(due: date | None, now: datetime | None) -> date:
    return due or ((now or datetime.now(timezone.utc)).date() + timedelta(days=DEFAULT_DUE_DAYS))


def open_issue(*, key: str, title: str, detail: str, owner: str, due: date | None = None, priority: int = 1, now: datetime | None = None,
               db: Database | None = None) -> IssueResult:
    """One task for the issue `key`. If an open task already has that key, it is returned and nothing is written."""
    if not isinstance(key, str) or not 3 <= len(key) <= 160 or "\n" in key:
        raise IssueError("the key of an issue is 3 to 160 characters on one line")
    due_day = _due(due, now)
    body = f"{KEY_LINE}{key}\ndue: {due_day.isoformat()}\nowner: {owner}\n\n{scrub_text(detail[:6000])}"
    with (db or get_database()).write() as u:
        if u.agents.find(owner) is None:
            raise IssueError(f"owner {owner!r} is not an agent: an issue needs a known owner")
        marks = ", ".join(f":s{i}" for i in range(len(OPEN_STATES)))
        candidates = u.session.query(f"SELECT id, body FROM tasks WHERE body LIKE :k ESCAPE '\\' AND status IN ({marks}) ORDER BY id",
                                     {"k": like(f"{KEY_LINE}{key}"), **{f"s{i}": s for i, s in enumerate(OPEN_STATES)}})
        for found in candidates:                      # the key is the whole first line: a key that only starts like another one does not match
            if (found["body"] or "").split("\n", 1)[0] == f"{KEY_LINE}{key}":
                return IssueResult(found["id"], False, key)
        task = u.tasks.create(scrub_text(title[:200]), actor=SYSTEM, body=body, status="ready", priority=priority, assigned_agent=owner)
        return IssueResult(task.id, True, key)


def for_break(break_id: int, *, owner: str, due: date | None = None, threshold: Decimal = MATERIAL_AMOUNT, now: datetime | None = None,
              db: Database | None = None) -> IssueResult | None:
    """The task of a reconciliation break, if the break is material. The break row is linked to the task (`recon_breaks.task_id`), so a
    second call, or the same break found again, finds it. The key is account, bucket and the key of the break."""
    database = db or get_database()
    with database.read() as u:
        row = u.session.one("SELECT b.*, r.account_id AS account_id FROM recon_breaks b JOIN reconciliations r ON r.id = b.recon_id WHERE b.id = :i",
                            {"i": break_id})
    if row is None:
        raise IssueError(f"break {break_id} does not exist")
    if row["action"] == "suppress" or not is_material(row["bucket"], row["diff"], threshold=threshold):
        return None
    key = f"recon-break:{row['account_id']}:{row['bucket']}:{row['key']}"
    result = open_issue(key=key, title=f"Reconciliation break {row['bucket']} on {row['key']}"[:200],
                        detail=f"bucket: {row['bucket']}\nkey: {row['key']}\ndiff: {row['diff']}\ncause: {row['cause']}\nexplanation: {row['explanation']}",
                        owner=owner, due=due, priority=2, now=now, db=database)
    with database.write() as u:
        u.session.update("recon_breaks", {"id": break_id}, {"task_id": result.task_id, "owner": owner, "due_at": f"{_due(due, now).isoformat()} 00:00:00"})
    return result


def for_failed_run(run_id: int, *, owner: str, due: date | None = None, now: datetime | None = None, db: Database | None = None) -> IssueResult | None:
    """The task of a run that failed. None for a run that did not fail. The key is the run, so it is raised once."""
    database = db or get_database()
    with database.read() as u:
        row = u.session.one("SELECT id, desk_id, kind, status, record_json FROM run_records WHERE id = :i", {"i": run_id})
    if row is None:
        raise IssueError(f"run {run_id} does not exist")
    if row["status"] != "failed":
        return None
    return open_issue(key=f"run-failed:{run_id}", title=f"Run {run_id} of kind {row['kind']} failed"[:200],
                      detail=f"desk: {row['desk_id']}\nrun: {run_id}\nkind: {row['kind']}\nrecord: {row['record_json'][:3000]}", owner=owner, due=due,
                      priority=2, now=now, db=database)


__all__ = ["IssueError", "IssueResult", "for_break", "for_failed_run", "is_material", "open_issue"]

"""The lesson log (SPEC-017: TA-44). Append-only: there is no delete anywhere in this package.

`propose` writes a lesson only when its order really executed. It is idempotent: the same text for the same order of the same
desk is the same lesson, not a second row. `promote` moves a settled lesson to the general memory and needs the Hokma manager
agent or the owner. Retiring a lesson changes its status; the row stays.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from hokma.db import Database, get_database
from hokma.db.timeutil import to_db_time, utcnow

MAX_TEXT = 4000
OWNER = "owner"
DEFAULT_MANAGER = "hokma"
MAX_NAME = 80


class LessonError(Exception):
    """Base of the errors of the lesson log."""


class NotExecuted(LessonError):
    """The order did not execute (it was refused, is waiting for the owner, was only a preview, or does not exist)."""


class NotReady(LessonError):
    """The outcome cannot be measured yet: no closing fill and no mark, or the window has not traded."""


class NotPromotable(LessonError):
    """The lesson cannot go to the general memory (not settled, or the approver is not allowed)."""


class UnknownLesson(LessonError):
    """There is no lesson with that id."""


@dataclass(frozen=True)
class Lesson:
    id: int
    desk_id: int | None
    scope: str
    decision_id: int
    order_id: int
    text: str
    outcome: dict[str, Any] | None
    measured_from: str | None
    status: str
    proposed_by: str
    approved_by: str | None
    created_at: str
    settled_at: str | None
    promoted_at: str | None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Lesson":
        raw = row.get("outcome_json")
        try:
            outcome = json.loads(raw) if raw else None
        except ValueError:
            outcome = None
        return cls(int(row["id"]), row.get("desk_id"), row["scope"], int(row["decision_id"]), int(row["order_id"]), row["text"],
                   outcome if isinstance(outcome, dict) else None, row.get("measured_from"), row["status"], row["proposed_by"],
                   row.get("approved_by"), row["created_at"], row.get("settled_at"), row.get("promoted_at"))


def _db(db: Database | None) -> Database:
    return db or get_database()


def _clean_name(value: str, what: str) -> str:
    text = (value or "").strip()
    if not 1 <= len(text) <= MAX_NAME:
        raise LessonError(f"{what} must have 1 to {MAX_NAME} characters")
    return text


def executed_order(session: Any, order_id: int, desk_id: int) -> dict[str, Any]:
    """The row of `broker_orders` when it is an order that was sent and accepted for this desk, else NotExecuted.

    Executed means: action `place`, a broker order id, not refused by the rules, no error in the answer, and a 2xx status when
    there is one. A preview, a refused attempt, an attempt that waits for the owner and a cancel are not executions."""
    row = session.one("SELECT * FROM broker_orders WHERE id = :i", {"i": order_id})
    if row is None:
        raise NotExecuted(f"order {order_id} does not exist")
    if row.get("desk_id") != desk_id:
        raise NotExecuted(f"order {order_id} belongs to another desk")
    if row["action"] != "place":
        raise NotExecuted(f"order {order_id} is a {row['action']}, not a placed order")
    if not row.get("broker_order_id"):
        raise NotExecuted(f"order {order_id} has no broker order id: it was not sent")
    if row.get("rule_result") in ("refused", "needs_owner"):
        raise NotExecuted(f"order {order_id} was {row['rule_result']}")
    status = row.get("http_status")
    if status is not None and not 200 <= int(status) <= 299:
        raise NotExecuted(f"order {order_id} got HTTP {status}")
    try:
        response = json.loads(row["response_json"]) if row.get("response_json") else {}
    except ValueError:
        response = {}
    if isinstance(response, dict) and response.get("error"):
        raise NotExecuted(f"order {order_id} came back with an error")
    return row


def propose(desk_id: int, decision_id: int, order_id: int, text: str, proposed_by: str, *, db: Database | None = None) -> Lesson:
    """Record a lesson of a desk, linked to the decision and to the order that executed it. Scope is `desk`.

    Raises NotExecuted when the order did not execute. The same (desk, order, text) returns the existing lesson."""
    text = (text or "").strip()
    if not 1 <= len(text) <= MAX_TEXT:
        raise LessonError(f"a lesson has 1 to {MAX_TEXT} characters")
    author = _clean_name(proposed_by, "proposed_by")
    with _db(db).write() as u:
        s = u.session
        if s.one("SELECT 1 AS x FROM desks WHERE id = :i", {"i": desk_id}) is None:
            raise LessonError(f"desk {desk_id} does not exist")
        order = executed_order(s, order_id, desk_id)
        if s.one("SELECT 1 AS x FROM decisions WHERE id = :i", {"i": decision_id}) is None:
            raise LessonError(f"decision {decision_id} does not exist")
        same = s.one("SELECT * FROM lessons WHERE desk_id = :d AND order_id = :o AND text = :t ORDER BY id LIMIT 1",
                     {"d": desk_id, "o": order_id, "t": text})
        if same is not None:
            return Lesson.from_row(same)
        new_id = s.insert("lessons", {"desk_id": desk_id, "scope": "desk", "decision_id": decision_id, "order_id": order_id,
                                      "text": text, "status": "proposed", "proposed_by": author, "created_at": utcnow()})
        return Lesson.from_row(s.one("SELECT * FROM lessons WHERE id = :i", {"i": new_id}))


def get(lesson_id: int, *, db: Database | None = None) -> Lesson:
    with _db(db).read() as u:
        row = u.session.one("SELECT * FROM lessons WHERE id = :i", {"i": lesson_id})
    if row is None:
        raise UnknownLesson(f"lesson {lesson_id} does not exist")
    return Lesson.from_row(row)


def promote(lesson_id: int, *, approved_by: str, manager: str = DEFAULT_MANAGER, now: str | None = None,
            db: Database | None = None) -> Lesson:
    """Move a settled lesson to the general memory. `approved_by` must be the owner or the Hokma manager agent (`manager`).

    A lesson that is not settled (no measured outcome) or was retired cannot be promoted. Promoting twice changes nothing."""
    approver = _clean_name(approved_by, "approved_by")
    if approver not in (OWNER, manager):
        raise NotPromotable(f"{approver!r} cannot promote a lesson: only the owner or the Hokma manager can")
    with _db(db).write() as u:
        s = u.session
        row = s.one("SELECT * FROM lessons WHERE id = :i", {"i": lesson_id})
        if row is None:
            raise UnknownLesson(f"lesson {lesson_id} does not exist")
        if row["status"] == "promoted":
            return Lesson.from_row(row)
        if row["status"] != "settled":
            raise NotPromotable(f"lesson {lesson_id} is {row['status']}: only a settled lesson can be promoted")
        s.update("lessons", {"id": lesson_id}, {"scope": "general", "status": "promoted", "approved_by": approver,
                                                "promoted_at": to_db_time(now) if now else utcnow()})
        return Lesson.from_row(s.one("SELECT * FROM lessons WHERE id = :i", {"i": lesson_id}))


def retire(lesson_id: int, *, db: Database | None = None) -> Lesson:
    """Take a lesson out of use. The row stays; its status becomes `retired`."""
    with _db(db).write() as u:
        s = u.session
        if s.one("SELECT 1 AS x FROM lessons WHERE id = :i", {"i": lesson_id}) is None:
            raise UnknownLesson(f"lesson {lesson_id} does not exist")
        s.update("lessons", {"id": lesson_id}, {"status": "retired"})
        return Lesson.from_row(s.one("SELECT * FROM lessons WHERE id = :i", {"i": lesson_id}))


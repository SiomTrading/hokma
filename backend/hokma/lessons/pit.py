"""Point-in-time reading of lessons (SPEC-017: TA-47).

`for_desk(desk_id, as_of=...)` returns what a desk knew at `as_of`: its own lessons and the promoted general ones, nothing created
after that moment, and no outcome that had not been measured by then. A lesson whose outcome was measured after `as_of` is
returned as it was at `as_of`: its text, with no outcome and the status `proposed`. A run on past data therefore never learns
from a result that had not happened yet. Retired lessons are not read. Lessons of other desks never appear.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from hokma.db import Database, get_database
from hokma.db.timeutil import to_db_time
from hokma.lessons.store import Lesson


def _as_known(lesson: Lesson, cutoff: str) -> Lesson:
    """The lesson as it stood at `cutoff`: a result measured later, or a promotion made later, is not visible yet."""
    if lesson.settled_at is not None and lesson.settled_at > cutoff:
        return replace(lesson, outcome=None, measured_from=None, status="proposed", scope="desk", settled_at=None,
                       promoted_at=None, approved_by=None)
    if lesson.promoted_at is not None and lesson.promoted_at > cutoff:
        return replace(lesson, status="settled", scope="desk", promoted_at=None, approved_by=None)
    return lesson


def for_desk(desk_id: int, *, as_of: datetime | str, limit: int | None = None, db: Database | None = None) -> list[Lesson]:
    """Lessons visible to `desk_id` at `as_of`, oldest first.

    The desk's own lessons: created at or before `as_of`, even if one was promoted later (it is then read as a desk lesson). General lessons: promoted at or before `as_of` (only status
    `promoted`), from any desk. `limit` keeps the most recent ones."""
    cutoff = to_db_time(as_of)
    if cutoff is None:
        raise ValueError("as_of is required")
    with (db or get_database()).read() as u:
        rows = u.session.query(
            "SELECT * FROM lessons WHERE status <> 'retired' AND created_at <= :c AND ("
            " desk_id = :d"
            " OR (scope = 'general' AND status = 'promoted' AND promoted_at IS NOT NULL AND promoted_at <= :c)"
            ") ORDER BY created_at, id", {"c": cutoff, "d": desk_id})
    lessons = [_as_known(Lesson.from_row(r), cutoff) for r in rows]
    return lessons[-limit:] if limit else lessons

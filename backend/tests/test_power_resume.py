"""Resume after sleep: a clock gap over 2 minutes makes the scheduler run, once each, what fell due while asleep."""
import asyncio
import sys
from datetime import datetime, timedelta, timezone

import pytest

from agents_support import db, work  # noqa: F401
from hokma.power import resume
from hokma.scheduler import loop
from test_scheduler_jobs import _status, activate_due, make_scheduler, wait_until

T0 = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
posix = pytest.mark.skipif(sys.platform == "win32", reason="the fake claude is a POSIX script")


def test_slept_needs_a_gap_over_two_minutes():
    assert resume.slept(None, T0) is None
    assert resume.slept(T0, T0 + timedelta(seconds=15)) is None
    assert resume.slept(T0, T0 + timedelta(minutes=2)) is None
    assert resume.slept(T0, T0 + timedelta(minutes=10)) == timedelta(minutes=10)
    assert resume.slept(T0, T0 + timedelta(days=3)) == resume.WINDOW
    assert resume.slept(T0, T0 - timedelta(minutes=10)) is None            # clock moved back


@posix
def test_a_job_that_fell_due_while_asleep_runs_once(db, work):
    now = datetime.now(timezone.utc)
    job_id = activate_due(db, work, kind="daily", expr="07:30", next=now - timedelta(minutes=10))

    async def go():
        scheduler = make_scheduler(db, work)
        assert scheduler.process_due(now, grace=resume.slept(now - timedelta(minutes=12), now)) == []
        ok = await wait_until(lambda: _status(db, job_id) == "done")
        scheduler.process_due(now, grace=resume.slept(now, now + timedelta(minutes=10)))     # same pass again
        await asyncio.sleep(0.3)
        return ok

    assert asyncio.run(go())
    assert len(work.entries("start")) == 1
    with db.read() as u:
        assert u.scheduled_jobs.get(job_id).next_run_at > now.strftime("%Y-%m-%d %H:%M")


def test_without_a_resume_a_late_job_is_still_skipped(db, work):
    now = datetime.now(timezone.utc)
    job_id = activate_due(db, work, kind="daily", expr="07:30", next=now - timedelta(minutes=10))
    assert [s.job_id for s in make_scheduler(db, work).process_due(now)] == [job_id]
    assert work.entries("start") == []


def test_a_job_older_than_the_gap_is_still_skipped(db, work):
    now = datetime.now(timezone.utc)
    job_id = activate_due(db, work, kind="daily", expr="07:30", next=now - timedelta(hours=2))
    skipped = make_scheduler(db, work).process_due(now, grace=timedelta(minutes=30))
    assert [s.job_id for s in skipped] == [job_id]
    assert work.entries("start") == []


@posix
def test_the_loop_passes_the_gap_to_the_pass(db, work, monkeypatch):
    clock = iter([T0, T0 + timedelta(seconds=15), T0 + timedelta(minutes=30), T0 + timedelta(minutes=30, seconds=15)])
    seen = []

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(clock, T0 + timedelta(hours=1))

    monkeypatch.setattr(loop, "datetime", FakeDatetime)

    async def go():
        scheduler = make_scheduler(db, work)
        monkeypatch.setattr(scheduler, "process_due", lambda now, **kw: seen.append(kw.get("grace")) or [])
        await scheduler.start()
        await wait_until(lambda: len(seen) >= 4, 5)
        await scheduler.stop()

    asyncio.run(go())
    assert seen[:4] == [None, None, timedelta(minutes=29, seconds=45), None]

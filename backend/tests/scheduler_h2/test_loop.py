"""The scanner fires every due job exactly once, recovers the one missed while the machine was off and never runs the same
execution twice (SPEC-018 B7: OA-32). The fake claude of the scheduler tests stands in for the engine."""
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone

import pytest

from agents_support import db, work  # noqa: F401  (fixtures)
from hokma.scheduler import loop
from test_scheduler_jobs import _job, _status, activate_due, make_scheduler, wait_until

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the fake claude is a POSIX script")


def hours_ago(h):
    return datetime.now(timezone.utc) - timedelta(hours=h)


def tasks_of(db, job_id):
    with db.read() as u:
        return u.session.scalar("SELECT count(*) FROM tasks WHERE title = :n", {"n": u.scheduled_jobs.get(job_id).name})


def test_a_due_job_fires_once_even_when_the_scan_runs_again_before_it_ends(db, work):
    job_id = activate_due(db, work, kind="daily", expr="07:30", next=datetime.now(timezone.utc) - timedelta(seconds=5))

    async def go():
        scheduler = make_scheduler(db, work)
        now = datetime.now(timezone.utc)
        for _ in range(5):                                  # five ticks before the first run has even started
            scheduler.process_due(now)
        assert await wait_until(lambda: not scheduler.running_ids() and _status(db, job_id) == "done")
        scheduler.process_due(datetime.now(timezone.utc))   # and one after it: its next run is tomorrow
        await asyncio.sleep(0.2)

    asyncio.run(go())
    assert len(work.entries("start")) == 1 and tasks_of(db, job_id) == 1
    assert _job(db, job_id).next_run_at > datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def test_two_scanners_that_read_the_same_due_job_run_it_once(db, work):
    """The concurrency case: both read the due list before either has started the run."""
    job_id = activate_due(db, work, kind="interval", expr="30m", next=datetime.now(timezone.utc) - timedelta(seconds=5))

    async def go():
        first, second = make_scheduler(db, work), make_scheduler(db, work)
        now = datetime.now(timezone.utc)
        first.process_due(now)
        second.process_due(now)                              # its due list still shows the job: the claim stops it
        assert first.running_ids() == [job_id] and second.running_ids() == []
        assert await wait_until(lambda: not first.running_ids())
        assert loop._INFLIGHT == set()                       # the claim is released when the run ends

    asyncio.run(go())
    assert len(work.entries("start")) == 1 and tasks_of(db, job_id) == 1


def test_a_stale_read_of_a_job_that_already_ran_is_not_started_again(db, work):
    job_id = activate_due(db, work, kind="interval", expr="30m", next=datetime.now(timezone.utc) - timedelta(seconds=5))
    stale = _job(db, job_id)                                 # what a slow scanner read before the run

    async def go():
        scheduler = make_scheduler(db, work)
        scheduler.process_due(datetime.now(timezone.utc))
        assert await wait_until(lambda: not scheduler.running_ids() and _status(db, job_id) == "done")
        assert scheduler._launch(stale, datetime.now(timezone.utc)) is False       # its next_run_at has moved on
        await asyncio.sleep(0.2)

    asyncio.run(go())
    assert len(work.entries("start")) == 1 and tasks_of(db, job_id) == 1


def test_a_job_turned_off_after_the_scan_read_it_is_not_started(db, work):
    job_id = activate_due(db, work, next=datetime.now(timezone.utc) - timedelta(seconds=5))
    stale = _job(db, job_id)
    with db.write() as u:
        u.scheduled_jobs.set_active(job_id, False)

    async def go():
        scheduler = make_scheduler(db, work)
        assert scheduler._launch(stale, datetime.now(timezone.utc)) is False
        await asyncio.sleep(0.1)

    asyncio.run(go())
    assert work.entries("start") == [] and loop._INFLIGHT == set()


def test_run_now_does_not_race_a_run_that_is_in_flight(db, work):
    job_id = activate_due(db, work, kind="interval", expr="30m", next=datetime.now(timezone.utc) - timedelta(seconds=5))

    async def go():
        first, second = make_scheduler(db, work), make_scheduler(db, work)
        first.process_due(datetime.now(timezone.utc))
        assert second.run_now(_job(db, job_id)) is False     # another scanner holds this job
        assert await wait_until(lambda: not first.running_ids())

    asyncio.run(go())
    assert len(work.entries("start")) == 1


def test_a_run_missed_while_the_machine_was_off_runs_once_and_says_it_was_late(db, work):
    job_id = activate_due(db, work, kind="daily", expr="07:30", next=hours_ago(5))
    planned = _job(db, job_id).next_run_at

    async def go():
        scheduler = make_scheduler(db, work)
        assert scheduler.process_due(datetime.now(timezone.utc), startup=True) == []
        assert await wait_until(lambda: not scheduler.running_ids() and _status(db, job_id) == "done")

    asyncio.run(go())
    assert len(work.entries("start")) == 1
    with db.read() as u:
        refs = [e.refs for e in u.journal.list() if isinstance(e.refs, dict) and e.refs.get("scheduled_job") == job_id]
    assert {"scheduled_job": job_id, "late_planned": planned} in refs


def test_one_job_that_cannot_start_does_not_stop_the_others(db, work, monkeypatch, caplog):
    bad = activate_due(db, work, next=datetime.now(timezone.utc) - timedelta(seconds=5))
    with db.write() as u:
        u.scheduled_jobs.update(bad, name="Bad job")
    good = activate_due(db, work, kind="interval", expr="30m", next=datetime.now(timezone.utc) - timedelta(seconds=1))
    original = loop.Scheduler._claim

    def claim(self, job, move_on):
        if job.id == bad:
            raise RuntimeError("boom")
        return original(self, job, move_on)

    monkeypatch.setattr(loop.Scheduler, "_claim", claim)

    async def go():
        scheduler = make_scheduler(db, work)
        with caplog.at_level(logging.ERROR, logger="hokma.scheduler"):
            scheduler.process_due(datetime.now(timezone.utc))
        assert await wait_until(lambda: not scheduler.running_ids() and _status(db, good) == "done")

    asyncio.run(go())
    assert len(work.entries("start")) == 1 and "could not be started" in caplog.text and _job(db, bad).last_run_at is None


def test_a_scan_that_overlaps_another_one_is_skipped(db, work):
    job_id = activate_due(db, work, next=datetime.now(timezone.utc) - timedelta(seconds=5))
    scheduler = make_scheduler(db, work)
    assert scheduler._scan_lock.acquire(blocking=False)
    try:
        assert scheduler.process_due(datetime.now(timezone.utc)) == [] and scheduler.running_ids() == []
    finally:
        scheduler._scan_lock.release()
    assert work.entries("start") == [] and _job(db, job_id).last_run_at is None

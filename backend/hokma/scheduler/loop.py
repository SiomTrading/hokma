"""The loop that fires scheduled jobs while Hokma is open.

- Every tick it runs the jobs that are due, are on and carry an approval of the owner.
- A run that is more than GRACE late is marked skipped. After a sleep (see `power.resume`) the runs that fell due
  while it slept are executed once each.
- Exactly once (OA-32): a run is claimed before it starts. The claim is an in-flight record shared by every scanner of the process
  (two scanners, a manual "run now" racing a due run) plus a fresh read of the job right before the start: a job that was
  already started, turned off, or moved on is not started again. A scan that overlaps another one is skipped, and one job that
  fails to start never stops the others.
- On startup a run missed while Hokma was closed (one per routine, however many times it was missed) runs once and
  says so (`late_planned`) when it is inside the window: 24 h, 3 h for the briefing. Older than that it is not run:
  it is marked skipped and the owner is asked (Telegram) whether to run it now.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from datetime import datetime, timedelta, timezone

from hokma.db import Database, DbError, from_db_time, get_database
from hokma.i18n import t
from hokma.power import resume
from hokma.scheduler import runner
from hokma.scheduler.service import BRIEFING_NAME

log = logging.getLogger("hokma.scheduler")

DEFAULT_TICK = 15.0
GRACE = timedelta(seconds=120)
LATE_WINDOW = resume.WINDOW                  # how late a missed routine may still run when Hokma opens
BRIEFING_WINDOW = timedelta(hours=3)         # the premarket briefing is worth nothing after the market opens
MAX_PARALLEL = 3
_INFLIGHT: set[tuple[str, int]] = set()      # (database, job id) of every run that was claimed and has not ended
_INFLIGHT_LOCK = threading.Lock()
OUTCOME_LISTENERS: list = []      # callables `(job, outcome)` told after each run; a failing one is logged, never raised


def tick_seconds() -> float:
    try:
        return max(0.05, float(os.environ.get("HOKMA_SCHEDULER_TICK", DEFAULT_TICK)))
    except ValueError:
        return DEFAULT_TICK


class Scheduler:
    def __init__(self, db: Database | None = None, *, job_runner: runner.JobRunner | None = None,
                 tick: float | None = None, grace: timedelta = GRACE) -> None:
        self._db = db
        self._runner = job_runner
        self.tick = tick or tick_seconds()
        self.grace = grace
        self._task: asyncio.Task | None = None
        self._running: dict[int, asyncio.Task] = {}
        self._gate = asyncio.Semaphore(MAX_PARALLEL)
        self._stop = asyncio.Event()
        self._scan_lock = threading.Lock()

    @property
    def db(self) -> Database:
        return self._db or get_database()

    @property
    def job_runner(self) -> runner.JobRunner:
        if self._runner is None:
            from hokma.rpc.ws import SESSION_TOKEN
            self._runner = runner.JobRunner(self.db, session_token=SESSION_TOKEN)
        return self._runner

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self.recover_interrupted()
        self._task = asyncio.create_task(self._loop(), name="hokma-scheduler")

    def recover_interrupted(self) -> list[int]:
        """Bug 1134: a run that was still `running` when Hokma went down (killed, power cut) has no process any more.
        Mark it failed as interrupted so the list does not show it as running forever. Returns the task ids."""
        if not self._ready():
            return []
        fixed: list[int] = []
        try:
            with self.db.write() as u:
                for job in u.scheduled_jobs.list():
                    if job.id in self._running or job.last_task_id is None:
                        continue
                    task = u.tasks.find(job.last_task_id)
                    if task is None or task.status != "running":
                        continue
                    reason = t("sched.error.interrupted")
                    u.tasks.fail(task.id, reason)
                    u.journal.add("alert", t("sched.journal.failed", name=job.name), actor=runner.SYSTEM,
                                  outcome="failure", agent_slug=job.agent_slug, task_id=task.id, body=reason,
                                  refs={"scheduled_job": job.id, "outcome": "interrupted"})
                    fixed.append(task.id)
        except DbError:
            log.exception("could not recover interrupted runs")
        return fixed

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        for task in list(self._running.values()):
            task.cancel()
        await asyncio.gather(*self._running.values(), return_exceptions=True)
        self._running.clear()

    async def _loop(self) -> None:
        first, last = True, None
        while not self._stop.is_set():
            now = datetime.now(timezone.utc)
            try:
                self.process_due(now, startup=first, grace=resume.slept(last, now))
            except DbError:
                log.exception("scheduler tick failed")
            except Exception:
                log.exception("scheduler tick crashed")
            first, last = False, now
            try:
                await asyncio.wait_for(self._stop.wait(), self.tick)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------ one pass

    def process_due(self, now: datetime, *, startup: bool = False, grace: timedelta | None = None) -> list[runner.RunOutcome]:
        """Start what is due; mark as skipped what is too late. Returns the skips (runs are async tasks)."""
        if not self._ready():
            return []
        if not self._scan_lock.acquire(blocking=False):
            log.info("scheduler scan skipped: the previous one is still running")
            return []
        try:
            return self._scan(now, startup, grace)
        finally:
            self._scan_lock.release()

    def _scan(self, now: datetime, startup: bool, grace: timedelta | None) -> list[runner.RunOutcome]:
        skipped: list[runner.RunOutcome] = []
        with self.db.read() as u:
            due = u.scheduled_jobs.due(now)
        briefing = self._briefing_id() if due else None
        for job in due:
            if job.id in self._running or job.approval_id is None:
                continue
            try:
                planned = from_db_time(job.next_run_at) if job.next_run_at else now
                limit = LATE_WINDOW if startup else (grace or self.grace)
                if job.id == briefing or job.name == BRIEFING_NAME:
                    limit = min(limit, BRIEFING_WINDOW)
                late = now - planned
                if late > limit:
                    # Past the window at startup the owner decides (the skip is recorded, the buttons come by Telegram).
                    claimed = self._claim(job, True)
                    if claimed is None:
                        continue
                    try:
                        outcome = (runner.skip(self.db, claimed, now, "asked", limit) if startup
                                   else runner.skip(self.db, claimed, now, "missed"))
                    finally:
                        self._release(job)
                    skipped.append(outcome)
                    self._notify(job, outcome)
                    continue
                self._launch(job, now, late_planned=job.next_run_at if late > GRACE else None)
            except Exception:                       # one job never stops the others
                log.exception("scheduled job %s could not be started", job.name)
        return skipped

    # ------------------------------------------------------------------ exactly once

    def _key(self, job) -> tuple[str, int]:
        return (str(getattr(self.db, "path", None) or id(self.db)), job.id)

    def _claim(self, job, move_on: bool):
        """The job to run, or None when this run is not ours: someone else holds it, or it is no longer due as it was read.
        `move_on=False` (the owner asked for it now) only needs the in-flight claim."""
        key = self._key(job)
        with _INFLIGHT_LOCK:
            if key in _INFLIGHT:
                return None
            _INFLIGHT.add(key)
        if not move_on:
            return job
        try:
            with self.db.read() as u:
                fresh = u.scheduled_jobs.get(job.id)
        except Exception:                           # deleted meanwhile, or the database is busy: not started now
            self._release(job)
            return None
        if fresh is None or not fresh.is_active or fresh.approval_id is None or fresh.next_run_at != job.next_run_at:
            self._release(job)                      # already started, turned off, edited or moved on by another scanner
            return None
        return fresh

    def _release(self, job) -> None:
        with _INFLIGHT_LOCK:
            _INFLIGHT.discard(self._key(job))

    def _briefing_id(self) -> int | None:
        from hokma.today.service import get_config
        try:
            return get_config(self.db)["briefing_job_id"]
        except Exception:
            return None

    def _ready(self) -> bool:
        from hokma.db import current_version
        try:
            return current_version(self.db.path) > 0
        except Exception:
            return False

    @staticmethod
    def _notify(job, outcome: runner.RunOutcome) -> None:
        for listener in list(OUTCOME_LISTENERS):
            try:
                listener(job, outcome)
            except Exception:
                log.exception("outcome listener failed")

    def run_now(self, job) -> bool:
        """The owner asked to run it now: same run, same notice, but the planned `next_run_at` stays. False if it runs."""
        if job.id in self._running:
            return False
        return self._launch(job, datetime.now(timezone.utc), move_on=False)

    def _launch(self, job, now: datetime, move_on: bool = True, late_planned: str | None = None) -> bool:
        """Start the run in the background. False when the run is not ours (see `_claim`)."""
        claimed = self._claim(job, move_on)
        if claimed is None:
            return False

        async def go() -> None:
            try:
                async with self._gate:
                    outcome = await self.job_runner.run(claimed, now, move_on=move_on, late_planned=late_planned)
                    log.info("scheduled job %s: %s", claimed.name, outcome.status)
                    self._notify(claimed, outcome)
            finally:
                self._release(claimed)

        try:
            task = asyncio.create_task(go(), name=f"hokma-job-{job.id}")
        except Exception:
            self._release(claimed)
            raise
        self._running[job.id] = task
        task.add_done_callback(lambda _t, jid=job.id: self._running.pop(jid, None))
        return True

    def running_ids(self) -> list[int]:
        return sorted(self._running)

"""The loop that fires scheduled jobs while Hokma is open.

- Every tick it runs the jobs that are due, are on and carry an approval of the owner.
- A run that is more than GRACE late (Hokma was closed) is marked skipped, never executed late. After a sleep
  (see `power.resume`) the runs that fell due while it slept are executed once each.
- On startup every run that fell due while Hokma was closed is marked skipped.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from hokma.db import Database, DbError, from_db_time, get_database
from hokma.power import resume
from hokma.scheduler import runner

log = logging.getLogger("hokma.scheduler")

DEFAULT_TICK = 15.0
GRACE = timedelta(seconds=120)
MAX_PARALLEL = 3
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
        self._task = asyncio.create_task(self._loop(), name="hokma-scheduler")

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
        skipped: list[runner.RunOutcome] = []
        with self.db.read() as u:
            due = u.scheduled_jobs.due(now)
        for job in due:
            if job.id in self._running or job.approval_id is None:
                continue
            planned = from_db_time(job.next_run_at) if job.next_run_at else now
            if startup or now - planned > (grace or self.grace):
                skipped.append(runner.skip(self.db, job, now, "missed"))
                continue
            self._launch(job, now)
        return skipped

    def _ready(self) -> bool:
        from hokma.db import current_version
        try:
            return current_version(self.db.path) > 0
        except Exception:
            return False

    def _launch(self, job, now: datetime) -> None:
        async def go() -> None:
            async with self._gate:
                outcome = await self.job_runner.run(job, now)
                log.info("scheduled job %s: %s", job.name, outcome.status)
                for listener in list(OUTCOME_LISTENERS):
                    try:
                        listener(job, outcome)
                    except Exception:
                        log.exception("outcome listener failed")

        task = asyncio.create_task(go(), name=f"hokma-job-{job.id}")
        self._running[job.id] = task
        task.add_done_callback(lambda _t, jid=job.id: self._running.pop(jid, None))

    def running_ids(self) -> list[int]:
        return sorted(self._running)

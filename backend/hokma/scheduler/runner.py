"""One scheduled run: a task row, a task run, a `claude -p` process, and the result back in the database.

The run is unattended: any permission request the agent makes is denied (nobody is there to answer) and recorded in
`guard_events`. Tokens and cost come from the `result` line of the stream-json output, the same source the chat uses.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from hokma.agents.files import TIER_MODEL
from hokma.db import Actor, Conflict, Database, DbError
from hokma.db.models import ScheduledJob
from hokma.engine import stream
from hokma.engine.process import ClaudeProcess, find_claude
from hokma.i18n import t
from hokma.kit.launch import LaunchSpec, prepare_claude_launch
from hokma.scheduler import schedule

log = logging.getLogger("hokma.scheduler")

RUN_FLAGS = ["-p", "--output-format", "stream-json", "--input-format", "stream-json", "--verbose",
             "--permission-prompt-tool", "stdio", "--permission-mode", "default"]
SECRET_ENV = ("HOKMA_SESSION_TOKEN", "HOKMA_TOKEN")
MAX_SUMMARY = 3900
SYSTEM = Actor.system("scheduler")


@dataclass
class RunOutcome:
    job_id: int
    task_id: int | None
    status: str                   # done | failed | skipped
    detail: str = ""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def advance(job: ScheduledJob, now: datetime) -> tuple[datetime | None, bool]:
    """(next run time, keep the job on). A `once` job turns itself off after it fires or is skipped."""
    if job.schedule_kind == "once":
        return None, False
    following = schedule.next_run(job.schedule_kind, job.schedule_expr, job.timezone, now)
    return following, following is not None


def skip(db: Database, job: ScheduledJob, now: datetime, reason: str) -> RunOutcome:
    """The run did not happen (Hokma was closed or the agent is off). Never executed late."""
    following, keep = advance(job, now)
    planned = job.next_run_at or ""
    body = t("sched.skip.agent_off", planned=planned) if reason == "agent_off" else t("sched.skip.missed", planned=planned)
    with db.write() as u:
        u.scheduled_jobs.update(job.id, next_run_at=following)
        if not keep:
            u.scheduled_jobs.set_active(job.id, False)
        u.journal.add("note", t("sched.journal.skipped", name=job.name), actor=SYSTEM, outcome="partial",
                      agent_slug=job.agent_slug, body=body,
                      refs={"scheduled_job": job.id, "planned": job.next_run_at, "reason": reason})
    return RunOutcome(job.id, None, "skipped", reason)


def _prepare(db: Database, job: ScheduledJob, now: datetime) -> tuple[int, str] | RunOutcome | None:
    """Create the task, claim it, open the run and move the job on, in one transaction (so it can fire only once)."""
    following, keep = advance(job, now)
    try:
        with db.write() as u:
            agent = u.agents.find(job.agent_slug)
            if agent is None or agent.status != "active":
                return None
            model = TIER_MODEL[agent.model_tier]
            task = u.tasks.create(job.name, actor=SYSTEM, body=job.prompt, assigned_agent=agent.slug, status="ready")
            u.tasks.claim(task.id, agent.slug, actor=SYSTEM, run_model=model)
            u.scheduled_jobs.mark_ran(job.id, task_id=task.id, last_run_at=now, next_run_at=following)
            if not keep:
                u.scheduled_jobs.set_active(job.id, False)
            return task.id, model
    except Conflict as exc:
        return RunOutcome(job.id, None, "skipped", str(exc))


class JobRunner:
    def __init__(self, db: Database, *, launch: Callable[[], LaunchSpec] = prepare_claude_launch,
                 claude_bin: str | None = None, session_token: str = "") -> None:
        self.db, self.launch, self.claude_bin, self.session_token = db, launch, claude_bin, session_token

    async def run(self, job: ScheduledJob, now: datetime | None = None) -> RunOutcome:
        now = now or _now()
        prepared = _prepare(self.db, job, now)
        if isinstance(prepared, RunOutcome):
            return prepared
        if prepared is None:
            return skip(self.db, job, now, "agent_off")
        task_id, model = prepared
        with self.db.read() as u:
            max_seconds = u.tasks.get(task_id).max_runtime_seconds
        try:
            text, usage = await asyncio.wait_for(self._execute(job, task_id, model), max_seconds)
        except asyncio.TimeoutError:
            return self._fail(job, task_id, t("sched.error.timeout", seconds=max_seconds), "timed_out")
        except _RunFailed as exc:
            return self._fail(job, task_id, str(exc), "failure", exc.usage)
        except Exception as exc:                                      # never leave a task running
            log.exception("scheduled run %s crashed", job.name)
            return self._fail(job, task_id, f"{type(exc).__name__}: {exc}", "crashed")
        return self._finish(job, task_id, text, usage)

    # ------------------------------------------------------------------ process

    async def _execute(self, job: ScheduledJob, task_id: int, model: str) -> tuple[str, dict[str, Any]]:
        spec = self.launch()
        env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV and (not self.session_token or v != self.session_token)}
        env.update(spec.env)
        claude = self.claude_bin or find_claude(env)
        if not claude:
            raise _RunFailed(t("sched.error.no_claude"))
        argv = [claude, *spec.args, *RUN_FLAGS, "--model", model, "--agent", job.agent_slug]
        proc = await ClaudeProcess.spawn(argv, env, spec.cwd)
        try:
            with self.db.write() as u:
                run = u.task_runs.current(task_id)
                if run is not None:
                    u.task_runs.set_pid(run.id, proc._proc.pid)
            await proc.write(stream.user_message([{"type": "text", "text": job.prompt}]))
            last_text = ""
            while True:
                msg = await proc.read()
                if msg is None:
                    raise _RunFailed(t("sched.error.exited", detail=proc.stderr_tail()[-300:]))
                kind = msg.get("type")
                if kind == "control_request":
                    await self._deny(proc, job, task_id, msg)
                elif kind == "assistant":
                    text = stream.assistant_text(msg)
                    if text:
                        last_text = text
                elif kind == "result":
                    usage = stream.usage_of(msg)
                    if msg.get("is_error") or str(msg.get("subtype", "")).startswith("error"):
                        raise _RunFailed(str(msg.get("result") or msg.get("subtype") or "error")[:500], usage)
                    return str(msg.get("result") or last_text or ""), usage
        finally:
            await proc.terminate()

    async def _deny(self, proc: ClaudeProcess, job: ScheduledJob, task_id: int, msg: dict[str, Any]) -> None:
        request = msg.get("request") or {}
        request_id = str(msg.get("request_id", ""))
        if request.get("subtype") != "can_use_tool":
            await proc.write(stream.control_error(request_id, "unsupported request"))
            return
        tool = str(request.get("tool_name", ""))
        await proc.write(stream.permission_reply(request_id, False, request.get("input"), t("sched.permission.denied")))
        with self.db.write() as u:
            u.guard_events.record("permission", "blocked", "unattended scheduled run: nobody can answer",
                                  agent_slug=job.agent_slug, tool=tool, task_id=task_id)

    # ------------------------------------------------------------------ result

    def _finish(self, job: ScheduledJob, task_id: int, text: str, usage: dict[str, Any]) -> RunOutcome:
        summary = (text.strip() or t("sched.result.empty"))[:MAX_SUMMARY]
        with self.db.write() as u:
            run = u.task_runs.current(task_id)
            if run is not None:
                u.task_runs.add_tokens(run.id, usage["tokens_in"], usage["tokens_out"])
            u.tasks.complete(task_id, summary, actor=SYSTEM,
                             metadata={"scheduled_job": job.id, "cache_read": usage["cache_read"],
                                       "cost_usd": usage["cost_usd"]})
            u.journal.add("action", t("sched.journal.done", name=job.name), actor=SYSTEM, outcome="success",
                          agent_slug=job.agent_slug, task_id=task_id, body=summary[:2000],
                          refs={"scheduled_job": job.id})
        return RunOutcome(job.id, task_id, "done", summary)

    def _fail(self, job: ScheduledJob, task_id: int, error: str, outcome: str,
              usage: dict[str, Any] | None = None) -> RunOutcome:
        try:
            with self.db.write() as u:
                run = u.task_runs.current(task_id)
                if run is not None and usage:
                    u.task_runs.add_tokens(run.id, usage["tokens_in"], usage["tokens_out"])
                u.tasks.fail(task_id, error)
                u.journal.add("alert", t("sched.journal.failed", name=job.name), actor=SYSTEM, outcome="failure",
                              agent_slug=job.agent_slug, task_id=task_id, body=error[:2000],
                              refs={"scheduled_job": job.id, "outcome": outcome})
        except DbError:
            log.exception("could not record the failure of %s", job.name)
        return RunOutcome(job.id, task_id, "failed", error)


class _RunFailed(Exception):
    def __init__(self, message: str, usage: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.usage = usage

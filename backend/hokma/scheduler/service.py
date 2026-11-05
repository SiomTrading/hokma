"""Scheduled jobs for the panel: list, create, edit, turn on (with the owner's confirmation) and off."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from hokma.agents.files import TIER_MODEL, clean_name, clean_text
from hokma.agents.service import AgentsError
from hokma.db import Database, DbError, IntegrityViolation, NotFound, get_database
from hokma.scheduler import schedule

MAX_PROMPT = 4000


def _schedule_error(exc: schedule.ScheduleError) -> AgentsError:
    return AgentsError(exc.code, 422, str(exc))


class JobsService:
    def __init__(self, db: Database | None = None) -> None:
        self._db = db

    @property
    def db(self) -> Database:
        return self._db or get_database()

    # ------------------------------------------------------------------ reading

    def _item(self, u, job, agents: dict[str, Any]) -> dict[str, Any]:
        agent = agents.get(job.agent_slug)
        last = None
        if job.last_task_id is not None:
            task = u.tasks.find(job.last_task_id)
            runs = u.task_runs.list(job.last_task_id)
            run = runs[-1] if runs else None
            if task is not None:
                last = {"task_id": task.id, "status": task.status, "summary": (task.result_summary or "")[:2000],
                        "error": run.error if run else None, "started_at": run.started_at if run else None,
                        "ended_at": run.ended_at if run else None,
                        "tokens": (run.tokens_in + run.tokens_out) if run else 0}
        return {"id": job.id, "name": job.name, "agent": job.agent_slug,
                "agent_name": agent.display_name if agent else job.agent_slug,
                "agent_active": bool(agent and agent.status == "active"),
                "model": TIER_MODEL[agent.model_tier] if agent else None,
                "prompt": job.prompt, "kind": job.schedule_kind, "expr": job.schedule_expr,
                "timezone": job.timezone, "active": job.is_active, "approved": job.approval_id is not None,
                "next_run_at": job.next_run_at, "last_run_at": job.last_run_at, "last": last}

    def list(self) -> list[dict[str, Any]]:
        with self.db.read() as u:
            agents = {a.slug: a for a in u.agents.list()}
            return [self._item(u, job, agents) for job in u.scheduled_jobs.list()]

    def get(self, job_id: int) -> dict[str, Any]:
        with self.db.read() as u:
            agents = {a.slug: a for a in u.agents.list()}
            try:
                return self._item(u, u.scheduled_jobs.get(job_id), agents)
            except NotFound as exc:
                raise AgentsError("job_not_found", 404, str(exc)) from exc

    # ------------------------------------------------------------------ validation

    @staticmethod
    def _fields(name: str, agent: str, prompt: str, kind: str, expr: str, tz: str) -> dict[str, str]:
        clean = clean_name(name)
        text = clean_text(prompt, MAX_PROMPT)
        if not clean:
            raise AgentsError("name_invalid", 422)
        if len(clean_text(prompt, MAX_PROMPT + 1)) > MAX_PROMPT:
            raise AgentsError("prompt_too_long", 422)
        if not text:
            raise AgentsError("prompt_required", 422)
        try:
            return {"name": clean, "agent_slug": agent, "prompt": text, "schedule_kind": kind,
                    "schedule_expr": schedule.normalize(kind, expr, tz), "timezone": tz}
        except schedule.ScheduleError as exc:
            raise _schedule_error(exc) from exc

    # ------------------------------------------------------------------ changes

    def create(self, name: str, agent: str, prompt: str, kind: str, expr: str, tz: str) -> dict[str, Any]:
        values = self._fields(name, agent, prompt, kind, expr, tz)
        try:
            with self.db.write() as u:
                if u.agents.find(agent) is None:
                    raise AgentsError("agent_not_found", 404)
                job = u.scheduled_jobs.create(values["name"], agent, values["prompt"], kind, values["schedule_expr"],
                                              tz, is_active=False)
        except IntegrityViolation as exc:
            raise AgentsError("job_name_taken", 409, str(exc)) from exc
        except DbError as exc:
            raise AgentsError("job_invalid", 422, str(exc)) from exc
        return self.get(job.id)

    def update(self, job_id: int, name: str, agent: str, prompt: str, kind: str, expr: str, tz: str) -> dict[str, Any]:
        values = self._fields(name, agent, prompt, kind, expr, tz)
        try:
            with self.db.write() as u:
                try:
                    current = u.scheduled_jobs.get(job_id)
                except NotFound as exc:
                    raise AgentsError("job_not_found", 404, str(exc)) from exc
                if u.agents.find(agent) is None:
                    raise AgentsError("agent_not_found", 404)
                u.scheduled_jobs.update(job_id, **values)
                changed = (current.agent_slug, current.prompt, current.schedule_kind, current.schedule_expr,
                           current.timezone) != (agent, values["prompt"], kind, values["schedule_expr"], tz)
                if changed and current.is_active:              # what the owner approved is no longer what will run
                    u.scheduled_jobs.set_active(job_id, False)
        except IntegrityViolation as exc:
            raise AgentsError("job_name_taken", 409, str(exc)) from exc
        except AgentsError:
            raise
        except DbError as exc:
            raise AgentsError("job_invalid", 422, str(exc)) from exc
        return self.get(job_id)

    def delete(self, job_id: int) -> None:
        try:
            with self.db.write() as u:
                u.scheduled_jobs.get(job_id)
                u.scheduled_jobs.delete(job_id)
        except NotFound as exc:
            raise AgentsError("job_not_found", 404, str(exc)) from exc
        except DbError as exc:
            raise AgentsError("job_invalid", 409, str(exc)) from exc

    def activate(self, job_id: int, *, confirmed: bool, now: datetime | None = None) -> dict[str, Any]:
        """Turning a job on is the owner's act: it needs the explicit confirmation and leaves an approval row."""
        if confirmed is not True:
            raise AgentsError("confirmation_required", 422)
        now = now or datetime.now(timezone.utc)
        try:
            with self.db.write() as u:
                try:
                    job = u.scheduled_jobs.get(job_id)
                except NotFound as exc:
                    raise AgentsError("job_not_found", 404, str(exc)) from exc
                agent = u.agents.find(job.agent_slug)
                if agent is None or agent.status != "active":
                    raise AgentsError("agent_off", 409)
                try:
                    following = schedule.next_run(job.schedule_kind, job.schedule_expr, job.timezone, now)
                except schedule.ScheduleError as exc:
                    raise _schedule_error(exc) from exc
                if following is None:
                    raise AgentsError("run_in_past", 422)
                owner = u.profile.get_or_default().owner_name or "owner"
                approval = u.approvals.record("scheduled_job", job_id, "approved", decided_by=owner,
                                              channel="dashboard", note=job.name)
                u.scheduled_jobs.update(job_id, next_run_at=following)
                u.scheduled_jobs.set_active(job_id, True, approval_id=approval.id)
        except AgentsError:
            raise
        except DbError as exc:
            raise AgentsError("job_invalid", 422, str(exc)) from exc
        return self.get(job_id)

    def deactivate(self, job_id: int) -> dict[str, Any]:
        try:
            with self.db.write() as u:
                u.scheduled_jobs.get(job_id)
                u.scheduled_jobs.set_active(job_id, False)
        except NotFound as exc:
            raise AgentsError("job_not_found", 404, str(exc)) from exc
        return self.get(job_id)


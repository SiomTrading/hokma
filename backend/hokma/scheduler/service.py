"""Scheduled jobs for the panel: list, create, edit, turn on (with the owner's confirmation) and off."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from hokma.agents.files import TIER_MODEL, clean_name, clean_text
from hokma.agents.service import AgentsError
from hokma.db import Database, DbError, IntegrityViolation, NotFound, get_database
from hokma.scheduler import schedule

_UTC = timezone.utc
MAX_PROMPT = 4000
MAX_DESK_RUNS = 10               # scheduled runs one desk may have at a time (SPEC-018: OA-07)
DESK_RUN_PREFIX = "desk-{desk_id}-run-"
BRIEFING_NAME = "Premarket briefing"
BRIEFING_SCHEDULE = ("weekly", "mon-fri 08:00")
MAX_NAME_INPUT = 200
SKIP_SCAN = 300          # newest journal notes read to find the last skipped run of each routine


def last_skips(u) -> dict[int, dict[str, Any]]:
    """Bug 1136: the newest skipped run of each routine, read from the journal (no new table): newest first, so the
    first note seen for a job wins."""
    found: dict[int, dict[str, Any]] = {}
    for entry in u.journal.list(kind="note", limit=SKIP_SCAN):
        refs = entry.refs if isinstance(entry.refs, dict) else {}
        job_id = refs.get("scheduled_job")
        if entry.outcome != "partial" or not isinstance(job_id, int) or job_id in found:
            continue
        found[job_id] = {"at": entry.created_at, "planned": refs.get("planned"), "reason": str(refs.get("reason") or "")}
    return found


def _late_planned(u, task_id: int) -> str | None:
    """When the last run of a routine went out late (Hokma was closed), the time it should have run: the journal
    note of that run carries it as `late_planned`."""
    for entry in u.journal.list(task_id=task_id, limit=5):
        refs = entry.refs if isinstance(entry.refs, dict) else {}
        if refs.get("late_planned"):
            return str(refs["late_planned"])
    return None


def _schedule_error(exc: schedule.ScheduleError) -> AgentsError:
    return AgentsError(exc.code, 422, str(exc))


class JobsService:
    def __init__(self, db: Database | None = None, running: Callable[[], Iterable[int]] | None = None) -> None:
        self._db = db
        self._running = running

    def _refuse_if_running(self, job_id: int) -> None:
        """Bug 1140: a run in progress cannot be edited, turned off or deleted under its feet."""
        from hokma.agents.api import current_scheduler

        if self._running is not None:
            ids = self._running()
        else:
            active = current_scheduler()
            ids = active.running_ids() if active else []
        if job_id in ids:
            raise AgentsError("job_running", 409)

    @property
    def db(self) -> Database:
        return self._db or get_database()

    # ------------------------------------------------------------------ reading

    def _item(self, u, job, agents: dict[str, Any], skips: dict[int, dict[str, Any]] | None = None) -> dict[str, Any]:
        agent = agents.get(job.agent_slug)
        skip = (skips if skips is not None else last_skips(u)).get(job.id)
        if skip is not None and job.last_run_at and skip["at"] <= job.last_run_at:
            skip = None                                  # a later run happened: the skip is old news
        last = None
        if job.last_task_id is not None:
            task = u.tasks.find(job.last_task_id)
            runs = u.task_runs.list(job.last_task_id)
            run = runs[-1] if runs else None
            if task is not None:
                last = {"task_id": task.id, "status": task.status, "summary": (task.result_summary or "")[:2000],
                        "error": run.error if run else None, "started_at": run.started_at if run else None,
                        "ended_at": run.ended_at if run else None,
                        "tokens": (run.tokens_in + run.tokens_out) if run else 0,
                        "denied": int((task.result_metadata or {}).get("denied") or 0)
                        if isinstance(task.result_metadata, dict) else 0,
                        "late_planned": _late_planned(u, job.last_task_id)}
        return {"id": job.id, "name": job.name, "agent": job.agent_slug,
                "agent_name": agent.display_name if agent else job.agent_slug,
                "agent_active": bool(agent and agent.status == "active"),
                "model": TIER_MODEL[agent.model_tier] if agent else None,
                "prompt": job.prompt, "kind": job.schedule_kind, "expr": job.schedule_expr,
                "timezone": job.timezone, "active": job.is_active, "approved": job.approval_id is not None,
                "next_run_at": job.next_run_at, "last_run_at": job.last_run_at, "last": last, "last_skip": skip}

    def list(self) -> list[dict[str, Any]]:
        with self.db.read() as u:
            agents = {a.slug: a for a in u.agents.list()}
            skips = last_skips(u)
            return [self._item(u, job, agents, skips) for job in u.scheduled_jobs.list()]

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
        if isinstance(name, str) and len(name) > MAX_NAME_INPUT:        # bug 1140: a clear error, not pydantic's array
            raise AgentsError("name_too_long", 422)
        clean = clean_name(name)
        text = clean_text(prompt, MAX_PROMPT)
        if not clean:
            raise AgentsError("name_invalid", 422)
        if len(clean_text(prompt, MAX_PROMPT + 1)) > MAX_PROMPT:
            raise AgentsError("prompt_too_long", 422)
        if not text:
            raise AgentsError("prompt_required", 422)
        try:
            return {"name": clean, "agent_slug": agent, "prompt": text, "schedule_kind": schedule.stored_kind(kind),
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
                job = u.scheduled_jobs.create(values["name"], agent, values["prompt"], values["schedule_kind"],
                                              values["schedule_expr"], tz, is_active=False)
        except IntegrityViolation as exc:
            raise AgentsError("job_name_taken", 409, str(exc)) from exc
        except DbError as exc:
            raise AgentsError("job_invalid", 422, str(exc)) from exc
        return self.get(job.id)

    def ensure_template(self) -> dict[str, Any]:
        """The premarket briefing routine: created off, Monday to Friday 08:00 in the owner's zone, Hokma as agent.
        If one with that name already exists it is returned as is (never duplicated, never edited, even when its prompt
        differs: the owner or an agent made it). Either way it becomes the routine the Today card reads, unless the card
        already points to a routine that exists."""
        from hokma.kit.integrity import app_root

        prompt = (app_root() / "kit" / "template" / "routines" / "premarket_briefing.md").read_text(encoding="utf-8")
        with self.db.read() as u:
            found = next((j for j in u.scheduled_jobs.list() if j.name == BRIEFING_NAME), None)
            tz = u.profile.get_or_default().timezone
        job = self.get(found.id) if found is not None else self.create(BRIEFING_NAME, "hokma", prompt, *BRIEFING_SCHEDULE, tz)
        self._link_briefing(job["id"])
        return job

    def _link_briefing(self, job_id: int) -> None:
        """Save `job_id` as the briefing routine of the Today card when it has none or its routine is gone."""
        from hokma.db import NotFound
        from hokma.today.service import change_config, get_config

        current = get_config(self.db)["briefing_job_id"]
        if current is not None:
            try:
                with self.db.read() as u:
                    u.scheduled_jobs.get(current)
                return
            except NotFound:
                pass
        change_config({"briefing_job_id": job_id}, self.db)

    def run_now(self, job_id: int) -> dict[str, Any]:
        """Run a routine the owner already approved without waiting for its hour. The next planned run does not move."""
        from hokma.agents.api import current_scheduler

        with self.db.read() as u:
            try:
                job = u.scheduled_jobs.get(job_id)
            except NotFound as exc:
                raise AgentsError("job_not_found", 404, str(exc)) from exc
            agent = u.agents.find(job.agent_slug)
        if job.approval_id is None:
            raise AgentsError("job_not_approved", 409)
        if agent is None or agent.status != "active":
            raise AgentsError("agent_off", 409)
        self._refuse_if_running(job_id)
        active = current_scheduler()
        if active is None:
            raise AgentsError("scheduler_off", 409)
        if not active.run_now(job):
            raise AgentsError("job_running", 409)
        return self.get(job_id)

    def update(self, job_id: int, name: str, agent: str, prompt: str, kind: str, expr: str, tz: str) -> dict[str, Any]:
        values = self._fields(name, agent, prompt, kind, expr, tz)
        self._refuse_if_running(job_id)
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
                           current.timezone) != (agent, values["prompt"], values["schedule_kind"], values["schedule_expr"], tz)
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
        self._refuse_if_running(job_id)
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
        self._refuse_if_running(job_id)
        try:
            with self.db.write() as u:
                u.scheduled_jobs.get(job_id)
                u.scheduled_jobs.set_active(job_id, False)
        except NotFound as exc:
            raise AgentsError("job_not_found", 404, str(exc)) from exc
        return self.get(job_id)



# ---------------------------------------------------------------------------------------------- runs of one desk (OA-07)

def _desk_run_prefix(desk_id: int) -> str:
    return DESK_RUN_PREFIX.format(desk_id=int(desk_id))


def _desk_member(u, desk_id: int, who: Any) -> str:
    """The slug that owns the run: the asking agent when it works on this desk, or the chief of the desk when the owner asks.
    Everything else is refused: the desk comes from the database, never from what the caller says about itself."""
    if getattr(who, "owner", False) is True:
        chief = u.desks.chief(desk_id)
        if chief is None:
            raise AgentsError("desk_has_no_chief", 409)
        return chief.agent_slug
    slug = getattr(who, "agent", None)
    member = u.desks.find_agent(desk_id, slug) if isinstance(slug, str) and slug else None
    if member is None or not member.active:
        raise AgentsError("not_desk_agent", 403)
    return slug


def schedule_desk_run(desk_id: int, *, expr: str, instruction: str, who: Any, db: Database | None = None,
                      timezone: str | None = None) -> dict[str, Any]:
    """Program a run of ONE desk: an agent of that desk, with an instruction, at a time (OA-07). Stable signature; the tool
    `schedule_run` of `kit/exec_tools.py` calls it.

    `expr`: `<kind> <expr> [tz=<IANA zone>]` with kind `at`, `every`, `cron`, `once`, `daily`, `weekly` or `interval` (see
    `hokma.scheduler.schedule`), for example `cron 30 9 * * mon-fri tz=America/New_York`. The zone is the `tz=` of the text, else
    `timezone`, else the owner's, else UTC. `who`: a `hokma.broker.service.Who`; an agent may program only the desk it belongs to
    (a desk it does not belong to is `not_desk_agent`), the owner any desk (the run is of the chief of the desk).

    The job is saved OFF and without an approval, like every job an agent creates: it runs only after the owner turns it on in the
    panel. The answer says so. Errors are `AgentsError` with a stable code: desk_not_found, not_desk_agent, desk_has_no_chief,
    too_many_runs, job_name_taken (the same run twice), prompt_required, prompt_too_long, run_in_past and the schedule codes
    (schedule_invalid, timezone_invalid, interval_out_of_range, kind_invalid)."""
    import hashlib

    database = db or get_database()
    text = clean_text(instruction, MAX_PROMPT)
    if not text:
        raise AgentsError("prompt_required", 422)
    if len(clean_text(instruction, MAX_PROMPT + 1)) > MAX_PROMPT - 200:           # room for the line that names the desk
        raise AgentsError("prompt_too_long", 422)
    try:
        with database.write() as u:
            desk = u.desks.find(int(desk_id)) if isinstance(desk_id, int) and not isinstance(desk_id, bool) else None
            if desk is None or desk.status == "archived":
                raise AgentsError("desk_not_found", 404)
            slug = _desk_member(u, desk.id, who)
            default_tz = timezone or u.profile.get_or_default().timezone or "UTC"
            try:
                parsed = schedule.parse_expression(expr, default_tz)
                first = schedule.next_run(parsed.kind, parsed.expr, parsed.timezone, datetime.now(_UTC))
            except schedule.ScheduleError as exc:
                raise _schedule_error(exc) from exc
            if first is None:
                raise AgentsError("run_in_past", 422)
            prefix = _desk_run_prefix(desk.id)
            if sum(1 for j in u.scheduled_jobs.list() if j.name.startswith(prefix)) >= MAX_DESK_RUNS:
                raise AgentsError("too_many_runs", 409)
            digest = hashlib.sha256(f"{slug}|{parsed.kind}|{parsed.expr}|{parsed.timezone}|{text}".encode()).hexdigest()[:10]
            body = f"Scheduled run of desk #{desk.id} ({desk.name}). Work only on this desk.\n\n{text}"
            job = u.scheduled_jobs.create(f"{prefix}{digest}", slug, body, parsed.kind, parsed.expr, parsed.timezone, is_active=False)
    except IntegrityViolation as exc:
        raise AgentsError("job_name_taken", 409, str(exc)) from exc
    except AgentsError:
        raise
    except DbError as exc:
        raise AgentsError("job_invalid", 422, str(exc)) from exc
    return {"job_id": job.id, "name": job.name, "desk_id": desk.id, "agent": slug, "kind": parsed.kind, "expr": parsed.expr,
            "timezone": parsed.timezone, "active": False, "first_run_at": first.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "note": "saved off: it runs only after the owner turns it on"}


def list_desk_runs(desk_id: int, db: Database | None = None) -> list[dict[str, Any]]:
    """The scheduled runs of one desk, for the owner: the jobs saved by `schedule_desk_run` whose agent still works on the desk.
    Read only."""
    database = db or get_database()
    prefix = _desk_run_prefix(desk_id)
    out: list[dict[str, Any]] = []
    with database.read() as u:
        members = {m.agent_slug for m in u.desks.list_agents(desk_id)}
        for job in u.scheduled_jobs.list():
            if not job.name.startswith(prefix) or job.agent_slug not in members:
                continue
            status = None
            if job.last_task_id is not None:
                task = u.tasks.find(job.last_task_id)
                status = task.status if task is not None else None
            out.append({"job_id": job.id, "name": job.name, "agent": job.agent_slug, "instruction": job.prompt.split("\n\n", 1)[-1],
                        "kind": job.schedule_kind, "expr": job.schedule_expr, "timezone": job.timezone, "active": job.is_active,
                        "approved": job.approval_id is not None, "next_run_at": job.next_run_at, "last_run_at": job.last_run_at,
                        "last_status": status})
    return sorted(out, key=lambda r: r["job_id"])

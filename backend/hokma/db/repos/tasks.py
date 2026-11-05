"""tasks, task_links, task_runs, task_events, task_comments."""
from __future__ import annotations

import secrets
from typing import Any, Collection, Sequence

from hokma.db.errors import Conflict, NotFound, RuleViolation, ValidationError
from hokma.db.models import (Actor, Task, TaskComment, TaskDetail, TaskEvent, TaskRun, dumps_json)
from hokma.db.repos.approvals import ApprovalsRepo
from hokma.db.repos.base import UNSET, Repo, flag, given, require_in
from hokma.db.timeutil import to_db_time, utc_in, utcnow

CREATE_STATUSES = ("triage", "todo", "ready")
TERMINAL = ("done", "failed", "archived")
RUN_OUTCOMES = ("running", "completed", "review_requested", "approved_by_reviewer", "changes_requested", "blocked",
                "failure", "timed_out", "crashed", "released")
NEEDS_SUMMARY = ("completed", "review_requested", "approved_by_reviewer", "changes_requested", "blocked")
BLOCK_KINDS = ("dependency", "needs_input", "capability", "transient")
_CLAIM_CLEAR = {"claim_token": None, "claimed_by": None, "claimed_at": None, "claim_expires_at": None}
_OPEN_PARENT = ("EXISTS (SELECT 1 FROM task_links l JOIN tasks p ON p.id = l.parent_id "
                "WHERE l.child_id = {t}.id AND p.status <> 'done')")


def _created_by_kind(actor: Actor) -> str:
    return "scheduler" if actor.kind == "system" else actor.kind


def _limit_text(name: str, value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    if len(value) > limit:
        raise ValidationError(f"{name} is at most {limit} characters")
    return value


class TaskRunsRepo(Repo):
    def start(self, task_id: int, agent_slug: str, *, role: str = "implement", model: str,
              pid: int | None = None) -> TaskRun:
        require_in("role", role, ("implement", "review"))
        if not model:
            raise ValidationError("model is required")
        now = utcnow()
        new_id = self.s.insert("task_runs", {"task_id": task_id, "agent_slug": agent_slug, "role": role,
                                             "model": model, "pid": pid, "started_at": now, "heartbeat_at": now})
        return self.get(new_id)

    def get(self, run_id: int) -> TaskRun:
        return self._get("task_runs", "id", run_id, TaskRun)

    def heartbeat(self, run_id: int) -> None:
        self.s.update("task_runs", {"id": run_id}, {"heartbeat_at": utcnow()})

    def set_pid(self, run_id: int, pid: int | None) -> None:
        self.s.update("task_runs", {"id": run_id}, {"pid": pid})

    def finish(self, run_id: int, outcome: str, *, summary: str | None = None, metadata: Any = None,
               error: str | None = None, tokens_in: int | None = None, tokens_out: int | None = None) -> TaskRun:
        require_in("outcome", outcome, RUN_OUTCOMES)
        if outcome == "running":
            raise ValidationError("a finished run cannot have outcome running")
        if outcome in NEEDS_SUMMARY and not summary:
            raise ValidationError(f"outcome {outcome} needs a summary")
        values: dict[str, Any] = {"outcome": outcome, "ended_at": utcnow(),
                                  "summary": _limit_text("summary", summary, 4000),
                                  "metadata": _limit_text("metadata", dumps_json(metadata), 16000),
                                  "error": _limit_text("error", error, 2000)}
        for name, value in (("tokens_in", tokens_in), ("tokens_out", tokens_out)):
            if value is not None:
                values[name] = value
        self.s.update("task_runs", {"id": run_id}, values)
        return self.get(run_id)

    def add_tokens(self, run_id: int, tokens_in: int, tokens_out: int) -> None:
        if tokens_in < 0 or tokens_out < 0:
            raise ValidationError("tokens cannot be negative")
        self.s.execute("UPDATE task_runs SET tokens_in = tokens_in + :a, tokens_out = tokens_out + :b WHERE id = :id",
                       {"a": tokens_in, "b": tokens_out, "id": run_id})

    def current(self, task_id: int) -> TaskRun | None:
        row = self.s.one("SELECT * FROM task_runs WHERE task_id = :t AND outcome = 'running'", {"t": task_id})
        return TaskRun.from_row(row) if row else None

    def list(self, task_id: int) -> list[TaskRun]:
        rows = self.s.query("SELECT * FROM task_runs WHERE task_id = :t ORDER BY id", {"t": task_id})
        return [TaskRun.from_row(r) for r in rows]


class TaskEventsRepo(Repo):
    """Read-only: the triggers write the history."""

    def list(self, task_id: int) -> list[TaskEvent]:
        rows = self.s.query("SELECT * FROM task_events WHERE task_id = :t ORDER BY id", {"t": task_id})
        return [TaskEvent.from_row(r) for r in rows]

    def recent(self, limit: int = 100) -> list[TaskEvent]:
        rows = self.s.query("SELECT * FROM task_events ORDER BY id DESC LIMIT :n", {"n": limit})
        return [TaskEvent.from_row(r) for r in rows]


class TaskCommentsRepo(Repo):
    def add(self, task_id: int, body: str, *, actor: Actor) -> TaskComment:
        text = (body or "").strip()
        if not 1 <= len(text) <= 2000:
            raise ValidationError("a comment has 1 to 2000 characters")
        new_id = self.s.insert("task_comments", {"task_id": task_id, "author_kind": actor.kind,
                                                 "author": actor.name, "body": text, "created_at": utcnow()})
        return self._get("task_comments", "id", new_id, TaskComment)

    def list(self, task_id: int) -> list[TaskComment]:
        rows = self.s.query("SELECT * FROM task_comments WHERE task_id = :t ORDER BY id", {"t": task_id})
        return [TaskComment.from_row(r) for r in rows]


class TaskLinksRepo(Repo):
    def parents(self, child_id: int) -> list[int]:
        rows = self.s.query("SELECT parent_id FROM task_links WHERE child_id = :c ORDER BY parent_id", {"c": child_id})
        return [r["parent_id"] for r in rows]

    def children(self, parent_id: int) -> list[int]:
        rows = self.s.query("SELECT child_id FROM task_links WHERE parent_id = :p ORDER BY child_id", {"p": parent_id})
        return [r["child_id"] for r in rows]

    def _descendants(self, task_id: int) -> set[int]:
        seen: set[int] = set()
        stack = [task_id]
        while stack:
            for child in self.children(stack.pop()):
                if child not in seen:
                    seen.add(child)
                    stack.append(child)
        return seen

    def add(self, parent_id: int, child_id: int) -> None:
        if parent_id == child_id:
            raise ValidationError("a task cannot depend on itself")
        parent = self._get("tasks", "id", parent_id, _StatusOnly)
        child = self._get("tasks", "id", child_id, _StatusOnly)
        if parent_id in self._descendants(child_id):
            raise ValidationError("this link would create a cycle")
        if parent_id in self.parents(child_id):
            return
        self.s.insert_row("task_links", {"parent_id": parent_id, "child_id": child_id})
        if child.status == "ready" and parent.status != "done":      # not ready any more: it waits for the parent
            TasksRepo(self.s).transition(child_id, "todo", actor=Actor.system(), reason="waiting for a new parent")

    def remove(self, parent_id: int, child_id: int) -> None:
        self.s.execute("DELETE FROM task_links WHERE parent_id = :p AND child_id = :c", {"p": parent_id, "c": child_id})


class _StatusOnly:
    """Minimal row for existence checks."""

    def __init__(self, status: str) -> None:
        self.status = status

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "_StatusOnly":
        return cls(row["status"])


class TasksRepo(Repo):
    # ---------------------------------------------------------------- reading
    def _hydrate(self, rows: list[dict[str, Any]]) -> list[Task]:
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        marks = ", ".join(f":i{n}" for n in range(len(ids)))
        links = self.s.query(f"SELECT child_id, parent_id FROM task_links WHERE child_id IN ({marks}) ORDER BY parent_id",
                             {f"i{n}": v for n, v in enumerate(ids)})
        parents: dict[int, list[int]] = {}
        for link in links:
            parents.setdefault(link["child_id"], []).append(link["parent_id"])
        return [Task.from_row({**r, "parent_ids": tuple(parents.get(r["id"], ()))}) for r in rows]

    def get(self, id: int) -> Task:
        found = self.find(id)
        if found is None:
            raise NotFound(f"tasks {id!r} not found")
        return found

    def find(self, id: int) -> Task | None:
        rows = self._hydrate(self.s.query("SELECT * FROM tasks WHERE id = :id", {"id": id}))
        return rows[0] if rows else None

    def detail(self, id: int) -> TaskDetail:
        task = self.get(id)
        links = TaskLinksRepo(self.s)
        return TaskDetail(task=task, parents=[self.get(p) for p in links.parents(id)],
                          children=[self.get(c) for c in links.children(id)], runs=TaskRunsRepo(self.s).list(id),
                          comments=TaskCommentsRepo(self.s).list(id), events=TaskEventsRepo(self.s).list(id))

    def list(self, *, status: str | None = None, statuses: Collection[str] | None = None,
             assigned_agent: str | None = None, project_id: int | None = None, created_by_kind: str | None = None,
             limit: int = 200, offset: int = 0) -> list[Task]:
        sql, params = "SELECT * FROM tasks WHERE 1 = 1", {"limit": limit, "offset": offset}
        if status:
            sql += " AND status = :status"
            params["status"] = status
        if statuses:
            names = []
            for n, value in enumerate(statuses):
                params[f"s{n}"] = value
                names.append(f":s{n}")
            sql += f" AND status IN ({', '.join(names)})"
        for column, value in (("assigned_agent", assigned_agent), ("project_id", project_id),
                              ("created_by_kind", created_by_kind)):
            if value is not None:
                sql += f" AND {column} = :{column}"
                params[column] = value
        sql += " ORDER BY priority DESC, created_at DESC, id DESC LIMIT :limit OFFSET :offset"
        return self._hydrate(self.s.query(sql, params))

    def running(self) -> list[Task]:
        return self._hydrate(self.s.query("SELECT * FROM tasks WHERE status = 'running' ORDER BY id"))

    # ---------------------------------------------------------------- creating and editing
    def create(self, title: str, *, actor: Actor, body: str | None = None, project_id: int | None = None,
               status: str = "ready", priority: int = 0, assigned_agent: str | None = None,
               requires_review: bool | None = None, reviewer_agent: str | None = None,
               requires_approval: bool | None = None, touches_real_money: bool = False,
               parents: Sequence[int] = (), run_at: Any = None, max_runtime_seconds: int = 1800,
               max_attempts: int = 2) -> Task:
        title = (title or "").strip()
        if not 1 <= len(title) <= 200:
            raise ValidationError("title must have 1 to 200 characters")
        _limit_text("body", body, 8000)
        require_in("status", status, CREATE_STATUSES)
        if not -2 <= priority <= 2:
            raise ValidationError("priority is between -2 and 2")
        review, approval, reviewer = requires_review, requires_approval, reviewer_agent
        if project_id is not None:
            project = self.s.one("SELECT * FROM projects WHERE id = :id", {"id": project_id})
            if project is None:
                raise NotFound(f"projects {project_id!r} not found")
            if review is None:
                review = bool(project["default_requires_review"])
            if approval is None:
                approval = bool(project["default_requires_approval"])
            if reviewer is None:
                reviewer = project["default_reviewer_agent"]
        review, approval = bool(review), bool(approval)
        if touches_real_money:
            approval = True
        if review and not reviewer:
            raise ValidationError("a reviewer is required when review is on")
        if reviewer and reviewer == assigned_agent:
            raise ValidationError("the reviewer must be a different agent from the implementer")
        parents = list(dict.fromkeys(parents))
        open_parent = False
        for parent_id in parents:
            found = self.s.one("SELECT status FROM tasks WHERE id = :id", {"id": parent_id})
            if found is None:
                raise NotFound(f"tasks {parent_id!r} not found")
            open_parent = open_parent or found["status"] != "done"
        run_time = to_db_time(run_at)
        if status == "ready" and (open_parent or (run_time and run_time > utcnow())):
            status = "todo"
        new_id = self.s.insert("tasks", {
            "project_id": project_id, "title": title, "body": body, "status": status, "priority": priority,
            "assigned_agent": assigned_agent, "requires_review": flag(review), "reviewer_agent": reviewer,
            "requires_approval": flag(approval), "touches_real_money": flag(touches_real_money),
            "created_by_kind": _created_by_kind(actor), "created_by": actor.name,
            "changed_by_kind": actor.kind, "changed_by": actor.name, "run_at": run_time,
            "max_runtime_seconds": max_runtime_seconds, "max_attempts": max_attempts,
            "created_at": utcnow(), "updated_at": utcnow()})
        for parent_id in parents:
            self.s.insert_row("task_links", {"parent_id": parent_id, "child_id": new_id})
        return self.get(new_id)

    def update(self, id: int, *, actor: Actor, title: Any = UNSET, body: Any = UNSET, priority: Any = UNSET,
               assigned_agent: Any = UNSET, project_id: Any = UNSET, run_at: Any = UNSET, requires_review: Any = UNSET,
               reviewer_agent: Any = UNSET, requires_approval: Any = UNSET, max_runtime_seconds: Any = UNSET,
               max_attempts: Any = UNSET) -> Task:
        current = self.get(id)
        values = given(title=title, body=body, priority=priority, assigned_agent=assigned_agent, project_id=project_id,
                       run_at=run_at, requires_review=requires_review, reviewer_agent=reviewer_agent,
                       requires_approval=requires_approval, max_runtime_seconds=max_runtime_seconds,
                       max_attempts=max_attempts)
        if "title" in values and not 1 <= len((values["title"] or "").strip()) <= 200:
            raise ValidationError("title must have 1 to 200 characters")
        if "body" in values:
            _limit_text("body", values["body"], 8000)
        if "priority" in values and not -2 <= values["priority"] <= 2:
            raise ValidationError("priority is between -2 and 2")
        if "run_at" in values:
            values["run_at"] = to_db_time(values["run_at"])
        for key in ("requires_review", "requires_approval"):
            if key in values:
                values[key] = flag(values[key])
        if values.get("requires_review") and not values.get("reviewer_agent", current.reviewer_agent):
            raise ValidationError("a reviewer is required when review is on")
        if not values:
            return current
        values.update(changed_by_kind=actor.kind, changed_by=actor.name)
        if current.status != "awaiting_approval":      # an approval must postdate the entry into awaiting_approval
            values["updated_at"] = utcnow()
        self.s.update("tasks", {"id": id}, values)
        return self.get(id)

    # ---------------------------------------------------------------- low-level transition
    def _open_run(self, task_id: int):
        return TaskRunsRepo(self.s).current(task_id)

    def _close_run(self, task_id: int, outcome: str, *, summary: str | None = None, error: str | None = None,
                   metadata: Any = None) -> None:
        run = self._open_run(task_id)
        if run is not None:
            TaskRunsRepo(self.s).finish(run.id, outcome, summary=summary or (outcome if outcome in NEEDS_SUMMARY else None),
                                        error=error, metadata=metadata)

    def transition(self, id: int, to_status: str, *, actor: Actor, reason: str | None = None,
                   **fields: Any) -> Task:
        current = self.get(id)
        if current.status == to_status:
            return current
        values: dict[str, Any] = {"status": to_status, "changed_by_kind": actor.kind, "changed_by": actor.name,
                                  "change_reason": _limit_text("reason", reason, 500)}
        if to_status != "running":
            values.update(_CLAIM_CLEAR)
        if to_status != "blocked":
            values.update(block_kind=None, block_reason=None)
        if to_status in TERMINAL:
            values["completed_at"] = current.completed_at if current.status in TERMINAL and current.completed_at else utcnow()
        elif current.status in TERMINAL:
            values["completed_at"] = None
        values.update(fields)
        sets = ", ".join(f"{c} = :v_{c}" for c in values)
        params = {f"v_{c}": v for c, v in values.items()} | {"id": id, "from": current.status}
        for column in values:
            self.s._check_name(column)
        changed = self.s.execute(f"UPDATE tasks SET {sets} WHERE id = :id AND status = :from", params)
        if changed == 0:
            raise Conflict(f"task {id} changed while it was being updated")
        return self.get(id)

    # ---------------------------------------------------------------- dispatcher
    def promote_due(self, now: Any = None) -> list[int]:
        stamp = to_db_time(now) or utcnow()
        rows = self.s.query(
            "SELECT id FROM tasks WHERE status = 'todo' AND (run_at IS NULL OR run_at <= :now) AND NOT "
            + _OPEN_PARENT.format(t="tasks") + " ORDER BY priority DESC, created_at, id", {"now": stamp})
        promoted = []
        for row in rows:
            self.transition(row["id"], "ready", actor=Actor.system(), reason="dependencies done and time reached")
            promoted.append(row["id"])
        return promoted

    def _lane(self, status: str, agent_column: str, agents: Collection[str] | None, only_active: bool,
              limit: int) -> list[Task]:
        sql = ("SELECT t.* FROM tasks t WHERE t.status = :status AND t.claim_token IS NULL "
               f"AND t.{agent_column} IS NOT NULL")
        params: dict[str, Any] = {"status": status, "limit": limit}
        if only_active:
            sql += f" AND EXISTS (SELECT 1 FROM agents a WHERE a.slug = t.{agent_column} AND a.status = 'active')"
        if agents is not None:
            names = []
            for n, slug in enumerate(agents):
                params[f"a{n}"] = slug
                names.append(f":a{n}")
            if not names:
                return []
            sql += f" AND t.{agent_column} IN ({', '.join(names)})"
        if status == "ready":
            sql += " AND NOT " + _OPEN_PARENT.format(t="t")
        sql += " ORDER BY t.priority DESC, t.created_at ASC, t.id ASC LIMIT :limit"
        return self._hydrate(self.s.query(sql, params))

    def list_ready(self, *, agents: Collection[str] | None = None, only_active: bool = True,
                   limit: int = 50) -> list[Task]:
        return self._lane("ready", "assigned_agent", agents, only_active, limit)

    def list_reviewable(self, *, only_active: bool = True, limit: int = 50) -> list[Task]:
        return self._lane("review", "reviewer_agent", None, only_active, limit)

    def claim(self, id: int, agent: str, *, actor: Actor | None = None, ttl_seconds: int | None = None,
              run_model: str | None = None) -> Task:
        actor = actor or Actor.agent(agent)
        current = self.get(id)
        ttl = ttl_seconds or current.max_runtime_seconds
        changed = self.s.execute(
            "UPDATE tasks SET status = 'running', claim_token = :token, claimed_by = :agent, claimed_at = :now, "
            "claim_expires_at = :expires, heartbeat_at = :now, attempts = attempts + 1, "
            "implementer_agent = COALESCE(implementer_agent, :agent), changed_by_kind = :kind, changed_by = :actor, "
            "change_reason = NULL, block_kind = NULL, block_reason = NULL "
            "WHERE id = :id AND status = 'ready' AND claim_token IS NULL",
            {"token": secrets.token_hex(16), "agent": agent, "now": utcnow(), "expires": utc_in(ttl),
             "kind": actor.kind, "actor": actor.name, "id": id})
        if changed == 0:
            raise Conflict(f"task {id} is not ready or was taken by someone else")
        if run_model:
            TaskRunsRepo(self.s).start(id, agent, role="implement", model=run_model)
        return self.get(id)

    def claim_next(self, agent: str, *, actor: Actor | None = None, run_model: str | None = None) -> Task | None:
        for candidate in self.list_ready(agents=[agent], only_active=False, limit=20):
            try:
                return self.claim(candidate.id, agent, actor=actor, run_model=run_model)
            except (Conflict, RuleViolation):
                continue
        return None

    def claim_review(self, id: int, reviewer: str, *, run_model: str | None = None) -> Task:
        changed = self.s.execute(
            "UPDATE tasks SET claim_token = :token, claimed_by = :agent, claimed_at = :now, claim_expires_at = :expires, "
            "heartbeat_at = :now WHERE id = :id AND status = 'review' AND claim_token IS NULL AND reviewer_agent = :agent",
            {"token": secrets.token_hex(16), "agent": reviewer, "now": utcnow(),
             "expires": utc_in(self.get(id).max_runtime_seconds), "id": id})
        if changed == 0:
            raise Conflict(f"task {id} is not waiting for review by {reviewer}")
        if run_model:
            TaskRunsRepo(self.s).start(id, reviewer, role="review", model=run_model)
        return self.get(id)

    def heartbeat(self, id: int, *, claim_token: str | None = None) -> None:
        sql = "UPDATE tasks SET heartbeat_at = :now WHERE id = :id AND claim_token IS NOT NULL"
        params: dict[str, Any] = {"now": utcnow(), "id": id}
        if claim_token is not None:
            sql += " AND claim_token = :token"
            params["token"] = claim_token
        if self.s.execute(sql, params) == 0:
            raise Conflict(f"task {id} has no matching claim")
        run = self._open_run(id)
        if run is not None:
            TaskRunsRepo(self.s).heartbeat(run.id)

    # ---------------------------------------------------------------- agent flow
    def complete(self, id: int, summary: str, *, actor: Actor, metadata: Any = None) -> Task:
        task = self.get(id)
        text = (summary or "").strip()
        if not text:
            raise ValidationError("a summary is required to complete a task")
        _limit_text("summary", text, 4000)
        meta = _limit_text("metadata", dumps_json(metadata), 16000)
        if task.requires_review:
            target, outcome = "review", "review_requested"
        elif task.requires_approval:
            target, outcome = "awaiting_approval", "completed"
        else:
            target, outcome = "done", "completed"
        result = self._finish_running(id, target, actor, text, meta)
        self._close_run(id, outcome, summary=text, metadata=meta)
        return result

    def _finish_running(self, id: int, target: str, actor: Actor, summary: str, meta: str | None) -> Task:
        return self.transition(id, target, actor=actor, result_summary=summary, result_metadata=meta)

    def request_review(self, id: int, summary: str, *, actor: Actor, metadata: Any = None) -> Task:
        text = (summary or "").strip()
        if not text:
            raise ValidationError("a summary is required to request a review")
        meta = _limit_text("metadata", dumps_json(metadata), 16000)
        result = self._finish_running(id, "review", actor, _limit_text("summary", text, 4000) or "", meta)
        self._close_run(id, "review_requested", summary=text, metadata=meta)
        return result

    def review_approve(self, id: int, *, actor: Actor, note: str | None = None) -> Task:
        task = self.get(id)
        if task.status != "review":
            raise RuleViolation(f"task {id} is not in review")
        if actor.kind == "human" and task.requires_approval:       # the owner's own review is the approval
            ApprovalsRepo(self.s).record("task", id, "approved", decided_by=actor.name, note=note)
            target = "done"
        else:
            target = "awaiting_approval" if task.requires_approval else "done"
        if note:
            TaskCommentsRepo(self.s).add(id, note, actor=actor)
        result = self.transition(id, target, actor=actor, reason=note)
        self._close_run(id, "approved_by_reviewer", summary=note or "approved")
        return result

    def request_changes(self, id: int, reason: str, *, actor: Actor) -> Task:
        if not (reason or "").strip():
            raise ValidationError("a reason is required to request changes")
        task = self.get(id)
        if task.status != "review":
            raise RuleViolation(f"task {id} is not in review")
        TaskCommentsRepo(self.s).add(id, reason, actor=actor)
        self._close_run(id, "changes_requested", summary=reason[:4000])
        if task.review_cycles >= task.review_limit:
            return self.to_triage(id, f"review limit reached: {reason}"[:500])
        return self.transition(id, "ready", actor=actor, reason=reason[:500], review_cycles=task.review_cycles + 1)

    def approve(self, id: int, *, decided_by: str, channel: str = "dashboard", note: str | None = None) -> Task:
        task = self.get(id)
        if task.status != "awaiting_approval":
            raise RuleViolation(f"task {id} is not awaiting approval")
        ApprovalsRepo(self.s).record("task", id, "approved", decided_by=decided_by, channel=channel, note=note)
        return self.transition(id, "done", actor=Actor.human(decided_by), reason=note)

    def reject(self, id: int, *, decided_by: str, channel: str = "dashboard", note: str | None = None,
               archive: bool = False) -> Task:
        task = self.get(id)
        if task.status != "awaiting_approval":
            raise RuleViolation(f"task {id} is not awaiting approval")
        ApprovalsRepo(self.s).record("task", id, "rejected" if archive else "changes_requested", decided_by=decided_by,
                                     channel=channel, note=note)
        return self.transition(id, "archived" if archive else "ready", actor=Actor.human(decided_by), reason=note)

    def block(self, id: int, kind: str, reason: str, *, actor: Actor) -> Task:
        require_in("kind", kind, BLOCK_KINDS)
        text = (reason or "").strip()
        if not text:
            raise ValidationError("a reason is required to block a task")
        text = text[:500]
        task = self.get(id)
        if task.block_count >= task.block_limit:
            self._close_run(id, "blocked", summary=text)
            return self.to_triage(id, f"block limit reached: {text}"[:500])
        result = self.transition(id, "blocked", actor=actor, reason=text, block_kind=kind, block_reason=text,
                                 block_count=task.block_count + 1)
        self._close_run(id, "blocked", summary=text)
        return result

    def unblock(self, id: int, *, actor: Actor, reason: str | None = None) -> Task:
        return self.transition(id, "ready", actor=actor, reason=reason)

    def release(self, id: int, reason: str, *, count_attempt: bool = False, outcome: str = "released") -> Task:
        """System: running -> ready (heartbeat expired, time cap, crash, plan limit, restart)."""
        require_in("outcome", outcome, ("released", "crashed", "timed_out"))
        task = self.get(id)
        if task.status != "running":
            raise RuleViolation(f"task {id} is not running")
        self._close_run(id, outcome, error=reason[:2000])
        if count_attempt and task.attempts >= task.max_attempts:
            return self.fail(id, reason)
        attempts = task.attempts if count_attempt else max(0, task.attempts - 1)
        return self.transition(id, "ready", actor=Actor.system(), reason=reason[:500], attempts=attempts)

    def fail(self, id: int, error: str) -> Task:
        task = self.get(id)
        if task.status == "running":
            self._close_run(id, "failure", error=error[:2000])
        return self.transition(id, "failed", actor=Actor.system(), reason=error[:500])

    def to_triage(self, id: int, reason: str, *, actor: Actor | None = None) -> Task:
        task = self.get(id)
        if task.status == "running":
            self._close_run(id, "released", error=reason[:2000])
        return self.transition(id, "triage", actor=actor or Actor.system(), reason=reason[:500])

    def archive(self, id: int, *, actor: Actor, reason: str | None = None) -> Task:
        task = self.get(id)
        result = self.transition(id, "archived", actor=actor, reason=reason)
        if task.status != "done":                       # an unfinished parent never releases its children
            for child_id in TaskLinksRepo(self.s).children(id):
                child = self.get(child_id)
                if child.status in ("todo", "ready", "blocked"):
                    self.transition(child_id, "triage", actor=Actor.system(), reason=f"parent {id} was archived")
        return result

    def reopen(self, id: int, *, actor: Actor, reason: str | None = None) -> Task:
        task = self.get(id)
        result = self.transition(id, "ready", actor=actor, reason=reason, attempts=0)
        if task.status == "done":                       # what depended on it has to wait again
            for child_id in TaskLinksRepo(self.s).children(id):
                child = self.get(child_id)
                if child.status in ("ready", "blocked"):
                    self.transition(child_id, "todo", actor=Actor.system(), reason=f"parent {id} was reopened")
        return result

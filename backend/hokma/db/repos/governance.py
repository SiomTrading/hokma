"""decisions, journal_entries, guard_events, bugs."""
from __future__ import annotations

from typing import Any

from hokma.db.errors import Conflict, RuleViolation, ValidationError
from hokma.db.models import Actor, Bug, Decision, GuardEvent, JournalEntry, dumps_json
from hokma.db.repos.base import UNSET, Repo, given, require_in
from hokma.db.repos.conversations import like
from hokma.db.timeutil import to_db_time, utcnow


class DecisionsRepo(Repo):
    def propose(self, topic: str, proposal: str, *, proposed_by: str, context: str | None = None, options: Any = None,
                project_id: int | None = None) -> Decision:
        if not 1 <= len((topic or "").strip()) <= 200:
            raise ValidationError("topic must have 1 to 200 characters")
        if not (proposal or "").strip():
            raise ValidationError("proposal is required")
        new_id = self.s.insert("decisions", {
            "project_id": project_id, "topic": topic.strip(), "context": context, "options": dumps_json(options),
            "proposal": proposal, "proposed_by": proposed_by, "created_at": utcnow()})
        return self.get(new_id)

    def get(self, id: int) -> Decision:
        return self._get("decisions", "id", id, Decision)

    def list(self, *, status: str | None = None, project_id: int | None = None, limit: int = 100) -> list[Decision]:
        sql, params = "SELECT * FROM decisions WHERE 1 = 1", {"limit": limit}
        if status:
            sql += " AND status = :status"
            params["status"] = status
        if project_id is not None:
            sql += " AND project_id = :project_id"
            params["project_id"] = project_id
        return [Decision.from_row(r) for r in self.s.query(sql + " ORDER BY id DESC LIMIT :limit", params)]

    def decide(self, id: int, verdict: str, *, actor: Actor, approval_id: int | None = None) -> Decision:
        require_in("verdict", verdict, ("approved", "rejected"))
        if actor.kind == "system":
            raise ValidationError("a decision is taken by a person or an agent")
        if actor.kind == "human" and approval_id is None:
            raise ValidationError("a decision taken by a person needs its approval id")
        current = self.get(id)
        if current.status != "proposed":
            raise RuleViolation(f"decision {id} is already {current.status}")
        self.s.update("decisions", {"id": id}, {"status": verdict, "decided_by_kind": actor.kind,
                                                "decided_by": actor.name, "approval_id": approval_id,
                                                "decided_at": utcnow()})
        return self.get(id)

    def supersede(self, id: int, by_id: int) -> Decision:
        if id == by_id:
            raise ValidationError("a decision cannot supersede itself")
        self.get(id)
        self.get(by_id)
        self.s.update("decisions", {"id": id}, {"status": "superseded", "superseded_by": by_id})
        return self.get(id)


class JournalRepo(Repo):
    def add(self, kind: str, title: str, *, actor: Actor, outcome: str = "info", agent_slug: str | None = None,
            task_id: int | None = None, project_id: int | None = None, body: str | None = None,
            refs: Any = None) -> JournalEntry:
        if agent_slug is None and actor.kind == "agent":
            agent_slug = actor.name
        new_id = self.s.insert("journal_entries", {
            "kind": kind, "outcome": outcome, "agent_slug": agent_slug, "actor_kind": actor.kind, "task_id": task_id,
            "project_id": project_id, "title": (title or "").strip(), "body": body, "refs": dumps_json(refs),
            "created_at": utcnow()})
        return self._get("journal_entries", "id", new_id, JournalEntry)

    def _where(self, kind, agent_slug, task_id, project_id, since, search) -> tuple[str, dict[str, Any]]:
        sql, params = " WHERE 1 = 1", {}
        for column, value in (("kind", kind), ("agent_slug", agent_slug), ("task_id", task_id),
                              ("project_id", project_id)):
            if value is not None:
                sql += f" AND {column} = :{column}"
                params[column] = value
        if since:
            sql += " AND created_at >= :since"
            params["since"] = to_db_time(since)
        if search:
            sql += " AND (title LIKE :q ESCAPE '\\' OR body LIKE :q ESCAPE '\\')"
            params["q"] = like(search)
        return sql, params

    def list(self, *, kind: str | None = None, agent_slug: str | None = None, task_id: int | None = None,
             project_id: int | None = None, since: Any = None, search: str | None = None,
             before_id: int | None = None, limit: int = 100) -> list[JournalEntry]:
        where, params = self._where(kind, agent_slug, task_id, project_id, since, search)
        if before_id is not None:
            where += " AND id < :before"
            params["before"] = before_id
        rows = self.s.query(f"SELECT * FROM journal_entries{where} ORDER BY id DESC LIMIT :limit",
                            {**params, "limit": limit})
        return [JournalEntry.from_row(r) for r in rows]

    def count(self, *, kind: str | None = None, agent_slug: str | None = None, task_id: int | None = None,
              project_id: int | None = None, since: Any = None, search: str | None = None) -> int:
        where, params = self._where(kind, agent_slug, task_id, project_id, since, search)
        return int(self.s.scalar(f"SELECT count(*) FROM journal_entries{where}", params))


HARD_RULES = ("delete", "truncate", "hokma_internal", "force_push", "outside_workdir", "secrets", "real_money")


class GuardEventsRepo(Repo):
    def record_block(self, rule: str, tool: str | None, target: str, *, task_id: int | None = None,
                     agent_slug: str | None = None, at: str | None = None) -> GuardEvent:
        """A call the always-on protections refused (`decision='blocked'`, `guard` = the rule)."""
        if rule not in HARD_RULES:
            raise ValidationError(f"unknown protection rule {rule!r}")
        new_id = self.s.insert("guard_events", {
            "guard": rule, "decision": "blocked", "agent_slug": agent_slug, "tool": (tool or "")[:200] or None,
            "task_id": task_id, "reason": (target or "")[:1000] or rule, "created_at": to_db_time(at) or utcnow()})
        return self._get("guard_events", "id", new_id, GuardEvent)

    def record(self, guard: str, decision: str, reason: str, *, agent_slug: str | None = None,
               tool: str | None = None, task_id: int | None = None) -> GuardEvent:
        new_id = self.s.insert("guard_events", {
            "guard": guard, "decision": decision, "agent_slug": agent_slug, "tool": tool, "task_id": task_id,
            "reason": (reason or "")[:1000], "created_at": utcnow()})
        return self._get("guard_events", "id", new_id, GuardEvent)

    def record_permission_ended(self, tool: str, reason: str, *, agent_slug: str | None = None,
                                task_id: int | None = None) -> GuardEvent:
        """A permission prompt nobody answered (timeout) or that a stop cancelled: a system block, not a person's call."""
        return self.record("permission", "blocked", reason, agent_slug=agent_slug, tool=tool, task_id=task_id)

    def list(self, *, guard: str | None = None, decision: str | None = None, since: Any = None,
             limit: int = 100) -> list[GuardEvent]:
        sql, params = "SELECT * FROM guard_events WHERE 1 = 1", {"limit": limit}
        for column, value in (("guard", guard), ("decision", decision)):
            if value is not None:
                sql += f" AND {column} = :{column}"
                params[column] = value
        if since:
            sql += " AND created_at >= :since"
            params["since"] = to_db_time(since)
        return [GuardEvent.from_row(r) for r in self.s.query(sql + " ORDER BY id DESC LIMIT :limit", params)]


class BugsRepo(Repo):
    def create(self, title: str, *, actor: Actor, severity: str = "medium", description: str | None = None,
               project_id: int | None = None) -> Bug:
        if not 1 <= len((title or "").strip()) <= 200:
            raise ValidationError("title must have 1 to 200 characters")
        require_in("severity", severity, ("low", "medium", "high", "critical"))
        new_id = self.s.insert("bugs", {
            "project_id": project_id, "title": title.strip(), "description": description, "severity": severity,
            "found_by_kind": actor.kind, "found_by": actor.name, "changed_by_kind": actor.kind,
            "changed_by": actor.name, "created_at": utcnow(), "updated_at": utcnow()})
        return self.get(new_id)

    def get(self, id: int) -> Bug:
        return self._get("bugs", "id", id, Bug)

    def list(self, *, status: str | None = None, severity: str | None = None, project_id: int | None = None,
             limit: int = 200) -> list[Bug]:
        sql, params = "SELECT * FROM bugs WHERE 1 = 1", {"limit": limit}
        for column, value in (("status", status), ("severity", severity), ("project_id", project_id)):
            if value is not None:
                sql += f" AND {column} = :{column}"
                params[column] = value
        return [Bug.from_row(r) for r in self.s.query(sql + " ORDER BY id DESC LIMIT :limit", params)]

    def transition(self, id: int, to_status: str, *, actor: Actor, fixed_by_task_id: int | None = None) -> Bug:
        current = self.get(id)
        values: dict[str, Any] = {"status": to_status, "changed_by_kind": actor.kind, "changed_by": actor.name}
        if fixed_by_task_id is not None:
            values["fixed_by_task_id"] = fixed_by_task_id
        if to_status in ("closed", "wont_fix"):
            values["closed_at"] = utcnow()
        elif current.closed_at:
            values["closed_at"] = None
        self.s._check_name(to_status)
        sets = ", ".join(f"{c} = :v_{c}" for c in values)
        changed = self.s.execute(f"UPDATE bugs SET {sets} WHERE id = :id AND status = :from",
                                 {f"v_{c}": v for c, v in values.items()} | {"id": id, "from": current.status})
        if changed == 0:
            raise Conflict(f"bug {id} changed while it was being updated")
        return self.get(id)

    def update(self, id: int, *, actor: Actor, title: Any = UNSET, description: Any = UNSET, severity: Any = UNSET,
               project_id: Any = UNSET) -> Bug:
        self.get(id)
        values = given(title=title, description=description, severity=severity, project_id=project_id)
        if "severity" in values:
            require_in("severity", values["severity"], ("low", "medium", "high", "critical"))
        if "title" in values and not 1 <= len((values["title"] or "").strip()) <= 200:
            raise ValidationError("title must have 1 to 200 characters")
        if values:
            self.s.update("bugs", {"id": id}, {**values, "updated_at": utcnow()})
        return self.get(id)

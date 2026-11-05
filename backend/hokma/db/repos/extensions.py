"""extensions, extension_requests and scheduled_jobs."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from hokma.db.errors import RuleViolation, ValidationError
from hokma.db.models import Actor, Extension, ExtensionRequest, ScheduledJob
from hokma.db.repos.approvals import ApprovalsRepo
from hokma.db.repos.base import UNSET, Repo, flag, given, require_in
from hokma.db.timeutil import from_db_time, to_db_time, utcnow

KINDS = ("plugin", "skill", "mcp")


class ExtensionsRepo(Repo):
    def add(self, name: str, marketplace: str, kind: str, *, version: str | None = None, license: str | None = None,
            enabled: bool = False, is_preinstalled: bool = False, approval_id: int | None = None) -> Extension:
        require_in("kind", kind, KINDS)
        if not name or not marketplace:
            raise ValidationError("name and marketplace are required")
        existing = self.find(marketplace, name)
        if existing is None:
            new_id = self.s.insert("extensions", {
                "name": name, "marketplace": marketplace, "kind": kind, "version": version, "license": license,
                "enabled": flag(enabled), "is_preinstalled": flag(is_preinstalled), "approval_id": approval_id,
                "installed_at": utcnow()})
            return self.get(new_id)
        self.s.update("extensions", {"id": existing.id}, {
            "kind": kind, "version": version, "license": license, "enabled": flag(enabled),
            "approval_id": approval_id if approval_id is not None else existing.approval_id})
        return self.get(existing.id)

    def get(self, id: int) -> Extension:
        return self._get("extensions", "id", id, Extension)

    def find(self, marketplace: str, name: str) -> Extension | None:
        row = self.s.one("SELECT * FROM extensions WHERE marketplace = :m AND name = :n", {"m": marketplace, "n": name})
        return Extension.from_row(row) if row else None

    def list(self, *, kind: str | None = None, enabled: bool | None = None) -> list[Extension]:
        sql, params = "SELECT * FROM extensions WHERE 1 = 1", {}
        if kind:
            sql += " AND kind = :kind"
            params["kind"] = kind
        if enabled is not None:
            sql += " AND enabled = :enabled"
            params["enabled"] = flag(enabled)
        return [Extension.from_row(r) for r in self.s.query(sql + " ORDER BY marketplace, name", params)]

    def set_enabled(self, id: int, enabled: bool) -> Extension:
        self.get(id)
        self.s.update("extensions", {"id": id}, {"enabled": flag(enabled)})
        return self.get(id)

    def remove(self, id: int) -> None:
        self.get(id)
        self.s.execute("DELETE FROM extensions WHERE id = :id", {"id": id})


class ExtensionRequestsRepo(Repo):
    def create(self, action: str, kind: str, name: str, marketplace: str, reason: str, *, actor: Actor,
               version: str | None = None, license: str | None = None, task_id: int | None = None) -> ExtensionRequest:
        require_in("action", action, ("install", "enable", "disable", "uninstall"))
        require_in("kind", kind, KINDS)
        if actor.kind == "system":
            raise ValidationError("a request comes from a person or an agent")
        if not 1 <= len((reason or "").strip()) <= 1000:
            raise ValidationError("reason must have 1 to 1000 characters")
        new_id = self.s.insert("extension_requests", {
            "action": action, "kind": kind, "name": name, "marketplace": marketplace, "version": version,
            "license": license, "reason": reason.strip(), "requested_by_kind": actor.kind, "requested_by": actor.name,
            "task_id": task_id, "requested_at": utcnow()})
        return self.get(new_id)

    def get(self, id: int) -> ExtensionRequest:
        return self._get("extension_requests", "id", id, ExtensionRequest)

    def list(self, *, status: str | None = None, limit: int = 100) -> list[ExtensionRequest]:
        sql, params = "SELECT * FROM extension_requests WHERE 1 = 1", {"limit": limit}
        if status:
            sql += " AND status = :status"
            params["status"] = status
        return [ExtensionRequest.from_row(r) for r in self.s.query(sql + " ORDER BY id DESC LIMIT :limit", params)]

    def _decide(self, id: int, verdict: str, status: str, decided_by: str, channel: str, note: str | None):
        request = self.get(id)
        if request.status != "pending":
            raise RuleViolation(f"request {id} is already {request.status}")
        approval = ApprovalsRepo(self.s).record("extension", id, verdict, decided_by=decided_by, channel=channel, note=note)
        self.s.update("extension_requests", {"id": id}, {"status": status, "approval_id": approval.id,
                                                         "decided_at": utcnow()})
        return self.get(id)

    def approve(self, id: int, *, decided_by: str, channel: str = "dashboard", note: str | None = None):
        return self._decide(id, "approved", "approved", decided_by, channel, note)

    def reject(self, id: int, *, decided_by: str, channel: str = "dashboard", note: str | None = None):
        return self._decide(id, "rejected", "rejected", decided_by, channel, note)

    def mark_applied(self, id: int, extension_id: int) -> ExtensionRequest:
        request = self.get(id)
        if request.status != "approved":
            raise RuleViolation(f"request {id} is {request.status}, not approved")
        self.s.update("extension_requests", {"id": id}, {"status": "applied", "extension_id": extension_id,
                                                         "applied_at": utcnow()})
        return self.get(id)

    def mark_failed(self, id: int, error: str) -> ExtensionRequest:
        request = self.get(id)
        if request.status != "approved":
            raise RuleViolation(f"request {id} is {request.status}, not approved")
        self.s.update("extension_requests", {"id": id}, {"status": "failed", "error": (error or "")[:1000]})
        return self.get(id)

    def expire_older_than(self, hours: float = 72) -> int:
        cutoff = to_db_time(from_db_time(utcnow()) - timedelta(hours=hours))
        return self.s.execute("UPDATE extension_requests SET status = 'expired' WHERE status = 'pending' "
                              "AND requested_at < :cutoff", {"cutoff": cutoff})


class ScheduledJobsRepo(Repo):
    def create(self, name: str, agent_slug: str, prompt: str, schedule_kind: str, schedule_expr: str, timezone: str,
               *, is_active: bool = False, approval_id: int | None = None, next_run_at: Any = None) -> ScheduledJob:
        require_in("schedule_kind", schedule_kind, ("interval", "daily", "weekly", "cron", "once"))
        new_id = self.s.insert("scheduled_jobs", {
            "name": name, "agent_slug": agent_slug, "prompt": prompt, "schedule_kind": schedule_kind,
            "schedule_expr": schedule_expr, "timezone": timezone, "is_active": flag(is_active),
            "approval_id": approval_id, "next_run_at": to_db_time(next_run_at), "created_at": utcnow()})
        return self.get(new_id)

    def get(self, id: int) -> ScheduledJob:
        return self._get("scheduled_jobs", "id", id, ScheduledJob)

    def find_by_name(self, name: str) -> ScheduledJob | None:
        return self._find("scheduled_jobs", "name", name, ScheduledJob)

    def list(self, *, active: bool | None = None) -> list[ScheduledJob]:
        sql, params = "SELECT * FROM scheduled_jobs WHERE 1 = 1", {}
        if active is not None:
            sql += " AND is_active = :a"
            params["a"] = flag(active)
        return [ScheduledJob.from_row(r) for r in self.s.query(sql + " ORDER BY name", params)]

    def update(self, id: int, *, name: Any = UNSET, agent_slug: Any = UNSET, prompt: Any = UNSET,
               schedule_kind: Any = UNSET, schedule_expr: Any = UNSET, timezone: Any = UNSET,
               next_run_at: Any = UNSET) -> ScheduledJob:
        self.get(id)
        values = given(name=name, agent_slug=agent_slug, prompt=prompt, schedule_kind=schedule_kind,
                       schedule_expr=schedule_expr, timezone=timezone, next_run_at=next_run_at)
        if "schedule_kind" in values:
            require_in("schedule_kind", values["schedule_kind"], ("interval", "daily", "weekly", "cron", "once"))
        if "next_run_at" in values:
            values["next_run_at"] = to_db_time(values["next_run_at"])
        self.s.update("scheduled_jobs", {"id": id}, values)
        return self.get(id)

    def set_active(self, id: int, active: bool, *, approval_id: int | None = None) -> ScheduledJob:
        self.get(id)
        values: dict[str, Any] = {"is_active": flag(active)}
        if approval_id is not None:
            values["approval_id"] = approval_id
        self.s.update("scheduled_jobs", {"id": id}, values)
        return self.get(id)

    def due(self, now: Any = None) -> list[ScheduledJob]:
        rows = self.s.query("SELECT * FROM scheduled_jobs WHERE is_active = 1 AND next_run_at IS NOT NULL "
                            "AND next_run_at <= :now ORDER BY next_run_at, id", {"now": to_db_time(now) or utcnow()})
        return [ScheduledJob.from_row(r) for r in rows]

    def mark_ran(self, id: int, *, task_id: int | None, last_run_at: Any = None, next_run_at: Any = None) -> ScheduledJob:
        self.get(id)
        self.s.update("scheduled_jobs", {"id": id}, {"last_run_at": to_db_time(last_run_at) or utcnow(),
                                                     "next_run_at": to_db_time(next_run_at), "last_task_id": task_id})
        return self.get(id)

    def delete(self, id: int) -> None:
        self.get(id)
        self.s.execute("DELETE FROM scheduled_jobs WHERE id = :id", {"id": id})

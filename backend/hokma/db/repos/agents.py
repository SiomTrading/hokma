"""agents."""
from __future__ import annotations

import re
from typing import Any, Iterable

from hokma.db.errors import ValidationError
from hokma.db.models import Agent
from hokma.db.repos.base import UNSET, Repo, flag, given, require_in
from hokma.db.timeutil import utcnow

TIERS = ("fast", "balanced", "deep")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")


def check_slug(slug: str) -> str:
    if not isinstance(slug, str) or not _SLUG.match(slug):
        raise ValidationError(f"invalid agent slug: {slug!r}")
    return slug


class AgentsRepo(Repo):
    def list(self, *, status: str | None = None, include_custom: bool = True) -> list[Agent]:
        sql, params = "SELECT * FROM agents WHERE 1 = 1", {}
        if status:
            require_in("status", status, ("active", "off"))
            sql += " AND status = :status"
            params["status"] = status
        if not include_custom:
            sql += " AND is_custom = 0"
        return [Agent.from_row(r) for r in self.s.query(sql + " ORDER BY slug", params)]

    def get(self, slug: str) -> Agent:
        return self._get("agents", "slug", slug, Agent)

    def find(self, slug: str) -> Agent | None:
        return self._find("agents", "slug", slug, Agent)

    def count(self, status: str | None = None) -> int:
        if status:
            return int(self.s.scalar("SELECT count(*) FROM agents WHERE status = :s", {"s": status}))
        return int(self.s.scalar("SELECT count(*) FROM agents"))

    def create_custom(self, slug: str, display_name: str, *, description: str | None = None,
                      model_tier: str = "balanced", file_path: str | None = None) -> Agent:
        check_slug(slug)
        require_in("model_tier", model_tier, TIERS)
        if not display_name or not display_name.strip():
            raise ValidationError("display_name is required")
        self.s.insert_row("agents", {"slug": slug, "display_name": display_name.strip(), "description": description,
                                     "model_tier": model_tier, "is_custom": 1, "file_path": file_path,
                                     "created_at": utcnow(), "updated_at": utcnow()})
        return self.get(slug)

    def set_status(self, slug: str, status: str, *, enabled_session: Any = UNSET) -> Agent:
        require_in("status", status, ("active", "off"))
        self.get(slug)
        values: dict[str, Any] = {"status": status, "updated_at": utcnow()}
        values.update(given(enabled_session=enabled_session))
        self.s.update("agents", {"slug": slug}, values)
        return self.get(slug)

    def set_model_tier(self, slug: str, tier: str) -> Agent:
        require_in("model_tier", tier, TIERS)
        self.get(slug)
        self.s.update("agents", {"slug": slug}, {"model_tier": tier, "updated_at": utcnow()})
        return self.get(slug)

    def update(self, slug: str, *, display_name: Any = UNSET, description: Any = UNSET,
               file_path: Any = UNSET) -> Agent:
        self.get(slug)
        values = given(display_name=display_name, description=description, file_path=file_path)
        if "display_name" in values and not str(values["display_name"] or "").strip():
            raise ValidationError("display_name is required")
        if values:
            self.s.update("agents", {"slug": slug}, {**values, "updated_at": utcnow()})
        return self.get(slug)

    def upsert_kit(self, slug: str, display_name: str, description: str | None, model_tier: str,
                   file_path: str | None, *, is_custom: bool = False) -> Agent:
        """Seeding: insert new agents (status off); refresh text of known ones. Status and tier the owner chose stay."""
        check_slug(slug)
        require_in("model_tier", model_tier, TIERS)
        current = self.find(slug)
        if current is None:
            self.s.insert_row("agents", {"slug": slug, "display_name": display_name, "description": description,
                                         "model_tier": model_tier, "is_custom": flag(is_custom),
                                         "file_path": file_path, "created_at": utcnow(), "updated_at": utcnow()})
        elif (current.display_name, current.description, current.file_path, current.is_custom) != (
                display_name, description, file_path, is_custom):
            self.s.update("agents", {"slug": slug}, {"display_name": display_name, "description": description,
                                                     "file_path": file_path, "is_custom": flag(is_custom),
                                                     "updated_at": utcnow()})
        return self.get(slug)

    def mark_missing_off(self, present_slugs: Iterable[str]) -> int:
        present = set(present_slugs)
        changed = 0
        for agent in self.list(include_custom=False):
            if agent.slug not in present and agent.status != "off":
                self.s.update("agents", {"slug": agent.slug}, {"status": "off", "updated_at": utcnow()})
                changed += 1
        return changed

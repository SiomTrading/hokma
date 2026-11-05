"""projects."""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from hokma.db.errors import ValidationError
from hokma.db.models import Project
from hokma.db.repos.base import UNSET, Repo, flag, given, require_in
from hokma.db.timeutil import utcnow

KINDS = ("ips", "wealth", "strategy", "research", "robot", "other")
STATUSES = ("idea", "active", "paused", "done", "cancelled")


def slugify(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")
    return slug[:60] or "project"


class ProjectsRepo(Repo):
    def _unique_slug(self, base: str) -> str:
        slug, n = base, 1
        while self.s.scalar("SELECT 1 FROM projects WHERE slug = :s", {"s": slug}) is not None:
            n += 1
            slug = f"{base}-{n}"
        return slug

    def create(self, name: str, path: str, *, slug: str | None = None, kind: str = "other", status: str = "active",
               description: str | None = None, default_requires_review: bool = False,
               default_reviewer_agent: str | None = None, default_requires_approval: bool = False) -> Project:
        name = (name or "").strip()
        if not 1 <= len(name) <= 120:
            raise ValidationError("name must have 1 to 120 characters")
        if not path or not str(path).strip():
            raise ValidationError("path is required")
        require_in("kind", kind, KINDS)
        require_in("status", status, STATUSES)
        if default_requires_review and not default_reviewer_agent:
            raise ValidationError("a default reviewer is required when review is on")
        slug = slugify(slug) if slug else self._unique_slug(slugify(name))
        new_id = self.s.insert("projects", {
            "slug": slug, "name": name, "path": str(path), "kind": kind, "status": status, "description": description,
            "default_requires_review": flag(default_requires_review), "default_reviewer_agent": default_reviewer_agent,
            "default_requires_approval": flag(default_requires_approval),
            "created_at": utcnow(), "updated_at": utcnow()})
        return self.get(new_id)

    def get(self, id: int) -> Project:
        return self._get("projects", "id", id, Project)

    def find(self, id: int) -> Project | None:
        return self._find("projects", "id", id, Project)

    def get_by_slug(self, slug: str) -> Project | None:
        return self._find("projects", "slug", slug, Project)

    def list(self, *, status: str | None = None, kind: str | None = None) -> list[Project]:
        sql, params = "SELECT * FROM projects WHERE 1 = 1", {}
        if status:
            sql += " AND status = :status"
            params["status"] = status
        if kind:
            sql += " AND kind = :kind"
            params["kind"] = kind
        return [Project.from_row(r) for r in self.s.query(sql + " ORDER BY name, id", params)]

    def update(self, id: int, *, name: Any = UNSET, path: Any = UNSET, description: Any = UNSET, kind: Any = UNSET,
               status: Any = UNSET, default_requires_review: Any = UNSET, default_reviewer_agent: Any = UNSET,
               default_requires_approval: Any = UNSET) -> Project:
        current = self.get(id)
        values = given(name=name, path=path, description=description, kind=kind, status=status,
                       default_requires_review=default_requires_review, default_reviewer_agent=default_reviewer_agent,
                       default_requires_approval=default_requires_approval)
        if "name" in values and not 1 <= len((values["name"] or "").strip()) <= 120:
            raise ValidationError("name must have 1 to 120 characters")
        if "path" in values and not str(values["path"] or "").strip():
            raise ValidationError("path is required")
        if "kind" in values:
            require_in("kind", values["kind"], KINDS)
        if "status" in values:
            require_in("status", values["status"], STATUSES)
        for key in ("default_requires_review", "default_requires_approval"):
            if key in values:
                values[key] = flag(values[key])
        review = values.get("default_requires_review", flag(current.default_requires_review))
        reviewer = values.get("default_reviewer_agent", current.default_reviewer_agent)
        if review and not reviewer:
            raise ValidationError("a default reviewer is required when review is on")
        if values:
            self.s.update("projects", {"id": id}, {**values, "updated_at": utcnow()})
        return self.get(id)

    def delete(self, id: int) -> None:
        self.get(id)
        self.s.execute("DELETE FROM projects WHERE id = :id", {"id": id})

"""projects."""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from hokma.db.errors import NotFound, ValidationError
from hokma.db.models import Project
from hokma.db.repos.base import UNSET, Repo, flag, given, require_in
from hokma.db.timeutil import utcnow

KINDS = ("ips", "wealth", "strategy", "research", "robot", "other")
STATUSES = ("idea", "active", "paused", "done", "cancelled")


def slugify(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")
    return slug[:60] or "project"


# (table, column) that a plain delete sets to NULL (ON DELETE SET NULL); decisions and the journal keep the link
DETACH = (
    ("tasks", "project_id"), ("bugs", "project_id"), ("conversations", "project_id"),
    ("attachments", "project_id"), ("trash_items", "project_id"), ("documents", "origin_project_id"),
    ("profile", "active_project_id"),
)


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

    def get(self, id: int, *, include_archived: bool = False) -> Project:
        project = self._get("projects", "id", id, Project)
        if project.archived_at and not include_archived:
            raise NotFound(f"project {id} not found")
        return project

    def find(self, id: int, *, include_archived: bool = False) -> Project | None:
        project = self._find("projects", "id", id, Project)
        return None if project is not None and project.archived_at and not include_archived else project

    def get_by_slug(self, slug: str) -> Project | None:
        return self._find("projects", "slug", slug, Project)

    def list(self, *, status: str | None = None, kind: str | None = None,
             include_archived: bool = False) -> list[Project]:
        sql, params = "SELECT * FROM projects WHERE 1 = 1", {}
        if not include_archived:
            sql += " AND archived_at IS NULL"
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

    def delete(self, id: int) -> bool:
        """Remove a project. One with entries in the journal (append-only, and it points at the project) is archived
        instead: its row stays, hidden, with its name and the date, so the history keeps showing where each entry came
        from. Returns True when it was archived. Everything a plain delete would have detached is detached either way."""
        project = self.get(id)
        archive = self.s.scalar("SELECT 1 FROM journal_entries WHERE project_id = :p LIMIT 1", {"p": id}) is not None
        if not archive:
            self.s.execute("DELETE FROM projects WHERE id = :id", {"id": id})
            return False
        params = {"p": id}
        for table, column in DETACH:
            self.s.execute(f"UPDATE {table} SET {column} = NULL WHERE {column} = :p", params)
        self.s.execute("DELETE FROM project_folders WHERE project_id = :p", params)
        self.s.execute("DELETE FROM libraries WHERE project_id = :p", params)
        self.s.update("projects", {"id": id}, {"archived_at": utcnow(), "slug": f"{project.slug}~archived-{id}",
                                               "updated_at": utcnow()})
        return True

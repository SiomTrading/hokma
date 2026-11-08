"""trash_items: what the agent moved to the Hokma trash instead of deleting."""
from __future__ import annotations

from hokma.db.errors import ValidationError
from hokma.db.models import TrashItem
from hokma.db.repos.base import Repo
from hokma.db.timeutil import utcnow

MAX_REASON = 500


class TrashItemsRepo(Repo):
    def add(self, *, original_path: str, stored_path: str, size_bytes: int | None, project_id: int | None = None,
            conversation_id: int | None = None, reason: str | None = None,
            source_root: str | None = None) -> TrashItem:
        if not original_path or not stored_path:
            raise ValidationError("original_path and stored_path are required")
        new_id = self.s.insert("trash_items", {
            "original_path": original_path, "stored_path": stored_path, "source_root": source_root, "size_bytes": size_bytes,
            "project_id": project_id, "conversation_id": conversation_id,
            "reason": (reason or "")[:MAX_REASON] or None, "moved_at": utcnow()})
        return self.get(new_id)

    def get(self, id: int) -> TrashItem:
        return self._get("trash_items", "id", id, TrashItem)

    def find(self, id: int) -> TrashItem | None:
        return self._find("trash_items", "id", id, TrashItem)

    def source_root(self, id: int) -> str | None:
        """The folder (working folder or project) the item was moved from; None when it was not recorded."""
        row = self.s.one("SELECT source_root FROM trash_items WHERE id = :id", {"id": id})
        return row["source_root"] if row else None

    @property
    def points(self) -> "CheckpointPointsRepo":
        return CheckpointPointsRepo(self.s)

    def set_stored_path(self, id: int, stored_path: str) -> None:
        self.s.update("trash_items", {"id": id}, {"stored_path": stored_path})

    def list(self, *, include_gone: bool = False, limit: int = 500) -> list[TrashItem]:
        """Items still in the trash, newest first. `include_gone` adds restored and purged ones."""
        where = "" if include_gone else " WHERE restored_at IS NULL AND purged_at IS NULL"
        rows = self.s.query(f"SELECT * FROM trash_items{where} ORDER BY id DESC LIMIT :n", {"n": limit})
        return [TrashItem.from_row(r) for r in rows]

    def mark_restored(self, id: int) -> TrashItem:
        self.s.update("trash_items", {"id": id}, {"restored_at": utcnow()})
        return self.get(id)

    def mark_purged(self, id: int) -> TrashItem:
        self.s.update("trash_items", {"id": id}, {"purged_at": utcnow()})
        return self.get(id)

    def delete_row(self, id: int) -> None:
        """Only for a move that failed halfway (the row was created before the files moved)."""
        self.s.execute("DELETE FROM trash_items WHERE id = :id", {"id": id})


class CheckpointPointsRepo(Repo):
    """checkpoint_points: the commit SHA of each restore point Hokma took, the only ones it trusts."""

    def add(self, project_path: str, point_id: str, commit_sha: str) -> None:
        if not project_path or not point_id or not commit_sha:
            raise ValidationError("project_path, point_id and commit_sha are required")
        self.s.execute("DELETE FROM checkpoint_points WHERE project_path = :p AND point_id = :i",
                       {"p": project_path, "i": point_id})
        self.s.insert("checkpoint_points", {"project_path": project_path, "point_id": point_id,
                                            "commit_sha": commit_sha, "created_at": utcnow()})

    def shas(self, project_path: str) -> dict[str, str]:
        rows = self.s.query("SELECT point_id, commit_sha FROM checkpoint_points WHERE project_path = :p",
                            {"p": project_path})
        return {r["point_id"]: r["commit_sha"] for r in rows}

    def delete(self, project_path: str, point_id: str) -> None:
        self.s.execute("DELETE FROM checkpoint_points WHERE project_path = :p AND point_id = :i",
                       {"p": project_path, "i": point_id})

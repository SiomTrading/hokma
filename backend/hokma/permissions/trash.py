"""The Hokma trash: where whatever the agent wants gone is moved instead of deleted.

`move` is the only thing an agent can do (through the MCP tool `trash_move`): it accepts paths inside the working
folder, never Hokma's own places, and puts each one in `<data>/trash/{id}/`. Restoring, deleting for good and
emptying are acts of the owner and live behind the panel's session-token endpoints; no agent tool reaches them and
`<data>/trash` is covered by the `hokma_internal` rule.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from hokma.core.config import hokma_data_dir
from hokma.i18n import t
from hokma.kit.guard import under_any

MAX_PATHS = 50
MAX_WALK = 200_000
FORBIDDEN_PARTS = {".git", ".hokma", ".claude"}


class TrashError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def trash_root() -> Path:
    return hokma_data_dir() / "trash"


def _size(path: Path) -> int:
    try:
        if path.is_symlink() or path.is_file():
            return path.lstat().st_size
        total, seen = 0, 0
        for base, _dirs, files in os.walk(path):
            for name in files:
                seen += 1
                if seen > MAX_WALK:
                    return total
                try:
                    total += os.lstat(os.path.join(base, name)).st_size
                except OSError:
                    pass
        return total
    except OSError:
        return 0


def _resolve_item(raw: str, cwd: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else Path(cwd) / path


def check_movable(raw: str, *, roots: list[str], protect: list[str], cwd: str) -> Path:
    """The path to move, or TrashError. The item itself may be a link: it is the link that moves, never its target."""
    if not isinstance(raw, str) or not raw.strip() or "\0" in raw:
        raise TrashError("missing", t("trash.err.missing"))
    item = _resolve_item(raw.strip(), cwd)
    if not os.path.lexists(item):
        raise TrashError("missing", t("trash.err.missing"))
    parent = Path(os.path.realpath(item.parent))
    placed = parent / item.name                                   # real folder, the item's own name
    if item.name in ("", ".", "..") or not under_any(str(parent), roots) or any(
            os.path.realpath(r) == str(placed) for r in roots):
        raise TrashError("outside", t("trash.err.outside"))
    if under_any(str(placed), protect) or under_any(str(trash_root()), [str(placed)]):
        raise TrashError("protected", t("trash.err.protected"))
    if has_forbidden_part(str(placed), roots):                    # the same names Restore refuses, so nothing is stranded
        raise TrashError("protected", t("trash.err.repo_folders"))
    return placed


def has_forbidden_part(real_path: str, roots: list[str]) -> bool:
    """A component of the path, below its most specific root, is a repository or settings folder (casefolded)."""
    holding = [os.path.realpath(r) for r in roots if within(real_path, os.path.realpath(r))]
    if not holding:
        return False
    root = max(holding, key=len)
    return any(part.casefold() in FORBIDDEN_PARTS for part in Path(os.path.relpath(real_path, root)).parts)


def within(real_path: str, real_root: str) -> bool:
    """`real_path` is `real_root` or inside it. Both are already real paths: a link is never followed again here."""
    try:
        return os.path.commonpath([real_path, real_root]) == real_root
    except ValueError:
        return False


def _source_root(src: Path, roots: list[str]) -> str:
    """The most specific root (real path) that holds `src`: the folder a restore must stay inside."""
    holding = [os.path.realpath(r) for r in roots if within(str(src), os.path.realpath(r))]
    if not holding:
        raise TrashError("outside", t("trash.err.outside"))
    return max(holding, key=len)


def _project_of(db: Any, roots: list[str]) -> int | None:
    try:
        with db.read() as u:
            wanted = {os.path.realpath(r) for r in roots}
            for p in u.projects.list():
                if os.path.realpath(p.path) in wanted:
                    return p.id
    except Exception:
        pass
    return None


def move(paths: list[str], reason: str, *, db: Any, roots: list[str], protect: list[str], cwd: str,
         project_id: int | None = None, conversation_id: int | None = None) -> list[dict[str, Any]]:
    """Move each path to the trash. Everything is checked first: one bad path moves nothing."""
    if not isinstance(paths, list) or not paths or len(paths) > MAX_PATHS:
        raise TrashError("missing", t("trash.err.missing"))
    if not isinstance(reason, str) or not reason.strip():
        raise TrashError("reason", t("trash.err.reason"))
    placed = [check_movable(p, roots=roots, protect=protect, cwd=cwd) for p in paths]
    if project_id is None:
        project_id = _project_of(db, roots)
    if conversation_id is not None:
        with db.read() as u:
            if u.conversations.find(conversation_id) is None:
                conversation_id = None                           # a stale id must not stop the move
    done: list[dict[str, Any]] = []
    for src in placed:
        size = _size(src)
        root = _source_root(src, roots)
        with db.write() as u:
            row = u.trash.add(original_path=str(src), stored_path="pending", size_bytes=size, project_id=project_id,
                              conversation_id=conversation_id, reason=reason.strip(), source_root=root)
        dest_dir = trash_root() / str(row.id)
        dest = dest_dir / src.name
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
        except OSError as exc:
            shutil.rmtree(dest_dir, ignore_errors=True)
            with db.write() as u:
                u.trash.delete_row(row.id)
            raise TrashError("move_failed", str(exc)) from exc
        with db.write() as u:
            u.trash.set_stored_path(row.id, str(dest))
        done.append({"id": row.id, "path": str(src), "stored": str(dest)})
    return done


# --------------------------------------------------------------------------------------------- owner's side

def _stored_ok(stored: str) -> bool:
    try:
        return under_any(stored, [str(trash_root())]) and os.path.realpath(stored) != os.path.realpath(trash_root())
    except OSError:
        return False


def describe(db: Any, item) -> dict[str, Any]:
    stored = Path(item.stored_path)
    present = os.path.lexists(stored)
    names = {}
    try:
        with db.read() as u:
            project = u.projects.find(item.project_id) if item.project_id else None
            conversation = u.conversations.find(item.conversation_id) if item.conversation_id else None
        names = {"project_name": project.name if project else None,
                 "conversation_title": (conversation.title or "") if conversation else None}
    except Exception:
        names = {"project_name": None, "conversation_title": None}
    return {"id": item.id, "name": Path(item.original_path).name, "original_path": item.original_path,
            "project_id": item.project_id, "conversation_id": item.conversation_id, **names,
            "size_bytes": item.size_bytes or 0, "moved_at": item.moved_at, "reason": item.reason or "",
            "is_dir": present and stored.is_dir() and not stored.is_symlink(), "present": present}


def listing(db: Any) -> dict[str, Any]:
    with db.read() as u:
        rows = u.trash.list()
    items = [describe(db, r) for r in rows]
    return {"items": items, "count": len(items), "total_bytes": sum(i["size_bytes"] for i in items if i["present"])}


def free_name(target: Path) -> Path:
    """`name (restored).ext`, then `name (restored 2).ext`, when the original name is taken."""
    stem, suffix = target.stem, target.suffix
    for n in range(1, 1000):
        label = "(restored)" if n == 1 else f"(restored {n})"
        candidate = target.with_name(f"{stem} {label}{suffix}")
        if not os.path.lexists(candidate):
            return candidate
    raise TrashError("restore_failed", t("permissions.err.restore_failed", error="no free name"))


def restore(db: Any, item_id: int, *, protect: list[str]) -> dict[str, Any]:
    with db.read() as u:
        item = u.trash.find(item_id)
    if item is None or item.restored_at or item.purged_at:
        raise TrashError("not_found", t("permissions.err.not_found"))
    stored = Path(item.stored_path)
    if not _stored_ok(item.stored_path) or not os.path.lexists(stored):
        raise TrashError("not_found", t("permissions.err.not_found"))
    target = Path(item.original_path)
    with db.read() as u:
        root = u.trash.source_root(item.id)
    real_parent = os.path.realpath(target.parent)
    if not root or not within(real_parent, os.path.realpath(root)):        # no origin, or a link leads out of it
        raise TrashError("outside", t("trash.err.outside"))
    real_target = os.path.join(real_parent, target.name)
    if any(under_any(p, protect) for p in (str(target.parent), str(target), real_parent, real_target)):
        raise TrashError("protected", t("trash.err.protected"))
    real_root = os.path.realpath(root)
    if has_forbidden_part(real_target, [real_root]):
        raise TrashError("protected", t("trash.err.protected"))
    walk = Path(real_root)
    below = Path(os.path.relpath(target.parent, real_root)).parts
    for part in () if below[:1] == ("..",) else below:
        walk = walk / part
        if os.path.islink(walk):                                           # a link in the way is never followed
            raise TrashError("outside", t("trash.err.outside"))
    target = Path(real_parent) / target.name                               # restore where it was checked
    if os.path.lexists(target):
        target = free_name(target)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if os.path.realpath(target.parent) != real_parent:                 # the folder changed under us
            raise OSError("folder changed")
        shutil.move(str(stored), str(target))
    except OSError as exc:
        raise TrashError("restore_failed", t("permissions.err.restore_failed", error=str(exc))) from exc
    shutil.rmtree(trash_root() / str(item.id), ignore_errors=True)
    with db.write() as u:
        u.trash.mark_restored(item.id)
    return {"id": item.id, "restored_to": str(target), "renamed": target.name != Path(item.original_path).name}


def purge(db: Any, item_id: int) -> dict[str, Any]:
    with db.read() as u:
        item = u.trash.find(item_id)
    if item is None or item.purged_at or item.restored_at:
        raise TrashError("not_found", t("permissions.err.not_found"))
    folder = trash_root() / str(item.id)
    if _stored_ok(item.stored_path) and under_any(str(folder), [str(trash_root())]):
        shutil.rmtree(folder, ignore_errors=True)
    with db.write() as u:
        u.trash.mark_purged(item.id)
    return {"id": item.id, "name": Path(item.original_path).name}


def empty(db: Any) -> int:
    with db.read() as u:
        ids = [i.id for i in u.trash.list(limit=100_000)]
    for item_id in ids:
        purge(db, item_id)
    return len(ids)

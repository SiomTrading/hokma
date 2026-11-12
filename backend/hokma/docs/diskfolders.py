"""Disk folders of a project (SPEC-010 section 4.3): which folders a project reads, the rules that keep a folder out,
and the project's own folder, which is registered by itself.

Every project owns a folder (the one its Files tab shows, with `documentos/`, `codigo/`, `contexto/` and so on). That
folder is registered as a read folder of the project without the student doing anything, for new and existing
projects, so a PDF dropped in Files becomes searchable. Other folders of the disk are added by the student with the
native picker. The files themselves are never copied, changed or deleted.
"""
import os
import sys
from pathlib import Path
from typing import Any

from hokma.core.config import hokma_data_dir
from hokma.db import Database, NotFound
from hokma.db.errors import DuplicateName, ValidationError
from hokma.db.models import ProjectFolder
from hokma.docs import folder_worker
from hokma.docs.ingest import _db
from hokma.projects.errors import ProjectError

MAX_FOLDERS_PER_PROJECT = 5
OWN_LABEL = "@own"                                    # marks the project's own folder; never shown, the UI names it
SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", "venv", ".venv"})

_POSIX_SYSTEM = ("/bin", "/boot", "/dev", "/etc", "/lib", "/lib32", "/lib64", "/proc", "/root", "/run", "/sbin",
                 "/sys", "/usr", "/var", "/System", "/Library", "/Applications", "/private", "/cores")
_WINDOWS_SYSTEM_NAMES = ("windows", "program files", "program files (x86)", "programdata", "$recycle.bin",
                         "system volume information")


def is_own(folder: ProjectFolder) -> bool:
    return folder.label == OWN_LABEL


def is_skipped_dir(name: str) -> bool:
    """Folders the scan never enters: hidden ones, `.git`, dependency and cache folders."""
    return name.startswith(".") or name.lower() in SKIP_DIRS


def inside(child: Path, parent: Path) -> bool:
    """True when `child` is `parent` or lives under it. Both must be resolved."""
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


# macOS resolves /var to /private/var and /tmp to /private/tmp. These are the user's own temporary areas (the per-user
# folder /private/var/folders/<x>/<y>/T among them), so they are not system folders even though /private is.
_POSIX_USER_AREAS = ("/private/var/folders", "/private/var/tmp", "/private/tmp", "/var/folders", "/var/tmp")


def _under(text: str, root: str) -> bool:
    return text == root or text.startswith(root.rstrip("/") + "/")


def is_posix_system_path(text: str, home: str | None = None) -> bool:
    """True when a resolved POSIX path is a system folder. Anything under the user's home and the user's temporary
    areas is never system, even when the home itself sits under a listed root (/root, /var/lib/user)."""
    if home and _under(text, home):
        return False
    if any(_under(text, area) for area in _POSIX_USER_AREAS):
        return False
    return any(_under(text, root) for root in _POSIX_SYSTEM)


def _is_system_folder(path: Path) -> bool:
    if sys.platform == "win32" or (len(path.parts) > 0 and ":" in path.parts[0]):
        parts = [p.lower() for p in path.parts[1:]]
        return bool(parts) and parts[0] in _WINDOWS_SYSTEM_NAMES
    try:
        home = Path.home().resolve().as_posix()
    except (OSError, RuntimeError):                                          # pragma: no cover
        home = None
    return is_posix_system_path(path.as_posix(), home)


def check_folder(raw: object) -> Path:
    """The resolved path of a folder the student may add, or a ProjectError that says why not (section 4.3): not a
    folder, the root of a disk, the whole personal folder, Hokma's own data folder or a system folder."""
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 1000 or "\x00" in raw:
        raise ProjectError("bad_path", 400)
    candidate = Path(raw.strip()).expanduser()
    if not candidate.is_absolute():
        raise ProjectError("bad_path", 400)
    try:
        path = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProjectError("disk_path_missing", 404) from exc
    if not path.is_dir():
        raise ProjectError("disk_not_folder", 400)
    if path.name.startswith("."):
        raise ProjectError("disk_hidden", 400)
    if path == Path(path.anchor) or path.parent == path:
        raise ProjectError("disk_root", 400)
    home = Path.home().resolve()
    if path == home:
        raise ProjectError("disk_home", 400)
    try:
        data = hokma_data_dir().resolve()
    except OSError:                                                          # pragma: no cover
        data = hokma_data_dir()
    if inside(path, data):
        raise ProjectError("disk_data_dir", 400)
    if _is_system_folder(path):
        raise ProjectError("disk_system", 400)
    return path


def _same_path(stored: str, resolved: Path) -> bool:
    try:
        return Path(stored).resolve() == resolved
    except (OSError, RuntimeError):
        return os.path.normcase(stored) == os.path.normcase(str(resolved))


def public(folder: ProjectFolder) -> dict[str, Any]:
    return {"id": folder.id, "project_id": folder.project_id, "path": folder.path, "own": is_own(folder),
            "label": None if is_own(folder) else folder.label, "status": folder.status,
            "status_detail": folder.status_detail, "file_count": folder.file_count,
            "last_scan_at": folder.last_scan_at, "created_at": folder.created_at}


# ------------------------------------------------------------------------------------------------ own folder

def ensure_own_folder(project_id: int, db: Database | None = None) -> ProjectFolder | None:
    """Register the project's own folder as one of its read folders. Idempotent; None when the project or its folder
    does not exist. A folder the student already added at that same path becomes the own one."""
    database = _db(db)
    try:
        with database.read() as u:
            project = u.projects.get(project_id)
            rows = u.library.list_project_folders(project_id)
    except NotFound:
        return None
    for row in rows:
        if row.label == OWN_LABEL:
            return row
    path = project.path
    if not path or not os.path.isdir(path):
        return None
    for row in rows:
        if os.path.normcase(row.path) == os.path.normcase(path):
            with database.write() as u:
                u.session.execute("UPDATE project_folders SET label = :l WHERE id = :i", {"l": OWN_LABEL, "i": row.id})
                return u.library.get_project_folder(row.id)
    try:
        with database.write() as u:
            return u.library.add_project_folder(project_id, path, label=OWN_LABEL)
    except (DuplicateName, ValidationError, NotFound):                       # pragma: no cover (a racing caller won)
        return None


def ensure_all_own_folders(db: Database | None = None) -> int:
    """Register the own folder of every project that lacks it (projects created before this existed). Returns how
    many were added."""
    database = _db(db)
    with database.read() as u:
        pending = [p.id for p in u.projects.list()
                   if not any(f.label == OWN_LABEL for f in u.library.list_project_folders(p.id))]
    return sum(1 for pid in pending if ensure_own_folder(pid, database) is not None)


# ------------------------------------------------------------------------------------------------ the API logic

def _require_project(database: Database, project_id: int) -> None:
    try:
        with database.read() as u:
            u.projects.get(project_id)
    except NotFound as exc:
        raise ProjectError("not_found", 404) from exc


def _folder_of(u, project_id: int, folder_id: int) -> ProjectFolder:
    try:
        folder = u.library.get_project_folder(folder_id)
    except NotFound as exc:
        raise ProjectError("disk_folder_not_found", 404) from exc
    if folder.project_id != project_id:
        raise ProjectError("disk_folder_not_found", 404)
    return folder


def list_folders(project_id: int, db: Database | None = None) -> dict[str, Any]:
    database = _db(db)
    _require_project(database, project_id)
    ensure_own_folder(project_id, database)
    with database.read() as u:
        rows = u.library.list_project_folders(project_id)
    folder_worker.request_project(project_id)            # opening the project asks for a fresh pass (section 4.3)
    return {"folders": [public(f) for f in rows], "max_folders": MAX_FOLDERS_PER_PROJECT}


def add_folder(project_id: int, path: object, db: Database | None = None) -> dict[str, Any]:
    """Add a disk folder to a project. 400 for a folder the rules keep out, 404 when it does not exist, 409 when it is
    already there or the project has the five folders it can read."""
    database = _db(db)
    _require_project(database, project_id)
    resolved = check_folder(path)
    ensure_own_folder(project_id, database)
    with database.write() as u:
        existing = u.library.list_project_folders(project_id)
        if any(_same_path(f.path, resolved) for f in existing):
            raise ProjectError("disk_duplicate", 409)
        if sum(1 for f in existing if not is_own(f)) >= MAX_FOLDERS_PER_PROJECT:
            raise ProjectError("disk_too_many", 409, limit=MAX_FOLDERS_PER_PROJECT)
        try:
            folder = u.library.add_project_folder(project_id, str(resolved))
        except DuplicateName as exc:                                         # pragma: no cover (checked above)
            raise ProjectError("disk_duplicate", 409) from exc
    worker = folder_worker.get_worker()
    if worker is not None:
        worker.request(folder.id)
    return public(folder)


def remove_folder(project_id: int, folder_id: int, db: Database | None = None) -> dict[str, Any]:
    """Stop reading a folder and drop what was indexed from it. The files on the disk are not touched. The project's
    own folder cannot be removed: it comes back by itself."""
    database = _db(db)
    _require_project(database, project_id)
    with database.write() as u:
        folder = _folder_of(u, project_id, folder_id)
        if is_own(folder):
            raise ProjectError("disk_own_folder", 400)
        u.library.remove_project_folder(folder_id)
    return {"removed": True}


def rescan_folder(project_id: int, folder_id: int, db: Database | None = None) -> dict[str, Any]:
    """Read the folder again right now and return it with its new counts."""
    from hokma.docs import folder_scan
    database = _db(db)
    _require_project(database, project_id)
    with database.read() as u:
        _folder_of(u, project_id, folder_id)
    folder_scan.scan_folder(folder_id, database)
    with database.read() as u:
        return public(u.library.get_project_folder(folder_id))

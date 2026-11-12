"""Folders of the student Library (SPEC-010 section 4.8): list a level, create, rename and remove. The tree has at most
8 levels, sibling names are unique without regard to case, and removing a folder that holds something needs a
confirmation that carries the counts."""
from pathlib import Path
from typing import Any

from hokma.db import Database, NotFound
from hokma.db.errors import DuplicateName, ValidationError
from hokma.db.models import LibraryFolder
from hokma.db.repos.library import MAX_DEPTH, clean_folder_name
from hokma.docs.ingest import _db, _remove_file, _require_folder
from hokma.projects.errors import ProjectError


def _present(u, folder: LibraryFolder) -> dict[str, Any]:
    """The folder as the API shows it: its place in the tree, its path and what removing it would delete."""
    contents = u.library.folder_contents(folder.id)
    return {"id": folder.id, "parent_id": folder.parent_id, "name": folder.name,
            "path": u.library.folder_path(folder.id), "created_at": folder.created_at,
            "documents": contents["documents"], "subfolders": contents["folders"]}


def _clean(name: str) -> str:
    try:
        return clean_folder_name(name)
    except ValidationError as exc:
        raise ProjectError("folder_bad_name", 400) from exc


def list_folders(parent_id: int | None = None, db: Database | None = None) -> dict[str, Any]:
    """The folders directly under `parent_id` (the top level when None), with the breadcrumbs of that parent."""
    database = _db(db)
    with database.write() as u:
        library = u.library.ensure_student_library()
        _require_folder(u, library.id, parent_id)
        level = [f for f in u.library.list_folders(library.id) if f.parent_id == parent_id]
        level.sort(key=lambda f: (f.name.casefold(), f.id))
        crumbs = [] if parent_id is None else [{"id": f.id, "name": f.name} for f in u.library.folder_chain(parent_id)]
        return {"folders": [_present(u, f) for f in level], "path": crumbs}


def create_folder(name: str, parent_id: int | None = None, db: Database | None = None) -> dict[str, Any]:
    """New folder at the top level or under `parent_id`. 404 parent missing, 400 bad name or deeper than 8 levels,
    409 name already used among its siblings."""
    database = _db(db)
    name = _clean(name)
    with database.write() as u:
        library = u.library.ensure_student_library()
        _require_folder(u, library.id, parent_id)
        if parent_id is not None and len(u.library.folder_chain(parent_id)) + 1 > MAX_DEPTH:
            raise ProjectError("folder_too_deep", 400)
        try:
            folder = u.library.create_folder(library.id, name, parent_id=parent_id)
        except DuplicateName as exc:
            raise ProjectError("duplicate_name", 409) from exc
        return _present(u, folder)


def rename_folder(folder_id: int, name: str, db: Database | None = None) -> dict[str, Any]:
    """Rename keeping the folder where it is. 404 missing folder, 400 bad name, 409 a sibling already has the name."""
    database = _db(db)
    name = _clean(name)
    with database.write() as u:
        library = u.library.ensure_student_library()
        _require_folder(u, library.id, folder_id)
        try:
            folder = u.library.rename_folder(folder_id, name)
        except DuplicateName as exc:
            raise ProjectError("duplicate_name", 409) from exc
        except NotFound as exc:                                                       # pragma: no cover (checked above)
            raise ProjectError("lib_folder_not_found", 404) from exc
        return _present(u, folder)


def remove_folder(folder_id: int, confirm: bool = False, db: Database | None = None) -> dict[str, Any]:
    """Remove an empty folder, or one with contents only when `confirm` is true: then it goes with its subfolders and
    their documents, and the stored originals are deleted after the rows. Without `confirm`, a folder with contents
    answers 409 with the counts and nothing changes."""
    database = _db(db)
    with database.write() as u:
        library = u.library.ensure_student_library()
        _require_folder(u, library.id, folder_id)
        contents = u.library.folder_contents(folder_id)
        counts = {"documents": contents["documents"], "subfolders": contents["folders"]}
        if (counts["documents"] or counts["subfolders"]) and not confirm:
            raise ProjectError("folder_not_empty", 409, extra=counts, **counts)
        removed = u.library.remove_folder(folder_id)
    files_removed = sum(1 for doc in removed if _remove_file(Path(doc.stored_path)))
    return {"removed": True, **counts, "files_removed": files_removed}

"""Data steps that run inside a migration's transaction, right after its SQL (SPEC-010 P2).

A step is a Python function registered for a schema version. It receives the open write session and returns the
files that became useless; the migration deletes them only after the transaction committed, so a rollback never
loses a file. Steps are idempotent: running one on data that is already migrated changes nothing.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from hokma.db.repos.library import LibraryRepo
from hokma.i18n import t

MAX_DESCRIPTION_CHARS = 500


def move_documents_to_student_library(session) -> list[Path]:
    """Version 8: every document a project library holds (and that does not come from a disk folder) moves to the
    student's single Library, remembering its project in `origin_project_id`.

    When two documents share a sha256 the oldest one stays (a copy the Library already holds always stays), its
    description records the other project, and the repeated row goes (its passages and vectors follow by cascade). Stored files never move;
    the file of a repeated row is returned so the caller can delete it after the commit. Library folders stay
    empty: migrated documents sit at the top level."""
    rows = session.query(
        "SELECT d.id, d.sha256, d.stored_path, d.created_at, l.project_id, p.name AS project_name "
        "FROM documents d JOIN libraries l ON l.id = d.library_id AND l.scope = 'project' "
        "LEFT JOIN projects p ON p.id = l.project_id "
        "WHERE d.folder_id IS NULL ORDER BY d.created_at, d.id")
    if not rows:
        return []                                                 # nothing to move: the Library is created on first use
    library = LibraryRepo(session).ensure_student_library()
    # sha256 -> document that stays; walked newest first so the oldest overwrites and wins
    kept: dict[str, int] = {r["sha256"]: r["id"] for r in reversed(session.query(
        "SELECT id, sha256 FROM documents WHERE library_id = :l ORDER BY created_at, id", {"l": library.id}))}
    leftovers: list[Path] = []
    for row in rows:
        twin = kept.get(row["sha256"])
        if twin is None:
            session.execute("UPDATE documents SET library_id = :l, origin_project_id = :p WHERE id = :d",
                            {"l": library.id, "p": row["project_id"], "d": row["id"]})
            kept[row["sha256"]] = row["id"]
            continue
        _note_other_project(session, twin, row["project_name"] or "")
        session.execute("DELETE FROM documents WHERE id = :d", {"d": row["id"]})
        leftovers.append(Path(row["stored_path"]))
    return leftovers


def _note_other_project(session, document_id: int, project_name: str) -> None:
    """Append "Also uploaded from {project}" to the description of the document that stays."""
    note = t("library.migrate.also_uploaded", project=project_name)
    current = session.scalar("SELECT description FROM documents WHERE id = :d", {"d": document_id}) or ""
    if note in current:
        return
    text = f"{current}; {note}" if current else note
    session.execute("UPDATE documents SET description = :x WHERE id = :d",
                    {"x": text[:MAX_DESCRIPTION_CHARS], "d": document_id})


def remove_stored_files(paths: list[Path]) -> list[Path]:
    """Delete stored originals that live under the libraries folder; returns the ones that could not be removed."""
    from hokma.docs.ingest import libraries_dir

    failed: list[Path] = []
    try:
        root = libraries_dir().resolve()
    except OSError:
        return list(paths)
    for path in paths:
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            failed.append(path)                                   # outside the libraries folder: never touched
            continue
        try:
            resolved.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            failed.append(path)
    return failed


STEPS: dict[int, Callable[[object], list[Path]]] = {8: move_documents_to_student_library}

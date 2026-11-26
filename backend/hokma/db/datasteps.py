"""Data steps that run inside a migration's transaction, right after its SQL (SPEC-010 P2).

A step is a Python function registered for a schema version. It receives the open write session and returns the
files that became useless; the migration deletes them only after the transaction committed, so a rollback never
loses a file. Steps are idempotent: running one on data that is already migrated changes nothing.
"""
from __future__ import annotations

import re
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


_CAPITAL_NOT_NULL = re.compile(r"(\bcapital\s+INTEGER)\s+NOT\s+NULL", re.IGNORECASE)


def _rewrite_desks_definition(session, change) -> bool:
    """Change the stored text of the definition of `desks` in SQLite and bump the schema cookie (SQLite manual, "Making Other Kinds Of
    Table Schema Changes": allowed for adding or removing a NOT NULL, a CHECK or a default). The data is not touched. Returns False when
    the text already has the wanted shape."""
    sql = session.scalar("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'desks'")
    if sql is None:
        raise ValueError("the table desks does not exist")
    new = change(sql)
    if new == sql:
        return False
    cookie = int(session.scalar("PRAGMA schema_version"))
    session.execute("PRAGMA writable_schema = ON")
    try:
        session.execute("UPDATE sqlite_master SET sql = :s WHERE type = 'table' AND name = 'desks'", {"s": new})
        session.execute(f"PRAGMA schema_version = {cookie + 1}")
    finally:
        session.execute("PRAGMA writable_schema = OFF")
    return True


def desk_capital_optional(session) -> list[Path]:
    """Version 25: `desks.capital` accepts NULL. Postgres does it in the SQL of the migration; here SQLite. Idempotent."""
    if session.engine == "sqlite":
        _rewrite_desks_definition(session, lambda sql: _CAPITAL_NOT_NULL.sub(r"\1", sql, count=1))
        if any(r["name"] == "capital" and r["notnull"] for r in session.query("PRAGMA table_info(desks)")):
            raise ValueError("desks.capital is still NOT NULL after the rewrite")
    return []


def assets_list_to_notice(session) -> list[Path]:
    """Version 27 (SPEC-026): the list of assets that the SYSTEM forced into a desk stops blocking and becomes a notice. A desk whose
    `guards` lists `whitelist` gets `rule_modes.assets_only = notice`, unless the owner set it by hand (`rule_sources.assets_only` says so, or `assets_only` is true in the limits):
    that one is written as an explicit restriction. A desk that already has an entry is left alone (idempotent). Every desk it changes
    leaves a row in `rule_mode_migrations`; the way back (hokma.db.downgrade.rollback_0027) takes the entries out and the list blocks again."""
    import json

    for row in session.query("SELECT desk_id, config FROM desk_limits ORDER BY desk_id"):
        try:
            config = json.loads(row["config"] or "{}")
        except ValueError:
            continue
        guards = config.get("guards")
        if not isinstance(guards, list) or "whitelist" not in guards:
            continue
        modes = dict(config.get("rule_modes") or {})
        if "assets_only" in modes:
            continue
        # the system only forces the list through `guards`; `assets_only: true` is a limit the owner set, with or without a record of its source
        by_hand = config.get("assets_only") is True or (config.get("rule_sources") or {}).get("assets_only") == "hand"
        after = "restriction" if by_hand else "notice"
        modes["assets_only"] = {"mode": after, **({} if by_hand else {"timeout_min": 30})}
        config["rule_modes"] = modes
        session.execute("UPDATE desk_limits SET config = :c WHERE desk_id = :d", {"c": json.dumps(config, ensure_ascii=False), "d": row["desk_id"]})
        session.execute("INSERT INTO rule_mode_migrations (desk_id, rule_id, before_mode, after_mode, reason) VALUES (:d, 'assets_only', 'restriction', :a, :r)",
                        {"d": row["desk_id"], "a": after, "r": "set by hand: kept" if by_hand else "forced by the system: now a notice"})
    return []


STEPS: dict[int, Callable[[object], list[Path]]] = {8: move_documents_to_student_library, 25: desk_capital_optional, 27: assets_list_to_notice}

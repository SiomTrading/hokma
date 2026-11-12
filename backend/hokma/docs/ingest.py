"""Ingestion of project documents (SPEC-009 P4): store the original, read it into passages and index it.

Everything runs inside the request, one file at a time. The original is kept under
<data>/libraries/{library_id}/{document_id}-{safe_name}, which is the only place this module writes or deletes.
"""
import hashlib
import os
import threading
from pathlib import Path
from typing import Any

from hokma.core.config import hokma_data_dir
from hokma.db import Database, NotFound, get_database
from hokma.db.errors import DuplicateDocument
from hokma.db.models import Document
from hokma.docs.readers import ReadResult, UnreadableDocument, UnsupportedFormat, detect_format, read_document
from hokma.engine.attachments import safe_name, sniff_mime
from hokma.i18n import t
from hokma.projects.errors import ProjectError

MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
MAX_DESCRIPTION_CHARS = 500
MAX_NAME_CHARS = 200
_LOCK = threading.Lock()


def libraries_dir() -> Path:
    return hokma_data_dir() / "libraries"


def clean_display_name(name: str) -> str:
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    base = "".join(ch for ch in base if ch.isprintable())[:MAX_NAME_CHARS].strip()
    return base or "file"


def clean_description(text: str | None) -> str | None:
    cleaned = " ".join((text or "").split())[:MAX_DESCRIPTION_CHARS]
    return cleaned or None


def public(doc: Document, *, library_path: str = "", origin_project: str | None = None) -> dict[str, Any]:
    """What the API shows of a document. The stored path and the hash stay inside. `library_path` is the Library
    folder that holds it ("" = top level) and `origin_project` the name of the project it was first added from."""
    return {
        "id": doc.id, "name": doc.name, "format": doc.format, "mime_type": doc.mime_type, "size_bytes": doc.size_bytes,
        "status": doc.status, "status_detail": doc.status_detail, "chunk_count": doc.chunk_count,
        "origin": "link" if doc.source_url else "file", "source_url": doc.source_url, "description": doc.description,
        "created_at": doc.created_at, "indexed_at": doc.indexed_at,
        "library_folder_id": doc.library_folder_id, "library_path": library_path,
        "origin_project_id": doc.origin_project_id, "origin_project": origin_project,
    }


def _detail(result: ReadResult) -> str | None:
    parts = []
    if result.truncated:
        kind = result.truncated.kind
        reached = result.truncated.reached
        if kind == "pages":
            parts.append(t("docs.detail.limit_pages", reached=reached))
        elif kind == "passages":
            parts.append(t("docs.detail.limit_passages", reached=reached))
        else:
            parts.append(t("docs.detail.limit_time", reached=reached))
    if result.notice:
        parts.append(t("docs.detail.no_text"))
    return " ".join(parts) or None


def _db(db: Database | None) -> Database:
    return db or get_database()


def _require_project(db: Database, project_id: int) -> None:
    try:
        with db.read() as u:
            u.projects.get(project_id)
    except NotFound as exc:
        raise ProjectError("not_found", 404) from exc


def _write_original(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, target)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _remove_file(path: Path) -> bool:
    """Delete a stored original, only when it lives under the libraries folder. False when it could not be removed."""
    try:
        resolved = path.resolve()
        resolved.relative_to(libraries_dir().resolve())
    except (OSError, ValueError):
        return False
    try:
        resolved.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _present(database: Database, docs: list[Document]) -> list[dict[str, Any]]:
    """Documents as the API shows them, with their Library folder path and the name of their origin project."""
    if not docs:
        return []
    with database.read() as u:
        paths: dict[int, str] = {}
        for fid in {d.library_folder_id for d in docs if d.library_folder_id is not None}:
            try:
                paths[fid] = u.library.folder_path(fid)
            except NotFound:                                                  # pragma: no cover (cascade removes them)
                paths[fid] = ""
        names: dict[int, str] = {}
        for pid in {d.origin_project_id for d in docs if d.origin_project_id is not None}:
            row = u.session.one("SELECT name FROM projects WHERE id = :p", {"p": pid})
            if row:
                names[pid] = row["name"]
    return [public(d, library_path=paths.get(d.library_folder_id or 0, ""), origin_project=names.get(d.origin_project_id or 0))
            for d in docs]


def _require_folder(u, library_id: int, folder_id: int | None) -> None:
    """A Library folder that exists and belongs to the student Library; anything else is a 404."""
    if folder_id is None:
        return
    try:
        folder = u.library.get_folder(folder_id)
    except NotFound as exc:
        raise ProjectError("lib_folder_not_found", 404) from exc
    if folder.library_id != library_id:
        raise ProjectError("lib_folder_not_found", 404)


def _library_document(u, library_id: int, document_id: int) -> Document:
    """A document of the student Library; a missing one and one from another library are the same 404."""
    try:
        doc = u.documents.get_document(document_id)
    except NotFound as exc:
        raise ProjectError("doc_not_found", 404) from exc
    if doc.library_id != library_id:
        raise ProjectError("doc_not_found", 404)
    return doc


def list_library_documents(folder_id: int | None = None, db: Database | None = None) -> dict[str, Any]:
    """Every document of the Library, or only those directly inside `folder_id` (that level, not its subfolders)."""
    database = _db(db)
    with database.write() as u:
        library = u.library.ensure_student_library()
        _require_folder(u, library.id, folder_id)
        docs = u.documents.list_documents(library.id)
    if folder_id is not None:
        docs = [d for d in docs if d.library_folder_id == folder_id]
    return {"documents": _present(database, docs)}


def add_library_document(name: str, data: bytes, *, description: str | None = None, source_url: str | None = None,
                         folder_id: int | None = None, origin_project_id: int | None = None,
                         db: Database | None = None) -> dict[str, Any]:
    """Store, read and index one file in the student Library, inside `folder_id` (None = top level). Raises
    ProjectError: 404 (folder), 400 (empty), 409 (same content already there), 413 (over 50 MB) or 415 (format).
    A file that cannot be read is kept as a `failed` document with its reason."""
    database = _db(db)

    def target(u):
        library = u.library.ensure_student_library()
        _require_folder(u, library.id, folder_id)
        return library

    return _ingest(database, target, name, data, description=description, source_url=source_url,
                   fields={"origin_project_id": origin_project_id, "library_folder_id": folder_id})


def add_document(project_id: int, name: str, data: bytes, *, description: str | None = None,
                 source_url: str | None = None, db: Database | None = None) -> dict[str, Any]:
    """Same as `add_library_document` but into the per-project library of 0.0.26. Only the agent tools still search
    that library (SPEC-010 P7 moves them to the Library); the HTTP API no longer writes here."""
    database = _db(db)
    _require_project(database, project_id)
    return _ingest(database, lambda u: u.documents.create_project_library(project_id), name, data,
                   description=description, source_url=source_url, fields={})


def _ingest(database: Database, target, name: str, data: bytes, *, description: str | None, source_url: str | None,
            fields: dict[str, Any]) -> dict[str, Any]:
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ProjectError("doc_too_large", 413)
    display = clean_display_name(name)
    fmt = detect_format(display)
    if fmt is None:
        raise ProjectError("doc_unsupported", 415)
    if not data:
        raise ProjectError("empty_file", 400)
    digest = hashlib.sha256(data).hexdigest()
    note = clean_description(description)

    with _LOCK:
        with database.write() as u:
            library = target(u)
            if u.documents.find_by_sha256(library.id, digest):
                raise ProjectError("doc_duplicate", 409)
            try:
                doc = u.documents.add_document(
                    library.id, name=display, stored_path=f"pending/{digest}", mime_type=sniff_mime(data, ""),
                    format=fmt, size_bytes=len(data), sha256=digest, source_url=source_url, description=note,
                    status="indexing", **fields)
            except DuplicateDocument as exc:
                raise ProjectError("doc_duplicate", 409) from exc

    stored = libraries_dir() / str(doc.library_id) / f"{doc.id}-{safe_name(display)}"
    try:
        _write_original(stored, data)
        with database.write() as u:
            u.documents.set_stored_path(doc.id, str(stored))
        result = read_document(stored, description=note)
    except (UnreadableDocument, UnsupportedFormat) as exc:
        return _finish_failed(database, doc.id, t("docs.detail.unreadable", reason=t(f"docs.detail.reason.{exc.code}")))
    except Exception:
        # Anything unexpected must not leave the row stuck on "indexing": the student sees it failed and can remove it.
        _finish_failed(database, doc.id, t("docs.detail.failed"))
        raise
    with database.write() as u:
        u.documents.add_chunks(doc.id, result.passages)
        saved = u.documents.set_status(doc.id, "ready", _detail(result))
    from hokma.docs import folder_worker
    folder_worker.wake()                       # the vectors of the new passages are computed now, not at the next poll
    return _present(database, [saved])[0]


def _finish_failed(database: Database, document_id: int, detail: str) -> dict[str, Any]:
    with database.write() as u:
        saved = u.documents.set_status(document_id, "failed", detail)
    return _present(database, [saved])[0]


def remove_library_document(document_id: int, db: Database | None = None) -> dict[str, Any]:
    """Delete the rows (document, passages, search entries) and then the stored original. Only a document of the
    student Library can be reached; any other id is a 404."""
    database = _db(db)
    with database.write() as u:
        library = u.library.ensure_student_library()
        doc = _library_document(u, library.id, document_id)
        u.documents.remove_document(document_id)
    return {"removed": True, "file_removed": _remove_file(Path(doc.stored_path))}


def move_library_document(document_id: int, folder_id: int | None, db: Database | None = None) -> dict[str, Any]:
    """Put a document in a Library folder (None = top level). 404 when the document or the folder does not exist,
    409 when that folder already holds a document with the same name."""
    database = _db(db)
    with database.write() as u:
        library = u.library.ensure_student_library()
        doc = _library_document(u, library.id, document_id)
        _require_folder(u, library.id, folder_id)
        wanted = doc.name.casefold()
        for other in u.documents.list_documents(library.id):
            if other.id != doc.id and other.library_folder_id == folder_id and other.name.casefold() == wanted:
                raise ProjectError("doc_name_taken", 409)
        saved = u.library.move_document(document_id, folder_id)
    return _present(database, [saved])[0]

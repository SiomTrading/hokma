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


def public(doc: Document) -> dict[str, Any]:
    """What the API shows of a document. The stored path and the hash stay inside."""
    return {
        "id": doc.id, "name": doc.name, "format": doc.format, "mime_type": doc.mime_type, "size_bytes": doc.size_bytes,
        "status": doc.status, "status_detail": doc.status_detail, "chunk_count": doc.chunk_count,
        "origin": "link" if doc.source_url else "file", "source_url": doc.source_url, "description": doc.description,
        "created_at": doc.created_at, "indexed_at": doc.indexed_at,
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


def list_documents(project_id: int, db: Database | None = None) -> dict[str, Any]:
    database = _db(db)
    _require_project(database, project_id)
    with database.read() as u:
        library = u.documents.find_project_library(project_id)
        docs = u.documents.list_documents(library.id) if library else []
    return {"documents": [public(d) for d in docs]}


def add_document(project_id: int, name: str, data: bytes, *, description: str | None = None,
                 source_url: str | None = None, db: Database | None = None) -> dict[str, Any]:
    """Store, read and index one file. Raises ProjectError: 404 (project), 400 (empty), 409 (duplicate),
    413 (over 50 MB) or 415 (format). A file that cannot be read is kept as a `failed` document with its reason."""
    database = _db(db)
    _require_project(database, project_id)
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
            library = u.documents.create_project_library(project_id)
            if u.documents.find_by_sha256(library.id, digest):
                raise ProjectError("doc_duplicate", 409)
            try:
                doc = u.documents.add_document(
                    library.id, name=display, stored_path=f"pending/{digest}", mime_type=sniff_mime(data, ""),
                    format=fmt, size_bytes=len(data), sha256=digest, source_url=source_url, description=note,
                    status="indexing")
            except DuplicateDocument as exc:
                raise ProjectError("doc_duplicate", 409) from exc

    target = libraries_dir() / str(doc.library_id) / f"{doc.id}-{safe_name(display)}"
    try:
        _write_original(target, data)
        with database.write() as u:
            u.documents.set_stored_path(doc.id, str(target))
        result = read_document(target, description=note)
    except (UnreadableDocument, UnsupportedFormat) as exc:
        return _finish_failed(database, doc.id, t("docs.detail.unreadable", reason=t(f"docs.detail.reason.{exc.code}")))
    except Exception:
        # Anything unexpected must not leave the row stuck on "indexing": the student sees it failed and can remove it.
        _finish_failed(database, doc.id, t("docs.detail.failed"))
        raise
    with database.write() as u:
        u.documents.add_chunks(doc.id, result.passages)
        saved = u.documents.set_status(doc.id, "ready", _detail(result))
    return public(saved)


def _finish_failed(database: Database, document_id: int, detail: str) -> dict[str, Any]:
    with database.write() as u:
        return public(u.documents.set_status(document_id, "failed", detail))


def remove_document(project_id: int, document_id: int, db: Database | None = None) -> dict[str, Any]:
    """Delete the rows (document, passages, search entries) and then the stored original. The document must belong
    to this project's library; any other id is a 404 so one project cannot reach another one's files."""
    database = _db(db)
    _require_project(database, project_id)
    with database.write() as u:
        library = u.documents.find_project_library(project_id)
        try:
            doc = u.documents.get_document(document_id)
        except NotFound as exc:
            raise ProjectError("doc_not_found", 404) from exc
        if library is None or doc.library_id != library.id:
            raise ProjectError("doc_not_found", 404)
        u.documents.remove_document(document_id)
    return {"removed": True, "file_removed": _remove_file(Path(doc.stored_path))}

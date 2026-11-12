"""One pass over a project's disk folder (SPEC-010 section 4.3): index what is new, reindex what changed in content,
forget what disappeared. Files are read where they are; nothing is copied.

A file is compared by `(size, mtime_ns)` first and by sha256 only when that differs, so a file that was touched but not
changed is not read again. Documents of a folder live in the project's own library with
`stored_path = 'folder:{folder_id}:{rel_path}'`.
"""
import hashlib
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from hokma.core.config import hokma_data_dir
from hokma.db import Database, NotFound
from hokma.db.errors import DuplicateDocument
from hokma.db.models import Document, ProjectFolder
from hokma.docs import ingest
from hokma.docs.diskfolders import check_folder, inside, is_own, is_skipped_dir
from hokma.docs.readers import UnreadableDocument, UnsupportedFormat, detect_format, read_document
from hokma.engine.attachments import sniff_mime
from hokma.i18n import t
from hokma.projects.errors import ProjectError

MAX_FILES_PER_FOLDER = 5000
MAX_PASSAGES = 50_000                       # passages from disk folders in the whole installation
SCAN_LOCK = threading.RLock()               # one pass at a time: the worker thread and "Rescan" never overlap


@dataclass
class ScanResult:
    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0
    skipped_large: int = 0
    skipped_over_cap: int = 0
    duplicates: int = 0
    passage_cap: bool = False
    status: str = "ready"

    def public(self) -> dict:
        return dict(self.__dict__)


def stored_path(folder_id: int, rel_path: str) -> str:
    return f"folder:{folder_id}:{rel_path}"


def walk_supported(root: Path, *, limit: int = MAX_FILES_PER_FOLDER) -> tuple[dict[str, tuple[int, int]], int, int]:
    """Supported files under `root` as `{rel_path: (size, mtime_ns)}`, how many were over the limit and how many
    were over 50 MB. Hidden entries, `.git`, dependency folders, links and Hokma's data folder are left out."""
    try:
        data = hokma_data_dir().resolve()
    except OSError:                                                          # pragma: no cover
        data = hokma_data_dir()
    found: dict[str, tuple[int, int]] = {}
    over_cap = too_large = 0
    stack: list[tuple[Path, str]] = [(root, "")]
    while stack:
        folder, rel = stack.pop()
        try:
            entries = sorted(os.scandir(folder), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if name.startswith(".") or name.startswith("~$"):
                continue
            try:
                if entry.is_symlink():
                    continue
                child_rel = f"{rel}/{name}" if rel else name
                if entry.is_dir(follow_symlinks=False):
                    if is_skipped_dir(name):
                        continue
                    try:
                        if inside(Path(entry.path).resolve(), data):
                            continue
                    except OSError:
                        continue
                    stack.append((Path(entry.path), child_rel))
                    continue
                if not entry.is_file(follow_symlinks=False) or detect_format(name) is None:
                    continue
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if st.st_size <= 0:
                continue
            if st.st_size > ingest.MAX_DOCUMENT_BYTES:
                too_large += 1
                continue
            if len(found) >= limit:
                over_cap += 1
                continue
            found[child_rel] = (st.st_size, st.st_mtime_ns)
    return found, over_cap, too_large


def _stays_inside(root: Path, path: Path) -> bool:
    """The file is still a plain file whose real location is under the folder: no link, and no folder on the way
    swapped for a link since the walk. Checked right before each read."""
    try:
        if path.is_symlink():
            return False
        return inside(path.resolve(strict=True), root.resolve(strict=True))
    except (OSError, RuntimeError):
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _forget(database: Database, doc: Document) -> None:
    with ingest._LOCK:
        with database.write() as u:
            u.documents.remove_document(doc.id)


def _index(database: Database, folder: ProjectFolder, library_id: int, rel: str, path: Path,
           size: int, mtime_ns: int, digest: str) -> int | None:
    """Add one file as a document and read it. Returns how many passages it added, or None when another document of
    the library already has that content."""
    display = ingest.clean_display_name(Path(rel).name)
    fmt = detect_format(display) or "text"
    with ingest._LOCK:
        with database.write() as u:
            try:
                doc = u.documents.add_document(
                    library_id, name=display, stored_path=stored_path(folder.id, rel), mime_type=sniff_mime(b"", display),
                    format=fmt, size_bytes=size, sha256=digest, status="indexing", folder_id=folder.id, rel_path=rel,
                    mtime_ns=mtime_ns, origin_project_id=folder.project_id)
            except DuplicateDocument:
                return None
    try:
        result = read_document(path)
    except (UnreadableDocument, UnsupportedFormat) as exc:
        with database.write() as u:
            u.documents.set_status(doc.id, "failed", t("docs.detail.unreadable", reason=t(f"docs.detail.reason.{exc.code}")))
        return 0
    except Exception:
        with database.write() as u:
            u.documents.set_status(doc.id, "failed", t("docs.detail.failed"))
        return 0
    with database.write() as u:
        u.documents.add_chunks(doc.id, result.passages)
        u.documents.set_status(doc.id, "ready", ingest._detail(result))
    return len(result.passages)


def _root_is_safe(folder: ProjectFolder, root: Path) -> bool:
    """The folder is still what was accepted: not turned into a link, and still passing the rules of section 4.3 (the
    project's own folder only has to be a real folder, not a link)."""
    if root.is_symlink():
        return False
    if is_own(folder):
        return True
    try:
        return check_folder(str(root)) == root
    except ProjectError:
        return False


def total_passages(database: Database) -> int:
    with database.read() as u:
        return int(u.session.scalar("SELECT COUNT(*) FROM doc_chunks") or 0)


def scan_folder(folder_id: int, db: Database | None = None) -> ScanResult:
    """One pass over a folder. A folder that is no longer on the disk becomes `missing` and keeps its passages.
    Passes are serialized: a second caller waits for the one in progress."""
    with SCAN_LOCK:
        return _scan_folder(folder_id, ingest._db(db))


def _scan_folder(folder_id: int, database: Database) -> ScanResult:
    with database.read() as u:
        folder = u.library.get_project_folder(folder_id)
        own_library = u.documents.find_project_library(folder.project_id)
        existing = [d for d in u.documents.list_documents(own_library.id) if d.folder_id == folder_id] if own_library else []
    root = Path(folder.path)
    result = ScanResult()
    if root.is_dir() and not _root_is_safe(folder, root):
        with database.write() as u:
            u.library.set_project_folder_status(folder_id, "failed", t("docs.disk.unsafe"))
        result.status = "failed"
        return result
    if not root.is_dir():
        with database.write() as u:
            u.library.set_project_folder_status(folder_id, "missing", t("docs.disk.missing"))
        result.status = "missing"
        return result
    with database.write() as u:
        u.library.set_project_folder_status(folder_id, "scanning")
        library = u.documents.create_project_library(folder.project_id)
    seen, result.skipped_over_cap, result.skipped_large = walk_supported(root)
    passages = total_passages(database)
    by_rel = {d.rel_path: d for d in existing if d.rel_path}
    for rel, doc in by_rel.items():
        if rel not in seen:
            _forget(database, doc)
            result.removed += 1
    for rel, (size, mtime_ns) in seen.items():
        path = root / rel
        if not _stays_inside(root, path):
            continue
        doc = by_rel.get(rel)
        if doc is not None and doc.size_bytes == size and doc.mtime_ns == mtime_ns:
            result.unchanged += 1
            continue
        try:
            digest = _sha256(path)
        except OSError:
            continue
        if doc is not None and doc.sha256 == digest:
            with database.write() as u:
                u.session.execute("UPDATE documents SET mtime_ns = :m, size_bytes = :s WHERE id = :i",
                                  {"m": mtime_ns, "s": size, "i": doc.id})
            result.unchanged += 1
            continue
        if passages >= MAX_PASSAGES:
            result.passage_cap = True
            break
        if doc is not None:
            passages -= doc.chunk_count
            _forget(database, doc)
        added = _index(database, folder, library.id, rel, path, size, mtime_ns, digest)
        if added is None:
            result.duplicates += 1
        else:
            passages += added
            result.updated += doc is not None
            result.added += doc is None
    with database.write() as u:
        count = int(u.session.scalar("SELECT count(*) FROM documents WHERE folder_id = :f", {"f": folder_id}))
        detail = (t("docs.disk.passage_cap", limit=f"{MAX_PASSAGES:,}") if result.passage_cap else
                  t("docs.disk.over_cap", count=result.skipped_over_cap) if result.skipped_over_cap else None)
        u.library.set_project_folder_status(folder_id, "ready", detail, file_count=count, scanned=True)
    return result

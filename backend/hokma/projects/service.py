"""Project logic: folders on disk, rows in the database (`projects`, `attachments`) and the project's own Git."""
import hashlib
import os
import threading
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hokma.core.config import hokma_data_dir, hokma_home
from hokma.db import Database, DbError, IntegrityViolation, NotFound, get_database
from hokma.db.repos.projects import KINDS, slugify
from hokma.engine import attachments as att
from hokma.i18n import t
from hokma.kit import safefs
from hokma.projects import fetch as fetcher
from hokma.projects import gitops
from hokma.projects.errors import ProjectError
from hokma.projects.paths import clean_filename, clean_rel, file_kind, project_root, subfolder_for

SUBFOLDERS = ("contexto", "documentos", "imagenes", "audios", "codigo", "resultados")
TREE_MAX_DEPTH = 6
TREE_MAX_ENTRIES = 2000
PREVIEW_TEXT_BYTES = 200 * 1024
HIDDEN = {".git", ".gitkeep"}
_LOCK = threading.RLock()


def _db(db: Database | None) -> Database:
    return db or get_database()


# ---- locations ------------------------------------------------------------------------------

def workdir(db: Database | None = None) -> Path:
    with _db(db).read() as u:
        configured = u.profile.get_or_default().workdir
    return Path(configured) if configured else hokma_home()


def projects_root(db: Database | None = None) -> Path:
    return workdir(db) / "projects"


def _row(db: Database, project_id: int):
    try:
        with db.read() as u:
            return u.projects.get(project_id)
    except NotFound as exc:
        raise ProjectError("not_found", 404) from exc


def _dir(project) -> Path:
    return project_root(project.path)


# ---- active project -------------------------------------------------------------------------

def _legacy_file() -> Path:
    """The file older versions kept the active project in. It is read once and then removed."""
    return hokma_data_dir() / "active-project"


def _import_legacy(database: Database) -> None:
    """Move the active project from the old file into `profile.active_project_id`, one time."""
    legacy = _legacy_file()
    try:
        wanted = int(legacy.read_text(encoding="ascii").strip())
    except OSError:
        return
    except ValueError:
        wanted = None
    with database.write() as u:
        profile = u.profile.get()
        if profile is not None and profile.active_project_id is None and wanted is not None \
                and u.projects.find(wanted) is not None:
            u.profile.set_active_project(wanted)
        elif profile is None:
            return                                    # nothing to store it in yet: keep the file for later
    try:
        legacy.unlink()
    except OSError:
        pass


def active_id(db: Database | None = None) -> int | None:
    """The project the chat uses when a conversation names none: the last one picked, else the oldest."""
    database = _db(db)
    _import_legacy(database)
    with database.read() as u:
        wanted = u.profile.get_or_default().active_project_id
        if wanted is not None and u.projects.find(wanted) is not None:
            return wanted
        rows = u.projects.list(status="active")
    return min((p.id for p in rows), default=None)


def activate(project_id: int, db: Database | None = None) -> int:
    database = _db(db)
    _row(database, project_id)
    _import_legacy(database)
    with database.write() as u:
        u.profile.set_active_project(project_id)
    return project_id


# ---- create, list, rename -------------------------------------------------------------------

def _git_state(path: Path) -> dict[str, Any]:
    state = gitops.repo_state(path)
    return {"ok": state.ok, "reason": state.reason}


def _count_files(root: Path) -> int:
    total = 0
    stack = [root]
    while stack and total < 100_000:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    if e.name in HIDDEN or e.is_symlink():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        stack.append(Path(e.path))
                    elif e.is_file(follow_symlinks=False):
                        total += 1
        except OSError:
            continue
    return total


def _public(project, active: int | None) -> dict[str, Any]:
    path = Path(project.path)
    exists = path.is_dir() and not path.is_symlink()
    return {"id": project.id, "name": project.name, "slug": project.slug, "kind": project.kind,
            "path": project.path, "active": project.id == active, "exists": exists,
            "files": _count_files(path) if exists else 0, "git": _git_state(path) if exists else
            {"ok": False, "reason": "no_repo"}}


def _unique_slug(db: Database, root: Path, base: str) -> str:
    n, slug = 1, base
    while True:
        with db.read() as u:
            taken = u.projects.get_by_slug(slug) is not None
        if not taken and not os.path.lexists(root / slug):
            return slug
        n += 1
        slug = f"{base}-{n}"


def _make_folder(root: Path, slug: str) -> Path:
    if os.path.lexists(root) and (root.is_symlink() or not root.is_dir()):
        raise ProjectError("folder_unsafe", 400)
    try:
        root.mkdir(parents=True, exist_ok=True)
        project = root / slug
        project.mkdir()
    except OSError as exc:
        raise ProjectError("folder_create", 500) from exc
    return project


def create_project(name: str, kind: str = "other", *, slug: str | None = None, db: Database | None = None,
                   onboarding: bool = True) -> dict:
    database = _db(db)
    name = " ".join((name or "").split())
    if not 1 <= len(name) <= 120:
        raise ProjectError("bad_name", 400)
    if kind not in KINDS:
        raise ProjectError("bad_kind", 400)
    with _LOCK:
        root = projects_root(database)
        wanted = slugify(slug or name)
        final_slug = wanted if slug and not _slug_used(database, root, wanted) else _unique_slug(database, root, wanted)
        folder = _make_folder(root, final_slug)
        for sub in SUBFOLDERS:
            safefs.write_atomic(folder, f"{sub}/.gitkeep", b"")
        git = gitops.init_repo(folder, t("projects.git.created"))
        try:
            with database.write() as u:
                row = u.projects.create(name, str(folder), slug=final_slug, kind=kind)
                if onboarding:
                    u.onboarding.mark("first_project")
        except DbError as exc:
            raise ProjectError("db_failed", 500) from exc
        if active_id(database) is None or _count_projects(database) == 1:
            activate(row.id, database)
        out = _public(row, active_id(database))
        out["git"] = {"ok": git.ok, "reason": git.reason}
        return out


def _slug_used(db: Database, root: Path, slug: str) -> bool:
    with db.read() as u:
        return u.projects.get_by_slug(slug) is not None or os.path.lexists(root / slug)


def _count_projects(db: Database) -> int:
    with db.read() as u:
        return len(u.projects.list())


def ensure_default(db: Database | None = None) -> None:
    """The first time, "principal" is created with the name of the fund."""
    database = _db(db)
    with _LOCK:
        with database.read() as u:
            profile = u.profile.get()
            empty = not u.projects.list()
        if profile is None or not empty:
            return
        create_project(profile.fund_name or t("projects.default_name"), "other", slug="principal", db=database, onboarding=False)


def list_projects(db: Database | None = None) -> dict[str, Any]:
    database = _db(db)
    ensure_default(database)
    with database.read() as u:
        rows = u.projects.list()
    active = active_id(database)
    return {"workdir": str(workdir(database)), "root": str(projects_root(database)), "active_id": active,
            "kinds": list(KINDS), "projects": [_public(p, active) for p in rows]}


def rename_project(project_id: int, name: str, db: Database | None = None) -> dict:
    database = _db(db)
    name = " ".join((name or "").split())
    if not 1 <= len(name) <= 120:
        raise ProjectError("bad_name", 400)
    _row(database, project_id)
    with database.write() as u:
        row = u.projects.update(project_id, name=name)
    return _public(row, active_id(database))


# ---- tree and files -------------------------------------------------------------------------

def _duration(path: str) -> float | None:
    if not path.lower().endswith(".wav"):
        return None
    try:
        with wave.open(path, "rb") as w:
            return round(w.getnframes() / float(w.getframerate()), 1)
    except (OSError, EOFError, wave.Error, ZeroDivisionError):
        return None


def _walk(root: Path) -> tuple[list[dict], bool]:
    entries: list[dict] = []
    truncated = False

    def visit(folder: Path, rel: str, depth: int) -> None:
        nonlocal truncated
        try:
            with os.scandir(folder) as it:
                items = [e for e in it if e.name not in HIDDEN and not e.is_symlink()]
        except OSError:
            return
        items.sort(key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
        for e in items:
            if len(entries) >= TREE_MAX_ENTRIES:
                truncated = True
                return
            path = f"{rel}/{e.name}" if rel else e.name
            if e.is_dir(follow_symlinks=False):
                entries.append({"path": path, "name": e.name, "type": "dir", "depth": depth})
                if depth + 1 < TREE_MAX_DEPTH:
                    visit(Path(e.path), path, depth + 1)
            elif e.is_file(follow_symlinks=False):
                entries.append({"path": path, "name": e.name, "type": "file", "depth": depth,
                                "size": e.stat(follow_symlinks=False).st_size, "duration": _duration(e.path)})

    visit(root, "", 0)
    return entries, truncated


def project_tree(project_id: int, db: Database | None = None) -> dict[str, Any]:
    database = _db(db)
    project = _row(database, project_id)
    folder = _dir(project)
    entries, truncated = _walk(folder)
    return {"id": project.id, "name": project.name, "path": project.path, "entries": entries,
            "truncated": truncated, "git": _git_state(folder), "commits": gitops.log(folder, 10)}


def _unique_target(folder: Path, sub: str, name: str, digest: str) -> tuple[str, bool]:
    """(relative path, already_there). Same name and different content gets the hash in the name."""
    rel = f"{sub}/{name}"
    existing = safefs.read_regular(folder, rel) if os.path.lexists(folder / rel) else None
    if existing is None:
        return rel, False
    if hashlib.sha256(existing).hexdigest() == digest:
        return rel, True
    stem, dot, ext = name.rpartition(".")
    alt = f"{stem}-{digest[:8]}.{ext}" if dot else f"{name}-{digest[:8]}"
    return f"{sub}/{alt}", False


def _record_row(db: Database, project_id: int, folder: Path, rel: str, data: bytes, mime: str, digest: str) -> None:
    name = rel.rsplit("/", 1)[-1]
    try:
        with db.write() as u:
            u.attachments.add(kind=file_kind(name), stored_path=str(folder / rel), mime_type=mime or
                              "application/octet-stream", size_bytes=len(data), sha256=digest, project_id=project_id)
    except (IntegrityViolation, DbError):
        pass                                   # the same path is already recorded


def _store(db: Database, project, folder: Path, filename: str, data: bytes, mime: str,
           sub: str | None = None) -> tuple[str, bool]:
    """Write `data` into the project. Returns (relative path, changed)."""
    if not data:
        raise ProjectError("empty_file", 400)
    if len(data) > att.MAX_ATTACHMENT_BYTES:
        raise ProjectError("too_large", 413)
    name = clean_filename(filename)
    digest = hashlib.sha256(data).hexdigest()
    try:
        rel, present = _unique_target(folder, sub or subfolder_for(name), name, digest)
    except safefs.UnsafePath as exc:
        raise ProjectError("bad_path", 400) from exc
    if present:
        return rel, False
    try:
        safefs.write_atomic(folder, rel, data)
    except safefs.UnsafePath as exc:
        raise ProjectError("bad_path", 400) from exc
    _record_row(db, project.id, folder, rel, data, mime, digest)
    return rel, True


def add_file(project_id: int, filename: str, data: bytes, mime: str = "", db: Database | None = None) -> dict:
    database = _db(db)
    project = _row(database, project_id)
    folder = _dir(project)
    with _LOCK:
        rel, changed = _store(database, project, folder, filename, data, att.sniff_mime(data, mime))
        git = gitops.commit(folder, [rel], t("projects.git.add_file", name=rel.rsplit("/", 1)[-1])) \
            if changed else gitops.GitResult(True)
    return {"path": rel, "size": len(data), "created": changed, "git": {"ok": git.ok, "reason": git.reason}}


def _append_link(folder: Path, url: str, title: str) -> None:
    try:
        _write_link(folder, url, title)
    except safefs.UnsafePath as exc:
        raise ProjectError("bad_path", 400) from exc


def _write_link(folder: Path, url: str, title: str) -> None:
    current = safefs.read_regular(folder, t("projects.links.file"))
    text = current.decode("utf-8", errors="replace") if current else f"# {t('projects.links.header')}\n\n"
    if not text.endswith("\n"):
        text += "\n"
    safe_title = " ".join(title.replace("[", "(").replace("]", ")").split()) or url
    safe_url = url.replace(")", "%29").replace(" ", "%20")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    text += f"- {stamp} [{safe_title}]({safe_url})\n"
    safefs.write_atomic(folder, t("projects.links.file"), text.encode("utf-8"))


def add_link(project_id: int, url: str, title: str = "", *, download: bool = True, db: Database | None = None,
             resolver=None, connector=None) -> dict:
    """Save the link in the links file of the context folder and, if it points to an allowed file, download it safely."""
    database = _db(db)
    project = _row(database, project_id)
    folder = _dir(project)
    url = (url or "").strip()
    fetcher.check_url(url)
    title = " ".join((title or "").split())[:200]
    got = fetcher.fetch(url, resolver=resolver, connector=connector) if download else fetcher.Fetched(url=url)
    with _LOCK:
        _append_link(folder, url, title or fetcher.check_url(url)[0])
        paths = [t("projects.links.file")]
        saved = None
        if got.data is not None:
            rel, changed = _store(database, project, folder, got.name, got.data, got.mime)
            saved = rel
            if changed:
                paths.append(rel)
        git = gitops.commit(folder, paths, t("projects.git.add_link", name=title or got.name or url[:60]))
    return {"link": url, "file": saved, "downloaded": saved is not None, "git": {"ok": git.ok, "reason": git.reason}}


# ---- reading --------------------------------------------------------------------------------

_TEXT_EXT = {"txt", "md", "csv", "json", "py", "js", "ts", "tsx", "jsx", "cs", "pine", "sql", "sh", "r", "yaml", "yml",
             "toml", "log", "html", "htm", "svg", "xml", "cpp", "c", "h", "java", "go", "rs", "ipynb"}
_AUDIO_TYPES = {"mp3": "audio/mpeg", "m4a": "audio/mp4", "wav": "audio/wav", "ogg": "audio/ogg", "flac": "audio/flac",
                "aac": "audio/aac", "opus": "audio/ogg", "webm": "audio/webm"}


def read_file(project_id: int, rel: str, limit: int | None = None, db: Database | None = None) -> tuple[bytes, str, str]:
    """(bytes, mime, filename) of a regular file of the project. Never HTML or SVG as markup: those go as text."""
    database = _db(db)
    project = _row(database, project_id)
    folder = _dir(project)
    rel = clean_rel(rel)
    try:
        data = safefs.read_regular(folder, rel)
    except safefs.UnsafePath as exc:
        raise ProjectError("bad_path", 400) from exc
    if data is None:
        raise ProjectError("file_not_found", 404)
    name = rel.rsplit("/", 1)[-1]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    sniffed = att.sniff_mime(data[:16], "")
    if att.is_image(sniffed):
        mime = sniffed
    elif sniffed == "application/pdf" and ext == "pdf":
        mime = sniffed
    elif ext in _AUDIO_TYPES:
        mime = _AUDIO_TYPES[ext]
    elif ext in _TEXT_EXT:
        mime = "text/plain; charset=utf-8"
    else:
        try:
            data[:4096].decode("utf-8")
            mime = "text/plain; charset=utf-8"
        except UnicodeDecodeError:
            mime = "application/octet-stream"
    return (data[:limit] if limit else data), mime, name


# ---- save from the chat ---------------------------------------------------------------------

def save_from_chat(project_id: int, attachment_ids: list[str], notes: list[dict], store,
                   db: Database | None = None) -> dict:
    """Copy chat attachments (by id) and short texts (a transcript, an answer) into the project's subfolders."""
    database = _db(db)
    project = _row(database, project_id)
    folder = _dir(project)
    if not (attachment_ids or notes):
        raise ProjectError("nothing_to_save", 400)
    if len(attachment_ids) + len(notes) > 20:
        raise ProjectError("too_many", 400)
    saved: list[str] = []
    with _LOCK:
        for attachment_id in attachment_ids:
            record = store.get_attachment(str(attachment_id))
            blob = att.attachments_dir() / record.sha256 if record else None
            if record is None or blob is None or not blob.is_file() or blob.is_symlink():
                raise ProjectError("attachment_not_found", 404)
            rel, _ = _store(database, project, folder, record.name, blob.read_bytes(), record.mime)
            saved.append(rel)
        for note in notes:
            text = note.get("text") if isinstance(note, dict) else None
            if not isinstance(text, str) or not text.strip() or len(text) > 200_000:
                raise ProjectError("bad_note", 400)
            name = clean_filename(str(note.get("name") or "note"))
            if "." not in name:
                name += ".md"
            rel, _ = _store(database, project, folder, name, text.encode("utf-8"), "text/markdown", "documentos")
            saved.append(rel)
        git = gitops.commit(folder, saved, t("projects.git.save_chat", count=len(saved)))
    return {"saved": saved, "git": {"ok": git.ok, "reason": git.reason}}

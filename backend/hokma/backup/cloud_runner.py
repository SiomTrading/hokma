"""Cloud backup of everything Hokma knows, to the student's private GitHub repository `hokma-backup`.

Projects go to branches `hokma-projects/<slug>` (`FundGit`); the database, the memory, the Library originals, the disk
folders added to projects and the project files too big for Git go into one encrypted `.hkb`, attached to a private
release. A new `.hkb` is only uploaded when something changed. The recovery key lives in the system credential store.
"""
import asyncio
import hashlib
import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from hokma.backup import github_release, secret_scan
from hokma.backup.bundle import BackupError, create_bundle, generate_recovery_key, group_recovery_key, \
    normalize_recovery_key
from hokma.backup.fund_git import FundGit, FundGitError, desktop_branch, https_token_env
from hokma.connections.store import KeyStore
from hokma.core.config import claude_config_dir, hokma_data_dir
from hokma.db.paths import db_path
from hokma.db.timeutil import utcnow
from hokma.docs.ingest import libraries_dir
from hokma.github_connect import flow
from hokma.i18n import t
from hokma.projects.service import workdir

log = logging.getLogger("hokma.backup")

REPO_NAME = "hokma-backup"
EVERY_SECONDS = 6 * 3600
FIRST_RUN_SECONDS = 60
KEY, CONFIRMED = "backup-key", "backup-key-confirmed"
_lock = threading.Lock()


class KeyExists(BackupError):
    """The recovery key was already generated on this computer."""


# ---------------------------------------------------------------------------------------------- recovery key

def new_key() -> str:
    """Generate the recovery key, keep it in the credential store and return it. It can be generated again until it is
    confirmed (the student may have closed the screen before saving it); once confirmed it is final."""
    store = KeyStore()
    if store.get(KEY) and store.get(CONFIRMED):
        raise KeyExists(t("backup.key.exists"))
    key = generate_recovery_key()
    store.set(KEY, key)
    return group_recovery_key(key)


def confirm_key(last4: str) -> bool:
    store = KeyStore()
    saved = store.get(KEY) or ""
    ok = bool(saved) and normalize_recovery_key(saved)[-4:] == last4.strip().upper()
    if ok:
        store.set(CONFIRMED, "1")
    return ok


def key_ready() -> bool:
    store = KeyStore()
    return bool(store.get(KEY) and store.get(CONFIRMED))


# ---------------------------------------------------------------------------------------------- what to save

def memory_dir() -> Path:
    """Claude's per-project memory inside Hokma's own configuration folder (credentials sit above it, not in it)."""
    return claude_config_dir() / "projects"


def _files(root: Path, prefix: str):
    for base, _, names in os.walk(root):
        for name in names:
            path = Path(base) / name
            if not path.is_symlink():
                yield path, f"{prefix}/{path.relative_to(root).as_posix()}"


def plan(db) -> tuple[list[tuple[Path, str]], list[dict]]:
    """Files for the `.hkb` (Library originals, disk folders of projects) and what is left out: secrets. A folder
    inside `projects/` is a project: Git saves it."""
    with db.read() as u:
        folders = [(row["id"], row["path"]) for row in u.session.query("SELECT id, path FROM project_folders")]
    extra, omitted, own = [], [], (workdir(db) / "projects").resolve()
    for root, prefix in [(libraries_dir(), "libraries")] + [
            (Path(p), f"folders/{i}") for i, p in folders if own not in Path(p).resolve().parents and Path(p).resolve() != own]:
        for path, name in _files(root, prefix):
            if secret_scan.scan_file(path) is None:
                extra.append((path, name))
            else:
                omitted.append({"path": name, "reason": "secret"})
    return extra, omitted


def _watched(extra) -> list[Path]:
    db = db_path()
    return [db, db.with_name(db.name + "-wal"), *(p for p, _ in _files(memory_dir(), "m")), *(p for p, _ in extra)]


def _stats(extra):
    for path in _watched(extra):
        try:
            yield path, path.stat()
        except OSError:
            pass


def _fingerprint(extra) -> str:
    digest = hashlib.sha256()
    for path, st in _stats(extra):
        digest.update(f"{path}|{st.st_size}|{st.st_mtime_ns}\n".encode())
    return digest.hexdigest()


def _estimate(extra) -> int:
    return sum(st.st_size for _, st in _stats(extra))


# ---------------------------------------------------------------------------------------------- state

def _cache_path() -> Path:
    return hokma_data_dir() / "cloud-backup.json"


def _cache(db) -> dict:
    try:
        return json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        extra, omitted = plan(db)
        cache = {"omitted": omitted, "total": _estimate(extra)}
        _save_cache(cache)
        return cache


def _save_cache(cache: dict) -> None:
    _cache_path().parent.mkdir(parents=True, exist_ok=True)
    _cache_path().write_text(json.dumps(cache), encoding="utf-8")


def _save(db, status: str, detail: str | None = None, **fields) -> None:
    values = {"status": status, "status_detail": detail, "updated_at": utcnow(), **fields}
    with db.write() as u:
        sets = ", ".join(f"{k} = :{k}" for k in values)          # column names are the constants above
        if not u.session.execute(f"UPDATE cloud_backup_state SET {sets} WHERE destination = 'github'", values):
            u.session.insert("cloud_backup_state", {"destination": "github", **values})


def status(db) -> dict:
    with db.read() as u:
        row = u.session.one("SELECT * FROM cloud_backup_state WHERE destination = 'github'") or {}
    cache = _cache(db)
    state = "running" if _lock.locked() else ("error" if row.get("status") == "failed" else row.get("status") or "idle")
    return {"connected": bool(flow.token()), "repo": cache.get("repo"), "last_backup_at": row.get("last_backup_at"),
            "last_size": row.get("last_size_bytes"), "status": state, "message": row.get("status_detail"),
            "total_size_estimate": cache.get("total", 0), "omitted": cache.get("omitted", []),
            "key_ready": key_ready()}


# ---------------------------------------------------------------------------------------------- the backup

def _backup(db) -> None:
    if not key_ready():
        raise BackupError(t("backup.key.missing"))
    token = flow.token()
    if not token:
        raise BackupError(t("backup.cloud.no_token"))
    try:
        repo = flow.ensure_private_repo(REPO_NAME)
    except flow.GitHubError as exc:
        raise BackupError(t(exc.code)) from None
    root = workdir(db)
    try:
        synced = FundGit(root, remote=repo["clone_url"], auth_env=lambda: https_token_env(token)).sync(
            message=f"Backup {datetime.now():%Y-%m-%d %H:%M}", conflict_branch=desktop_branch())
    except FundGitError as exc:
        raise BackupError(str(exc)) from None
    extra, omitted = plan(db)
    for item in synced.skipped:                              # too big for Git: into the `.hkb`; secrets: left out
        rel = f"projects/{item.project}/{item.path}" if item.project else item.path
        if item.reason == "too_large":
            extra.append((root / rel, f"home/{rel}"))
        else:
            omitted.append({"path": rel, "reason": item.reason})
    fingerprint, cache = _fingerprint(extra), _cache(db)
    cache.update(omitted=omitted, total=_estimate(extra), repo=repo["full_name"])
    fields = {"last_backup_at": utcnow()}
    if cache.get("fingerprint") != fingerprint:
        name = f"hokma-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.hkb"
        with tempfile.TemporaryDirectory(dir=hokma_data_dir(), prefix="backup-") as tmp:
            info = create_bundle(Path(tmp) / name, db_path=db_path(), key=KeyStore().get(KEY), memory_dir=memory_dir(),
                                 extra=extra)
            github_release.upload(info.path, repo["full_name"], token=token)
        fields.update(last_size_bytes=info.size, last_remote_name=name)
    _save(db, "ok", None, **fields)
    cache["fingerprint"] = _fingerprint(extra)               # after the save: its own write is not a change
    _save_cache(cache)


def run(db) -> None:
    """One backup, now. Does nothing when one is already running."""
    if not _lock.acquire(blocking=False):
        return
    try:
        try:
            _backup(db)
        except BackupError as exc:
            _save(db, "failed", str(exc))
        except Exception as exc:                                 # never let a thread die silently
            log.warning("backup failed: %s", type(exc).__name__)
            _save(db, "failed", t("backup.cloud.failed", error=type(exc).__name__))
    finally:
        _lock.release()


def start(db) -> None:
    threading.Thread(target=run, args=(db,), daemon=True, name="hokma-backup").start()


async def loop(app) -> None:
    """From the start of the engine, every 6 hours; it only does something when GitHub and the key are ready."""
    await asyncio.sleep(FIRST_RUN_SECONDS)
    while True:
        if flow.token() and key_ready():
            start(app.state.db)
        await asyncio.sleep(EVERY_SECONDS)

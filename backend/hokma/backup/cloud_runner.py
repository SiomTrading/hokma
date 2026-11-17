"""Cloud backup of everything Hokma knows, to the student's private GitHub repository `hokma-backup`.

Projects go to branches `hokma-projects/<slug>` (`FundGit`); the database, the memory, the Library originals, the disk
folders added to projects and the project files too big for Git go into one encrypted `.hkb`, attached to a private
release. A new `.hkb` is only uploaded when something changed, or when the release no longer holds one. The recovery key lives in the system credential store.
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
from hokma.i18n import t, use_language
from hokma.projects.service import workdir

log = logging.getLogger("hokma.backup")

REPO_NAME = "hokma-backup"
EVERY_SECONDS = 6 * 3600
FIRST_RUN_SECONDS = 60
KEY, CONFIRMED = "backup-key", "backup-key-confirmed"
_lock = threading.Lock()
_cache_lock = threading.RLock()       # the read-modify-write of the status cache (the backup thread and the status poll)


class KeyExists(BackupError):
    """The recovery key was already generated on this computer."""


class Failure(BackupError):
    """A failed backup with a stable `code` (a catalog key) the panel can act on; the text is already translated."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# What a failed state was waiting for: once it is true the failure no longer applies and the state goes back to idle.
_RESOLVED = {
    "backup.key.missing": lambda: key_ready(),
    "backup.cloud.no_token": lambda: bool(flow.token()),
    "github.not_connected": lambda: bool(flow.token()),
}


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


def _read_cache() -> dict:
    return json.loads(_cache_path().read_text(encoding="utf-8"))


def _cache(db) -> dict:
    try:
        return _read_cache()
    except (OSError, ValueError) as exc:
        extra, omitted = plan(db)                # slow: outside the lock
        cache = {"omitted": omitted, "total": _estimate(extra)}
        # Rebuild on disk a file that is missing or holds invalid JSON; a read that merely failed on a good file (busy,
        # locked) must not overwrite its `failed_code`. The status poll gets here too, while a backup writes the file:
        # under the lock the file is read again, so a rebuild that took long never lands over what the backup saved.
        if isinstance(exc, (FileNotFoundError, ValueError)):
            with _cache_lock:
                try:
                    return _read_cache()
                except (OSError, ValueError):
                    _save_cache(cache)
        return cache


def _save_cache(cache: dict) -> None:
    """Atomic: the panel polls the status while a backup ends, and a half-written file would be read as broken, rebuilt
    and saved over `failed_code` (the failure then showed no code)."""
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)             # nothing left behind when the write or the replace fails


def _save(db, status: str, detail: str | None = None, **fields) -> None:
    values = {"status": status, "status_detail": detail, "updated_at": utcnow(), **fields}
    with db.write() as u:
        sets = ", ".join(f"{k} = :{k}" for k in values)          # column names are the constants above
        if not u.session.execute(f"UPDATE cloud_backup_state SET {sets} WHERE destination = 'github'", values):
            u.session.insert("cloud_backup_state", {"destination": "github", **values})


def restore_failure_path() -> Path:
    return hokma_data_dir() / "restore-failed.json"


def restore_failure() -> dict | None:
    """Set when a staged restore could not be applied at start-up: {"at": iso}. Cleared when it is dismissed."""
    try:
        value = json.loads(restore_failure_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _warning_text(warnings: dict) -> str:
    parts = []
    if warnings.get("conflict"):
        parts.append(t("backup.cloud.conflict", branches=", ".join(warnings["conflict"])))
    if warnings.get("projects"):
        parts.append(t("backup.cloud.project_skipped", projects=", ".join(warnings["projects"])))
    return " ".join(parts)


def status(db) -> dict:
    with db.read() as u:
        row = u.session.one("SELECT * FROM cloud_backup_state WHERE destination = 'github'") or {}
    cache = _cache(db)
    state = "running" if _lock.locked() else ("error" if row.get("status") == "failed" else row.get("status") or "idle")
    message, code = row.get("status_detail"), None
    if state == "ok" and cache.get("warnings"):                 # saved, but not all of it where it should be
        state, message = "warning", _warning_text(cache["warnings"])
    if state == "error":
        code = cache.get("failed_code")
        if code in _RESOLVED and _RESOLVED[code]():             # the reason of the failure is gone: it is not an error now
            state, message, code = "idle", None, None
        elif code in _RESOLVED:
            message = t(code)                                   # in the language of the panel that asks
    return {"connected": bool(flow.token()), "repo": cache.get("repo"), "last_backup_at": row.get("last_backup_at"),
            "last_size": row.get("last_size_bytes"), "status": state, "message": message, "code": code,
            "total_size_estimate": cache.get("total", 0), "omitted": cache.get("omitted", []),
            "key_ready": key_ready(), "restore_failed": restore_failure()}


# ---------------------------------------------------------------------------------------------- the backup

def _backup(db) -> None:
    if not key_ready():
        raise Failure("backup.key.missing", t("backup.key.missing"))
    token = flow.token()
    if not token:
        raise Failure("backup.cloud.no_token", t("backup.cloud.no_token"))
    try:
        repo = flow.ensure_private_repo(REPO_NAME)
    except flow.GitHubError as exc:
        raise Failure(exc.code, t(exc.code)) from None
    root = workdir(db)
    try:
        synced = FundGit(root, remote=repo["clone_url"], auth_env=lambda: https_token_env(token)).sync(
            message=f"Backup {datetime.now():%Y-%m-%d %H:%M}", conflict_branch=desktop_branch())
    except FundGitError as exc:
        raise BackupError(str(exc)) from None
    extra, omitted = plan(db)
    warnings: dict = {}
    branches = list(dict.fromkeys([*synced.branches, *([synced.branch] if synced.branch else [])]))
    if synced.status == "conflict_branch" or branches:       # the copy went to a side branch, not to the main one
        warnings["conflict"] = branches or [desktop_branch()]
    unsafe = [slug for slug, value in synced.projects.items() if isinstance(value, str)]
    if unsafe:                                               # git refused the project: it is NOT in the backup
        warnings["projects"] = unsafe
        omitted.extend({"path": f"projects/{slug}", "reason": "git"} for slug in unsafe)
    for item in synced.skipped:                              # too big for Git: into the `.hkb`; secrets: left out
        rel = f"projects/{item.project}/{item.path}" if item.project else item.path
        if item.reason == "too_large":
            extra.append((root / rel, f"home/{rel}"))
        else:
            omitted.append({"path": rel, "reason": item.reason})
    fingerprint, cache = _fingerprint(extra), _cache(db)
    cache.update(omitted=omitted, total=_estimate(extra), repo=repo["full_name"])
    fields: dict = {}
    remote = None
    if cache.get("fingerprint") == fingerprint:
        # Unchanged locally: the local fingerprint alone proves nothing about GitHub (the release may have been
        # deleted there), so only skip the upload when the newest backup is really in the release.
        # A network failure raises BackupError: the state becomes "failed", never a false "ok".
        found = github_release.list_backups(repo["full_name"], token=token)
        remote = found[0] if found else None
    if remote is None:
        name = f"hokma-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.hkb"
        with tempfile.TemporaryDirectory(dir=hokma_data_dir(), prefix="backup-") as tmp:
            info = create_bundle(Path(tmp) / name, db_path=db_path(), key=KeyStore().get(KEY), memory_dir=memory_dir(),
                                 extra=extra)
            github_release.upload(info.path, repo["full_name"], token=token)
        fields.update(last_size_bytes=info.size, last_remote_name=name)
    else:
        fields.update(last_size_bytes=remote["size"], last_remote_name=remote["name"])
    fields["last_backup_at"] = utcnow()
    _save(db, "ok", None, **fields)                          # the table only allows ok: the warning lives in the cache
    cache["fingerprint"] = _fingerprint(extra)               # after the save: its own write is not a change
    cache.pop("failed_code", None)
    if warnings:
        cache["warnings"] = warnings
    else:
        cache.pop("warnings", None)
    _save_cache(cache)


def _failed(db, detail: str, code: str | None) -> None:
    _save(db, "failed", detail)
    with _cache_lock:
        cache = _cache(db)
        if code:
            cache["failed_code"] = code
        else:
            cache.pop("failed_code", None)
        _save_cache(cache)


def _run_held(db, lang: str | None) -> None:
    """The work of one backup. The caller already holds `_lock`; it is released here."""
    try:
        with use_language(lang):
            try:
                _backup(db)
            except Failure as exc:
                _failed(db, str(exc), exc.code)
            except BackupError as exc:
                _failed(db, str(exc), None)
            except Exception as exc:                             # never let a thread die silently
                log.warning("backup failed: %s", type(exc).__name__)
                _failed(db, t("backup.cloud.failed", error=type(exc).__name__), None)
    finally:
        _lock.release()


def run(db, lang: str | None = None) -> None:
    """One backup, now. Does nothing when one is already running. `lang`: the panel's language for its messages."""
    if _lock.acquire(blocking=False):
        _run_held(db, lang)


def start(db, lang: str | None = None) -> None:
    """One backup in a thread. The lock is taken here, before the thread exists: `POST /api/backup/run` answers
    "running", so the next status read must say "running" too, not the previous state while the thread is still starting."""
    if _lock.acquire(blocking=False):
        try:
            threading.Thread(target=_run_held, args=(db, lang), daemon=True, name="hokma-backup").start()
        except BaseException:
            _lock.release()
            raise


async def loop(app) -> None:
    """From the start of the engine, every 6 hours; it only does something when GitHub and the key are ready."""
    await asyncio.sleep(FIRST_RUN_SECONDS)
    while True:
        if flow.token() and key_ready():
            start(app.state.db)
        await asyncio.sleep(EVERY_SECONDS)

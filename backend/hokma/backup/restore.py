"""Restore on a new computer: download and open the chosen `.hkb`, pull the projects, and leave the rest prepared.

`prepare` only stages the files; nothing live is touched. `apply_pending` runs when the engine starts, before the
database opens: it keeps the current database in `backups/` and puts the staged data in place.
"""
import logging
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from hokma.backup import github_release
from hokma.backup.bundle import DB_ENTRY, BackupError, open_bundle
from hokma.backup.cloud_runner import REPO_NAME, memory_dir
from hokma.backup.fund_git import FundGit, FundGitError, https_token_env
from hokma.backup.snapshot import safe_copy_db, safe_restore_db
from hokma.core.config import hokma_data_dir, hokma_home
from hokma.db.paths import backups_dir, db_path
from hokma.docs.ingest import libraries_dir
from hokma.github_connect import flow
from hokma.i18n import t
from hokma.projects.service import workdir

log = logging.getLogger("hokma.backup")

STAGE = "restore-pending"


def _repo() -> dict:
    try:
        return flow.ensure_private_repo(REPO_NAME)
    except flow.GitHubError as exc:
        raise BackupError(t(exc.code)) from None


def list_backups() -> list[dict]:
    return [{"name": b["name"], "size": b["size"], "created_at": b["created_at"]}
            for b in github_release.list_backups(_repo()["full_name"])]


def _rows(db_file: Path, sql: str) -> list[tuple]:
    try:
        con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
        try:
            return con.execute(sql).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []


def prepare(db, name: str, key: str) -> dict:
    """Stage backup `name`. A wrong key or a damaged file fails here, before anything is staged for good."""
    if Path(name).name != name:
        raise BackupError(t("backup.cloud.not_found"))
    repo, token = _repo(), flow.token()
    stage = hokma_data_dir() / STAGE
    shutil.rmtree(stage, ignore_errors=True)
    try:
        with tempfile.TemporaryDirectory(dir=hokma_data_dir(), prefix="restore-") as tmp:
            open_bundle(github_release.download(repo["full_name"], name, Path(tmp)), key, stage)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    staged = stage / DB_ENTRY
    reconnect = [{"kind": "engine", "name": r[0]} for r in _rows(
        staged, "SELECT label FROM engine_connections WHERE status <> 'removed'")]
    reconnect += [{"kind": "telegram", "name": r[0]} for r in _rows(
        staged, "SELECT COALESCE(user_name, chat_id) FROM telegram_link")]
    reconnect += [{"kind": "folder", "name": r[1]} for r in _rows(staged, "SELECT id, path FROM project_folders")
                  if not Path(r[1]).exists() and not (stage / "files" / "folders" / str(r[0])).is_dir()]
    try:                                                         # the projects come back from their own branches
        FundGit(workdir(db), remote=repo["clone_url"], auth_env=lambda: https_token_env(token),
                adopt_projects=True).pull_all()
    except FundGitError as exc:
        reconnect.append({"kind": "projects", "name": str(exc)})
    return {"status": "restart_required", "reconnect": reconnect}


def _copy(src: Path, dst: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)


def apply_pending() -> None:
    """At engine start, before the database opens. Without a staged restore it does nothing."""
    stage = hokma_data_dir() / STAGE
    staged = stage / DB_ENTRY
    if not staged.is_file():
        return
    target = db_path()
    if target.is_file():
        backups_dir().mkdir(parents=True, exist_ok=True)
        safe_copy_db(target, backups_dir() / f"before-restore-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.db")
    if not safe_restore_db(staged, target):
        log.error("restore: the database could not be replaced; the staged copy is kept in %s", stage)
        return
    files = stage / "files"
    _copy(stage / "memory", memory_dir())
    _copy(files / "libraries", libraries_dir())
    _copy(files / "home", hokma_home())
    for folder_id, path in _rows(staged, "SELECT id, path FROM project_folders"):
        try:
            _copy(files / "folders" / str(folder_id), Path(path))
        except OSError:                                          # the old path does not exist on this computer
            _copy(files / "folders" / str(folder_id), hokma_home() / "Restored" / f"folder-{folder_id}")
    shutil.rmtree(stage, ignore_errors=True)

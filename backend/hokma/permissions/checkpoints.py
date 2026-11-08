"""Restore points: the safety net for what the protections cannot see.

In "auto" mode, before every turn (chat and routine) the engine takes a snapshot of the project folder. It is a
Git commit made with a temporary index file (`GIT_INDEX_FILE`), so it holds the tracked files and also the
untracked ones that `.gitignore` does not exclude, and it touches nothing of the student's: not the index, not the
branch, not the history. The commit hangs from `refs/hokma/checkpoints/{ts}-{short sha}`.

- No new snapshot when the tree is identical to the latest one.
- Kept 7 days or 200 snapshots, whichever comes first, per project; older refs are dropped (the objects stay in Git
  until its normal garbage collection).
- Restoring never deletes anything that exists now: a missing file comes back in place; a file that exists and
  differs comes back next to it as `name (restored).ext`.

Only the points whose commit Hokma recorded in its own database are trusted: a ref that the agent writes into
`.git` (or one that points somewhere else than recorded) is ignored, and a restore never writes into Hokma's
protected places nor into `.git`, `.hokma` or `.claude` folders.

The project must be a Git repository with the plain config that `hokma.projects.gitops` accepts; otherwise there is no
restore point and the panel says so.
"""
from __future__ import annotations

import os
import re
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

from hokma.db import get_database
from hokma.i18n import t
from hokma.kit.guard import under_any
from hokma.kit.settings import protected_paths
from hokma.permissions.trash import free_name, within
from hokma.projects import gitops

REF_PREFIX = "refs/hokma/checkpoints/"
KEEP_SECONDS = 7 * 24 * 3600
KEEP_COUNT = 200
MAX_FILES = 50_000
_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{4,40}$")
FORBIDDEN_PARTS = {".git", ".hokma", ".claude"}
_SUBJECT = re.compile(r"^hokma checkpoint(?: conversation=(\S+))?")


class CheckpointError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def _out(done) -> str:
    return done.stdout.decode("utf-8", "replace").strip() if done is not None and done.returncode == 0 else ""


def _ready(project: Path) -> None:
    state = gitops.repo_state(project)
    if not state.ok:
        raise CheckpointError("no_git", t("permissions.err.no_git"))


def _git(project: Path, *args: str, env: dict[str, str] | None = None, input_bytes: bytes | None = None):
    return gitops.run_project_git(project, list(args), extra_env=env, input_bytes=input_bytes)


def _key(project: Path) -> str:
    return os.path.realpath(project)


def _trusted(project: Path, db: Any = None) -> dict[str, str]:
    """{point id: commit sha} of the points Hokma took in this project. Empty when the database cannot be read."""
    try:
        with (db or get_database()).read() as u:
            return u.trash.points.shas(_key(project))
    except Exception:
        return {}


def _list_refs(project: Path, db: Any = None) -> list[dict[str, Any]]:
    trusted = _trusted(project, db)
    done = _git(project, "for-each-ref", "--sort=-committerdate", "--format=%(refname)%09%(objectname)%09"
                "%(committerdate:unix)%09%(subject)", REF_PREFIX)
    rows = []
    for line in _out(done).splitlines():
        parts = line.split("\t", 3)
        if len(parts) < 4 or not parts[0].startswith(REF_PREFIX):
            continue
        ref, commit, stamp, subject = parts
        point_id = ref[len(REF_PREFIX):]
        if not _ID.match(point_id) or not stamp.isdigit() or trusted.get(point_id) != commit:
            continue                                                  # not taken by Hokma, or moved since
        m = _SUBJECT.match(subject)
        rows.append({"id": point_id, "ref": ref, "commit": commit, "at_unix": int(stamp),
                     "conversation_id": m.group(1) if m else None})
    return rows


def _prune(project: Path, rows: list[dict[str, Any]], now: float, db: Any = None) -> int:
    dropped = 0
    for index, row in enumerate(rows):
        if index >= KEEP_COUNT or now - row["at_unix"] > KEEP_SECONDS:
            if gitops.run_project_git(project, ["update-ref", "-d", row["ref"]]) is not None:
                dropped += 1
                try:
                    with (db or get_database()).write() as u:
                        u.trash.points.delete(_key(project), row["id"])
                except Exception:
                    pass
    return dropped


def take(project: str | Path, conversation_id: str | int | None = None, *, now: float | None = None) -> dict[str, Any]:
    """Snapshot `project`. Returns {"taken": bool, "reason": ..., "id": ...}; never raises for a normal "no"."""
    project = Path(project)
    now = time.time() if now is None else now
    state = gitops.repo_state(project)
    if not state.ok:
        return {"taken": False, "reason": state.reason or "no_git"}
    with tempfile.TemporaryDirectory(prefix="hokma-index-") as tmp:
        env = {"GIT_INDEX_FILE": os.path.join(tmp, "index")}
        head = _out(_git(project, "rev-parse", "-q", "--verify", "HEAD"))
        if head and _git(project, "read-tree", "HEAD", env=env) is None:
            return {"taken": False, "reason": "missing"}
        added = _git(project, "add", "-A", "--", ".", env=env)
        if added is None or added.returncode != 0:
            return {"taken": False, "reason": "failed"}
        tree = _out(_git(project, "write-tree", env=env))
    if not tree:
        return {"taken": False, "reason": "failed"}
    rows = _list_refs(project)
    if rows and _out(_git(project, "rev-parse", f"{rows[0]['commit']}^{{tree}}")) == tree:
        return {"taken": False, "reason": "unchanged"}
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    subject = "hokma checkpoint" + (f" conversation={conversation_id}" if conversation_id not in (None, "") else "")
    args = ["commit-tree", tree, *(["-p", head] if head else []), "-m", subject]
    commit = _out(_git(project, *args, env={"GIT_AUTHOR_DATE": f"{int(now)} +0000",
                                           "GIT_COMMITTER_DATE": f"{int(now)} +0000"}))
    if not commit:
        return {"taken": False, "reason": "failed"}
    point_id = f"{stamp}-{commit[:7]}"
    done = _git(project, "update-ref", REF_PREFIX + point_id, commit)
    if done is None or done.returncode != 0:
        return {"taken": False, "reason": "failed"}
    try:
        with get_database().write() as u:
            u.trash.points.add(_key(project), point_id, commit)
    except Exception:
        _git(project, "update-ref", "-d", REF_PREFIX + point_id)      # a point that is not recorded cannot be trusted
        return {"taken": False, "reason": "failed"}
    _prune(project, [{"id": point_id, "ref": REF_PREFIX + point_id, "commit": commit, "at_unix": int(now),
                      "conversation_id": None}, *rows], now)
    return {"taken": True, "reason": "", "id": point_id}


def listing(project: str | Path) -> list[dict[str, Any]]:
    project = Path(project)
    _ready(project)
    return [{"id": r["id"], "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r["at_unix"])),
             "conversation_id": r["conversation_id"]} for r in _list_refs(project)]


def _commit_of(project: Path, point_id: str) -> str:
    if not isinstance(point_id, str) or not _ID.match(point_id):
        raise CheckpointError("unknown_point", t("permissions.err.unknown_point"))
    commit = _out(_git(project, "rev-parse", "-q", "--verify", REF_PREFIX + point_id + "^{commit}"))
    if not commit or _trusted(project).get(point_id) != commit:
        raise CheckpointError("unknown_point", t("permissions.err.unknown_point"))
    if not commit:
        raise CheckpointError("unknown_point", t("permissions.err.unknown_point"))
    return commit


def _tree_files(project: Path, commit: str) -> list[tuple[str, str, str]]:
    """(mode, blob sha, path) of every file in the snapshot."""
    done = _git(project, "ls-tree", "-r", "-z", "--full-tree", commit)
    if done is None or done.returncode != 0:
        raise CheckpointError("unknown_point", t("permissions.err.unknown_point"))
    rows = []
    for entry in done.stdout.decode("utf-8", "surrogateescape").split("\0"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        parts = meta.split()
        if len(parts) == 3 and parts[1] == "blob":
            rows.append((parts[0], parts[2], path))
    return rows


def changes(project: str | Path, point_id: str) -> dict[str, Any]:
    """Files that are gone or different now compared with the snapshot (files created later are not listed)."""
    project = Path(project)
    _ready(project)
    commit = _commit_of(project, point_id)
    files = _tree_files(project, commit)
    truncated = len(files) > MAX_FILES
    files = files[:MAX_FILES]
    deleted, present = [], []
    for mode, sha, path in files:
        full = project / path
        if not os.path.lexists(full):
            deleted.append(path)
        elif os.path.isfile(full) and not os.path.islink(full):
            present.append((sha, path))
    changed: list[str] = []
    if present:
        done = _git(project, "hash-object", "--no-filters", "--stdin-paths",
                    input_bytes=("\n".join(p for _s, p in present) + "\n").encode("utf-8", "surrogateescape"))
        if done is not None and done.returncode == 0:
            hashes = done.stdout.decode("ascii", "replace").split()
            if len(hashes) == len(present):
                changed = [p for (sha, p), now in zip(present, hashes) if sha != now]
    return {"id": point_id, "deleted": sorted(deleted), "changed": sorted(changed), "truncated": truncated}


def _safe_relative(rel: str) -> str:
    if not isinstance(rel, str) or not rel or "\0" in rel or "\\" in rel:
        raise CheckpointError("bad_path", t("permissions.err.bad_path"))
    pure = PurePosixPath(rel)
    if (pure.is_absolute() or ".." in pure.parts or str(pure) != rel
            or any(part.lower() in FORBIDDEN_PARTS for part in pure.parts)):
        raise CheckpointError("bad_path", t("permissions.err.bad_path"))
    return rel


def _blob(project: Path, sha: str) -> bytes:
    done = _git(project, "cat-file", "blob", sha)
    if done is None or done.returncode != 0:
        raise CheckpointError("restore_failed", t("permissions.err.restore_failed", error="blob"))
    return done.stdout


def _check_target(project: Path, rel: str, protect: list[str]) -> Path:
    """The place a file goes back to, or CheckpointError: inside the project once links are resolved, never in a
    protected place (Hokma's data, config, launcher or `.hokma`), never under a `.git`, `.hokma` or `.claude` part
    of the resolved path, and never through a link in the folders between the project and the file."""
    target = project / rel
    real_project = os.path.realpath(project)
    real_parent = os.path.realpath(target.parent)
    if not within(real_parent, real_project):
        raise CheckpointError("bad_path", t("permissions.err.bad_path"))
    for candidate in (str(target), real_parent, os.path.join(real_parent, target.name), os.path.realpath(target)):
        if under_any(candidate, protect):
            raise CheckpointError("protected", t("trash.err.protected"))
    resolved = os.path.relpath(os.path.join(real_parent, target.name), real_project)
    if any(part.casefold() in FORBIDDEN_PARTS for part in PurePosixPath(Path(resolved).as_posix()).parts):
        raise CheckpointError("bad_path", t("permissions.err.bad_path"))
    walk = project
    for part in PurePosixPath(rel).parent.parts:
        walk = walk / part
        if os.path.islink(walk):                                   # a link in the way is never followed
            raise CheckpointError("bad_path", t("permissions.err.bad_path"))
    return target


def _write_back(project: Path, rel: str, mode: str, sha: str, protect: list[str]) -> tuple[Path, bool]:
    target = _check_target(project, rel, protect)
    data = _blob(project, sha)
    real_parent = os.path.realpath(target.parent)
    real_target = Path(real_parent) / target.name                  # write where it was checked, not through links
    renamed = False
    if os.path.lexists(real_target):
        try:
            if real_target.is_file() and not real_target.is_symlink() and real_target.read_bytes() == data:
                return target, False
        except OSError:
            pass
        real_target, renamed = free_name(real_target), True
    fd = -1
    try:
        real_target.parent.mkdir(parents=True, exist_ok=True)
        if os.path.realpath(real_target.parent) != real_parent:    # the folder changed under us
            raise OSError("folder changed")
        fd = os.open(real_target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(data)
            if mode == "100755":
                os.fchmod(handle.fileno(), 0o755)
    except OSError as exc:
        if fd >= 0:
            os.close(fd)
        raise CheckpointError("restore_failed", t("permissions.err.restore_failed", error=str(exc))) from exc
    return project / os.path.relpath(real_target, os.path.realpath(project)), renamed


def _protect(protect: list[str] | None) -> list[str]:
    return [str(p) for p in (protected_paths() if protect is None else protect)]


def restore_file(project: str | Path, point_id: str, rel: str, protect: list[str] | None = None) -> dict[str, Any]:
    project = Path(project)
    protect = _protect(protect)
    _ready(project)
    commit = _commit_of(project, point_id)
    rel = _safe_relative(rel)
    for mode, sha, path in _tree_files(project, commit):
        if path == rel:
            target, renamed = _write_back(project, rel, mode, sha, protect)
            return {"restored": [str(target.relative_to(project))], "renamed": renamed}
    raise CheckpointError("bad_path", t("permissions.err.bad_path"))


def restore_deleted(project: str | Path, point_id: str, protect: list[str] | None = None) -> dict[str, Any]:
    """Bring back every file that is gone. Files that exist now are not touched. Paths that are not allowed
    (a `.git`, `.hokma` or `.claude` part, a protected place, a link out of the project) are skipped and listed."""
    project = Path(project)
    protect = _protect(protect)
    _ready(project)
    commit = _commit_of(project, point_id)
    restored, skipped = [], []
    for mode, sha, path in _tree_files(project, commit)[:MAX_FILES]:
        if not os.path.lexists(project / path):
            try:
                _safe_relative(path)
                target, _renamed = _write_back(project, path, mode, sha, protect)
            except CheckpointError as exc:
                if exc.code not in ("bad_path", "protected"):
                    raise
                skipped.append(path)
                continue
            restored.append(str(target.relative_to(project)))
    return {"restored": sorted(restored), "count": len(restored), "skipped": sorted(skipped)}

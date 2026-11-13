"""Git for the fund folder: commit, fetch, rebase and push, without ever forcing anything.

Every git call goes through `_git`, which uses the same lock-down as the project repositories
(`hokma.projects.gitops`): hooks at the null device, global and system config null, clean environment, a fresh
temporary working directory, and only a plain `.git/config` (`repo_state`). The remote is never stored in the
repository config: it is an explicit URL on each call, so a config the agent could write cannot redirect it.

Conflict policy: when the push is rejected, fetch and rebase. When the rebase conflicts, abort it and push the
result to a branch of its own (`desktop/<date>`). Nothing is lost and
nothing is forced: no argument of any command here is `--force` or a `+refspec`.

Each project under `projects/` is its own repository and syncs on its own, to `hokma-projects/<slug>` of the same
remote. `projects/` is left out through a rules file Hokma owns in the data folder (`core.excludesFile`), never by writing
inside `.git`, so the fund repository never holds their contents.
"""
from __future__ import annotations

import base64
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

from hokma.core.config import hokma_data_dir
from hokma.core.proc import spawn_kwargs
from hokma.i18n import t
from hokma.projects.gitops import _HARDEN, _env, repo_state          # the shared lock-down
from hokma.update.safe_exec import find_executable

from hokma.backup import secret_scan

DEFAULT_BRANCH = "main"
PROJECT_PREFIX = "hokma-projects/"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
GIT_TIMEOUT = 120
MAX_DETAIL = 300
EXCLUDES_FILE = "fund-excludes"       # in the data folder: keeps `projects/` out of the fund repository
# Protocols git may use for the remote: no `ext::` or other helper programs.
_BASE_ENV = {"GIT_ALLOW_PROTOCOL": "file:ssh:https", "GIT_EDITOR": ":", "GIT_MERGE_AUTOEDIT": "no"}


class FundGitError(Exception):
    """A failure with a stable `code` (the panel and the machine row translate by it). The message is localized."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code, self.detail = code, detail
        super().__init__(error_message(code, detail))


def error_message(code: str, detail: str = "") -> str:
    if code == "missing":
        return t("fund.git.missing")
    if code == "unsafe":
        return t("fund.git.unsafe")
    if code == "no_remote":
        return t("fund.git.no_remote")
    if code == "bad_remote":
        return t("fund.git.bad_remote")
    return t("fund.git.failed", detail=detail or "?")


@dataclass(frozen=True)
class Skipped:
    path: str
    reason: str                     # too_large | secret
    line: int | None = None         # for secrets: where (the value is never kept)
    project: str | None = None

    def message(self) -> str:
        where = f"{self.project}/{self.path}" if self.project else self.path
        if self.reason == "too_large":
            return t("fund.skipped.too_large", path=where)
        return t("fund.skipped.secret", path=where, line=self.line or 0)


@dataclass(frozen=True)
class CommitResult:
    committed: bool
    skipped: tuple[Skipped, ...] = ()
    head: str | None = None


@dataclass(frozen=True)
class PullResult:
    status: str        # empty_remote | up_to_date | fast_forward | rebased | created | conflict
    head: str | None = None
    remote_head: str | None = None


@dataclass
class SyncResult:
    status: str = "synced"                      # synced | conflict_branch
    branch: str | None = None                   # the conflict branch of THIS repository, when it was needed
    head: str | None = None
    committed: bool = False
    skipped: list[Skipped] = field(default_factory=list)
    branches: list[str] = field(default_factory=list)      # every conflict branch, fund and projects
    projects: dict[str, "SyncResult | str"] = field(default_factory=dict)    # slug -> result, or an error code


# ---------------------------------------------------------------------------------------------- configuration


def parse_remote(url: str) -> str:
    """The remote URL as given, or FundGitError('bad_remote'). Never carries credentials."""
    value = (url or "").strip()
    if not value or value.startswith("-") or "::" in value or any(c in value for c in " \t\r\n\0"):
        raise FundGitError("bad_remote")
    if value.startswith("https://"):
        host_part = value[len("https://"):].split("/", 1)[0]
        if "@" in host_part or not host_part:
            raise FundGitError("bad_remote")
        return value
    if re.match(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^:]+$", value) or re.match(r"^ssh://[A-Za-z0-9._-]+@[A-Za-z0-9.-]+(:\d+)?/.+$", value):
        return value
    if os.path.isabs(value):                     # a local path: a mirror on a disk, and the tests
        return value
    raise FundGitError("bad_remote")


def _valid_branch(name: str) -> bool:
    return bool(re.match(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,100}$", name)) and ".." not in name and not name.endswith(("/", ".lock"))


def https_token_env(token: str) -> dict[str, str]:
    """Environment for one git process that authenticates to github.com over HTTPS. The token never goes in the
    URL or in argv: only in this process environment, for the life of one command."""
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}"}


def desktop_branch(today: date | None = None) -> str:
    return f"desktop/{(today or date.today()).isoformat()}"


# ---------------------------------------------------------------------------------------------- the repository


class FundGit:
    """One repository (the fund, or with `remote_branch='hokma-projects/<slug>'` one of its projects)."""

    def __init__(self, folder: Path | str, *, remote: str | None = None, branch: str = DEFAULT_BRANCH,
                 remote_branch: str | None = None, identity: tuple[str, str] = ("Hokma", "hokma@localhost"),
                 auth_env: Callable[[], dict[str, str]] | None = None,
                 adopt_projects: bool = False, project_slug: str | None = None) -> None:
        self.folder = Path(folder)
        self.remote = parse_remote(remote) if remote else None
        self.branch = branch if _valid_branch(branch) else DEFAULT_BRANCH
        self.remote_branch = remote_branch or self.branch
        self.identity = identity
        self.auth_env = auth_env
        self.adopt_projects = adopt_projects            # restoring brings projects that exist only on the remote
        self.project_slug = project_slug

    # ------------------------------------------------------------------ process

    def _git(self, *args: str, check: bool = True, in_repo: bool = True, timeout: int = GIT_TIMEOUT
             ) -> subprocess.CompletedProcess:
        exe = find_executable("git")
        if exe is None:
            raise FundGitError("missing")
        extra = {**_BASE_ENV, **(self.auth_env() if self.auth_env else {})}
        prefix = ["-C", str(self.folder)] if in_repo else []
        name, email = self.identity
        with tempfile.TemporaryDirectory(prefix="hokma-helper-") as cwd:
            # A known-good configuration on every call: no credential helper, the default ssh program (the runner's
            # key arrives in GIT_SSH_COMMAND, which wins), hooks stay at the null device (the shared lock-down), and the ignore rule from a file
            # that Hokma owns instead of one inside the repository.
            known_good = ["-c", "credential.helper=", "-c", "core.sshCommand=ssh",
                          "-c", f"core.excludesFile={self._rules_file()}"]
            argv = [exe, *prefix, *_HARDEN, *known_good, "-c", f"user.name={name}", "-c", f"user.email={email}", *args]
            env = _env(cwd)
            env.update(extra)
            try:
                done = subprocess.run(argv, capture_output=True, encoding="utf-8", errors="replace",
                                      timeout=timeout, env=env, cwd=cwd, stdin=subprocess.DEVNULL, **spawn_kwargs())
            except subprocess.TimeoutExpired:
                raise FundGitError("failed", "timed out") from None
            except (OSError, subprocess.SubprocessError):
                raise FundGitError("missing") from None
        if check and done.returncode != 0:
            raise FundGitError("failed", _scrub(done.stderr or done.stdout, extra))
        return done

    def _rules_file(self) -> str:
        """The ignore rules Hokma owns. Only the fund repository leaves out `projects/`; a project repository none."""
        if self.project_slug is not None:
            return os.devnull
        path = hokma_data_dir() / EXCLUDES_FILE
        try:
            if not path.is_file() or path.is_symlink():
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(path.name + ".tmp")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write("projects/\n")
                os.replace(tmp, path)
        except OSError as exc:
            raise FundGitError("failed", str(exc)[:MAX_DETAIL]) from None
        return str(path)

    def _require_remote(self) -> str:
        if not self.remote:
            raise FundGitError("no_remote")
        return self.remote

    def _require_safe(self) -> None:
        state = repo_state(self.folder)
        if state.reason == "no_repo":
            raise FundGitError("failed", "not a repository")
        if not state.ok:
            raise FundGitError("unsafe")

    # ------------------------------------------------------------------ setup

    def ensure_repo(self) -> None:
        """Init when there is no repository, keep `projects/` out of the fund repository. The identity is passed on
        each command, so the repository config stays the plain one Git writes."""
        state = repo_state(self.folder)
        if state.reason == "no_repo":
            self.folder.mkdir(parents=True, exist_ok=True)
            self._git("init", "-q", "-b", self.branch, str(self.folder), in_repo=False)
        elif not state.ok:
            self._require_safe()
        self._rules_file()

    def head(self) -> str | None:
        done = self._git("rev-parse", "-q", "--verify", "HEAD", check=False)
        return done.stdout.strip() or None if done.returncode == 0 else None

    # ------------------------------------------------------------------ commit

    def commit_all(self, message: str) -> CommitResult:
        """Stage everything that is allowed, commit when something changed. Files that are too large or look like
        secrets stay out of the commit and are reported."""
        self._require_safe()
        skipped = self._stage()
        changed = self._git("diff", "--cached", "--quiet", check=False)
        if changed.returncode == 0:
            return CommitResult(False, tuple(skipped), self.head())
        if changed.returncode != 1:
            raise FundGitError("failed", _scrub(changed.stderr, {}))
        self._git("commit", "-q", "--no-verify", "--no-gpg-sign", "-m", message)
        return CommitResult(True, tuple(skipped), self.head())

    def _stage(self) -> list[Skipped]:
        out = self._git("status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
        skipped: list[Skipped] = []
        tokens = out.split("\0")
        i = 0
        while i < len(tokens):
            entry = tokens[i]
            i += 1
            if len(entry) < 4:
                continue
            code, path = entry[:2], entry[3:]
            if code[0] in "RC":
                i += 1                                   # the original name of a rename or copy
            if "D" in code:
                continue
            found = self._inspect(path)
            if found is not None:
                skipped.append(Skipped(path, found[0], found[1], self.project_slug))
        excludes = [f":(exclude,literal){s.path}" for s in skipped]
        self._git("add", "-A", "--", ".", *excludes)
        return skipped

    def _inspect(self, rel: str) -> tuple[str, int | None] | None:
        path = self.folder / rel
        try:
            st = os.lstat(path)
        except OSError:
            return None
        if not (st.st_mode & 0o170000 == 0o100000):      # links and folders are not read
            return None
        if st.st_size > secret_scan.MAX_FILE_BYTES:
            return "too_large", None
        line = secret_scan.scan_file(path)
        return ("secret", line) if line is not None else None

    # ------------------------------------------------------------------ fetch, pull, push

    def _fetch(self) -> str | None:
        """Fetch the remote branch into FETCH_HEAD (no tracking ref, no `+` refspec). Its commit, or None when the
        remote does not have the branch yet."""
        self._require_safe()
        done = self._git("fetch", "-q", "--no-tags", self._require_remote(), f"refs/heads/{self.remote_branch}",
                         check=False)
        if done.returncode != 0:
            text = done.stderr or ""
            if "couldn't find remote ref" in text:
                return None
            raise FundGitError("failed", _scrub(text, self.auth_env() if self.auth_env else {}))
        sha = self._git("rev-parse", "-q", "--verify", "FETCH_HEAD^{commit}", check=False).stdout.strip()
        return sha or None

    def pull(self) -> PullResult:
        """Fetch and bring the remote branch in: fast-forward when possible, rebase local commits on top when the
        history diverged. A rebase that conflicts is aborted and reported as `conflict` (the tree is as it was)."""
        self._require_safe()
        remote = self._fetch()
        local = self.head()
        if remote is None:
            return PullResult("empty_remote", local, None)
        if local is None:
            self._git("checkout", "-q", "-B", self.branch, remote)
            return PullResult("created", remote, remote)
        if local == remote or self._git("merge-base", "--is-ancestor", remote, local, check=False).returncode == 0:
            return PullResult("up_to_date", local, remote)
        if self._git("merge-base", "--is-ancestor", local, remote, check=False).returncode == 0:
            self._git("merge", "-q", "--ff-only", remote)
            return PullResult("fast_forward", self.head(), remote)
        self._require_safe()
        rebased = self._git("rebase", remote, check=False)
        if rebased.returncode == 0:
            return PullResult("rebased", self.head(), remote)
        self._git("rebase", "--abort", check=False)
        if self.head() != local:                                # the abort did not restore: do not go on
            raise FundGitError("failed", "rebase could not be undone")
        return PullResult("conflict", local, remote)

    def _push_ref(self, ref: str) -> str:
        """Push HEAD to `ref` of the remote. 'ok', or 'rejected' when the remote moved. Never forced."""
        self._require_safe()
        done = self._git("push", "-q", self._require_remote(), f"HEAD:{ref}", check=False)
        if done.returncode == 0:
            return "ok"
        text = done.stderr or ""
        if "[rejected]" in text or "non-fast-forward" in text or "fetch first" in text:
            return "rejected"
        raise FundGitError("failed", _scrub(text, self.auth_env() if self.auth_env else {}))

    # ------------------------------------------------------------------ sync

    def sync(self, *, message: str, conflict_branch: str) -> SyncResult:
        """Commit locally, fetch, rebase, push. The fund first, then every project repository."""
        self.ensure_repo()
        self._require_remote()
        result = self._sync_one(message, conflict_branch)
        if result.branch:
            result.branches.append(result.branch)
        if self.project_slug is None:
            self._sync_projects(result, message, conflict_branch)
        return result

    def _sync_one(self, message: str, conflict_branch: str) -> SyncResult:
        commit = self.commit_all(message)
        result = SyncResult(committed=commit.committed, skipped=list(commit.skipped))
        for _ in range(3):
            pulled = self.pull()
            result.head = self.head()
            if pulled.status == "conflict":
                return self._save_conflict(result, conflict_branch)
            if result.head is None or result.head == pulled.remote_head:
                return result                                   # nothing to send
            if self._push_ref(f"refs/heads/{self.remote_branch}") == "ok":
                return result
        return self._save_conflict(result, conflict_branch)     # the remote keeps moving: keep the work apart

    def _save_conflict(self, result: SyncResult, base: str) -> SyncResult:
        """Push the local result to its own branch. Local history stays as it is."""
        head = self.head() or ""
        for name in (base, f"{base}-{head[:7]}"):
            if self._push_ref(f"refs/heads/{name}") == "ok":
                result.status, result.branch = "conflict_branch", name
                return result
        raise FundGitError("failed", "the conflict branch was rejected")

    def _children(self) -> list["FundGit"]:
        """The project repositories under `projects/` (restoring first creates the ones only the remote has)."""
        root = self.folder / "projects"
        if root.is_symlink() or not _inside(root, self.folder):
            return []
        if self.adopt_projects:
            self._adopt(root)
        try:
            slugs = sorted(p.name for p in root.iterdir()
                           if not p.is_symlink() and p.is_dir() and SLUG_RE.match(p.name) and (p / ".git").exists()
                           and _inside(p, root) and _inside(p, self.folder))
        except OSError:
            return []
        return [FundGit(root / slug, remote=self.remote, branch=self.branch, remote_branch=f"{PROJECT_PREFIX}{slug}",
                        identity=self.identity, auth_env=self.auth_env,
                        project_slug=slug) for slug in slugs]

    def _sync_projects(self, result: SyncResult, message: str, conflict_branch: str) -> None:
        for child in self._children():
            slug = child.project_slug or ""
            try:
                sub = child._sync_one(message, f"{conflict_branch}-{slug}")
            except FundGitError as exc:
                result.projects[slug] = exc.code
                continue
            result.projects[slug] = sub
            result.skipped.extend(sub.skipped)
            if sub.branch:
                result.branches.append(sub.branch)
        if result.branches:
            result.status = "conflict_branch"

    def _adopt(self, root: Path) -> None:
        """Create an empty repository for every project the remote has and this machine does not."""
        self._require_safe()
        done = self._git("ls-remote", "--heads", self._require_remote(), f"refs/heads/{PROJECT_PREFIX}*", check=False)
        if done.returncode != 0:
            return
        for line in done.stdout.splitlines():
            _, _, ref = line.partition("\t")
            slug = ref.removeprefix(f"refs/heads/{PROJECT_PREFIX}")
            target = root / slug
            if not SLUG_RE.match(slug) or os.path.lexists(target):
                continue
            target.mkdir(parents=True, exist_ok=True)
            self._git("init", "-q", "-b", self.branch, str(target), in_repo=False)

    def pull_all(self) -> PullResult:
        """Before a run: fetch and fast-forward the fund and its projects. A diverged history is reported as
        `conflict` and left for `sync` (which saves the result on its own branch)."""
        self.ensure_repo()
        self._require_remote()
        pulled = self.pull()
        if self.project_slug is None:
            for child in self._children():
                try:
                    if child.pull().status == "conflict" and pulled.status != "conflict":
                        pulled = PullResult("conflict", pulled.head, pulled.remote_head)
                except FundGitError:
                    continue
        return pulled


def _inside(path: Path, base: Path) -> bool:
    """True when `path` resolves to a place within `base` (symlinks followed on both)."""
    try:
        path.resolve().relative_to(base.resolve())
    except (OSError, ValueError):
        return False
    return True


def _scrub(text: str, secrets_env: dict[str, str]) -> str:
    """Short, one-line error text with any value that was handed to git through the environment removed."""
    out = (text or "").strip()
    for value in secrets_env.values():
        if value and len(value) > 8:
            out = out.replace(value, "***")
            if " " in value:
                out = out.replace(value.rsplit(" ", 1)[-1], "***")       # the base64 part of a header
    out = " ".join(out.split())
    return out[:MAX_DETAIL]

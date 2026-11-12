"""Each project has its own Git repository. Git runs through `hokma.update.safe_exec` (clean environment,
fresh temporary working directory) and only when the repository config is the plain one Git writes itself.

The agent can write inside the project, so `.git/config` is untrusted: a filter, a fsmonitor or a hooks path
there would run a program of the agent's choosing. Anything but the plain keys disables Git for that project
(files are still saved) and the panel says so.
"""
import os
import stat
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from hokma.core.proc import spawn_kwargs
from hokma.update.safe_exec import clean_env, find_executable

# Only these keys may exist in a project's .git/config (compared in lowercase). Git itself parses the file
# (`git config --file --list -z`), so odd spacing, indented sections or multi-line values cannot hide a key.
_ALLOWED_KEYS = frozenset({
    "core.repositoryformatversion", "core.filemode", "core.bare", "core.logallrefupdates", "core.ignorecase",
    "core.precomposeunicode", "user.name", "user.email", "init.defaultbranch",
})
_IDENTITY = ["-c", "user.name=Hokma", "-c", "user.email=hokma@localhost"]
# Every project git call: no hooks, no fsmonitor, no attributes file, no signing. Together with the environment
# below (global and system config pointed at the null device) no filter, alias or helper defined anywhere
# outside the validated local config can run program code during add or commit.
_HARDEN = ["-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false", "-c", "commit.gpgsign=false",
           "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "-c", f"core.attributesFile={os.devnull}"]


def _env(cwd: str) -> dict[str, str]:
    env = clean_env(cwd)
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_NOSYSTEM="1",
               GIT_ATTR_NOSYSTEM="1")
    return env


def _run(args: list[str], timeout: int):
    """git in a fresh empty folder with a locked-down environment. None when git is missing or cannot run."""
    exe = find_executable("git")
    if exe is None:
        return None
    with tempfile.TemporaryDirectory(prefix="hokma-helper-") as cwd:
        try:
            return subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout, env=_env(cwd),
                                  cwd=cwd, stdin=subprocess.DEVNULL, **spawn_kwargs())
        except (OSError, subprocess.SubprocessError):
            return None


@dataclass(frozen=True)
class GitResult:
    ok: bool
    reason: str = ""       # "", "no_repo", "unsafe", "failed", "missing"


def _config_is_plain(config: Path) -> bool:
    done = _run(["config", "--file", str(config), "--list", "-z"], 15)
    if done is None or done.returncode != 0:
        return False
    for entry in done.stdout.split("\0"):
        if entry and entry.split("\n", 1)[0].lower() not in _ALLOWED_KEYS:
            return False
    return True


def repo_state(project: Path) -> GitResult:
    git = project / ".git"
    try:
        st = os.lstat(git)
    except OSError:
        return GitResult(False, "no_repo")
    if not stat.S_ISDIR(st.st_mode):
        return GitResult(False, "unsafe")
    config = git / "config"
    try:
        if not stat.S_ISREG(os.lstat(config).st_mode):
            return GitResult(False, "unsafe")
    except OSError:
        return GitResult(False, "unsafe")
    return GitResult(True) if _config_is_plain(config) else GitResult(False, "unsafe")


_AUTO_LOCK = threading.Lock()
_AUTO_TRIED: set[str] = set()        # projects whose history was already attempted in this run: once each, never again
AUTO_INIT_WAIT = 1.5                 # seconds a listing waits for the first history of a project, at most


def git_installed() -> bool:
    return find_executable("git") is not None


def _auto_init(project: Path, message: str) -> None:
    # `.git` is looked at again right before: an existing one, safe or not, is never touched.
    if os.path.lexists(project / ".git"):
        return
    init_repo(project, message)


def ensure_repo(project: Path, message: str, wait: float | None = None) -> GitResult:
    """The project's state, creating its history by itself when it has none: git installed, a real folder and no
    `.git` of any kind. It is attempted once per project and runs in a thread, so the caller waits `wait` seconds
    at most (the first answer can still say `no_repo` on a very slow disk; the next one reads the finished history).
    Git missing is reported as `missing` and nothing is attempted. An existing `.git` is never changed."""
    state = repo_state(project)
    if state.reason != "no_repo":
        return state
    if not git_installed():
        return GitResult(False, "missing")
    try:
        if project.is_symlink() or not project.is_dir():
            return state
        key = str(project.resolve())
    except OSError:
        return state
    with _AUTO_LOCK:
        first = key not in _AUTO_TRIED
        _AUTO_TRIED.add(key)
    if first:
        worker = threading.Thread(target=_auto_init, args=(project, message), name="hokma-git-init", daemon=True)
        worker.start()
        worker.join(AUTO_INIT_WAIT if wait is None else wait)
    return repo_state(project)


def _git(project: Path, *args: str, timeout: int = 60):
    return _run(["-C", str(project), *_HARDEN, *_IDENTITY, *args], timeout)


def init_repo(project: Path, message: str) -> GitResult:
    """git init plus a first commit of everything the project starts with."""
    done = _run(["init", "-q", str(project)], 30)
    if done is None:
        return GitResult(False, "missing")
    if done.returncode != 0:
        return GitResult(False, "failed")
    return commit(project, ["."], message)


def commit(project: Path, paths: list[str], message: str) -> GitResult:
    state = repo_state(project)
    if not state.ok:
        return state
    added = _git(project, "add", "--", *paths)
    if added is None:
        return GitResult(False, "missing")
    if added.returncode != 0:
        return GitResult(False, "failed")
    done = _git(project, "commit", "-q", "--no-verify", "--no-gpg-sign", "-m", message)
    if done is None:
        return GitResult(False, "missing")
    if done.returncode != 0 and "nothing to commit" not in (done.stdout + done.stderr):
        return GitResult(False, "failed")
    return GitResult(True)


def log(project: Path, limit: int = 20) -> list[str]:
    state = repo_state(project)
    if not state.ok:
        return []
    done = _git(project, "log", f"-{int(limit)}", "--pretty=format:%s")
    return done.stdout.splitlines() if done is not None and done.returncode == 0 else []


def run_project_git(project: Path, args: list[str], *, extra_env: dict[str, str] | None = None,
                    input_bytes: bytes | None = None, timeout: int = 120):
    """git on a project with the same lock-down as every other call here, for callers that need raw bytes or an
    extra environment variable (a temporary index file). Returns CompletedProcess (stdout and stderr as bytes) or
    None when git is missing or cannot run. The caller has checked `repo_state` first."""
    exe = find_executable("git")
    if exe is None:
        return None
    with tempfile.TemporaryDirectory(prefix="hokma-helper-") as cwd:
        env = _env(cwd)
        env.update(extra_env or {})
        try:
            return subprocess.run([exe, "-C", str(project), *_HARDEN, *_IDENTITY, *args], capture_output=True,
                                  timeout=timeout, env=env, cwd=cwd, input=input_bytes,
                                  stdin=None if input_bytes is not None else subprocess.DEVNULL, **spawn_kwargs())
        except (OSError, subprocess.SubprocessError):
            return None

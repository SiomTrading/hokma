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

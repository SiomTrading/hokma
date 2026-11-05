"""Run `git` and `gh` without letting the surrounding folder or environment steer them.

The student's working folder is a git repository the agent can write to. A `.git/config` there could name a
credential helper or another program, and environment variables such as GIT_DIR or GIT_CONFIG_* can redirect
git to a config the agent controls. So every helper process here:

- runs in a fresh empty temporary folder (never the inherited cwd and never HOKMA_HOME), with
  GIT_CEILING_DIRECTORIES set to that folder's parent so git cannot discover a repository above it;
- gets an environment with the git and gh redirection variables removed;
- is started from an absolute path found on a PATH stripped of relative entries, of the inherited cwd itself
  and of anything inside HOKMA_HOME; the program found must not live in any of those places.
"""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from hokma.core.config import hokma_home

STRIPPED_NAMES = frozenset({
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR", "GIT_NAMESPACE",
    "GIT_EXEC_PATH", "GIT_ASKPASS", "SSH_ASKPASS", "GH_CONFIG_DIR",
})
STRIPPED_PREFIXES = ("GIT_CONFIG",)       # GIT_CONFIG, _GLOBAL, _SYSTEM, _COUNT, _KEY_n, _VALUE_n, _PARAMETERS


def clean_env(cwd: str | os.PathLike, base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for a helper process started in `cwd` (a folder this code created)."""
    source = os.environ if base is None else base
    env = {k: v for k, v in source.items()
           if k.upper() not in STRIPPED_NAMES and not k.upper().startswith(STRIPPED_PREFIXES)}
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never",
               GIT_CEILING_DIRECTORIES=str(Path(cwd).resolve().parent))
    return env


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
    except (ValueError, OSError):
        return False
    return True


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def _inherited_cwd() -> Path | None:
    try:
        return Path.cwd()
    except OSError:
        return None


def _is_blocked(path: Path) -> bool:
    """Blocked: the whole working folder, and the inherited cwd itself (not its other descendants).

    A cwd that is an ancestor of a legitimate install (the student's home with Git under AppData) must not
    exclude it. A cwd inside the working folder is already covered by the working folder rule.
    """
    home = hokma_home()
    if home.parent != home and _inside(path, home):
        return True
    cwd = _inherited_cwd()
    return cwd is not None and _same(path, cwd)


def find_executable(name: str) -> str | None:
    """Absolute path of `name`, or None when it is only reachable through the cwd or the working folder."""
    entries = [e for e in os.environ.get("PATH", "").split(os.pathsep)
               if e and os.path.isabs(e) and not _is_blocked(Path(e))]
    found = shutil.which(name, path=os.pathsep.join(entries))
    if not found:
        return None
    program = Path(found).absolute()
    if _is_blocked(program.parent) or _is_blocked(Path(found).resolve().parent):
        return None
    return str(program)


def run_isolated(name: str, args: list[str], *, input_text: str | None = None, timeout: int = 15):
    """Run `name args` in a fresh temporary folder. None when the program is missing or cannot run."""
    exe = find_executable(name)
    if exe is None:
        return None
    with tempfile.TemporaryDirectory(prefix="hokma-helper-") as cwd:
        try:
            return subprocess.run([exe, *args], input=input_text, capture_output=True, text=True, timeout=timeout,
                                  env=clean_env(cwd), cwd=cwd,
                                  stdin=None if input_text is not None else subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None

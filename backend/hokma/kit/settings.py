"""settings.json that Hokma generates for its own CLAUDE_CONFIG_DIR.

Controls, from strongest to weakest. None of the first three depends on the text of a command:

1. Sandbox (Claude Code `sandbox` settings): Bash runs sandboxed with writes allowed only in the
   working folder; the data folder, the config, the launcher and the student's extensions file are in
   `filesystem.denyWrite`. Not available on native Windows: `hokma doctor` warns.
2. PreToolUse hook `hokma/kit/guard.py`: deterministic, resolves real paths for Edit, MultiEdit, Write
   and NotebookEdit and blocks the protected paths. Fails CLOSED.
3. Regeneration: the file is rebuilt from the package template at EVERY Claude launch and passed
   explicitly with --settings, so an edit by the agent does not survive the next launch.
4. `permissions.deny` for Edit/Write/Bash on those paths. The Bash substring rule is best effort
   only (it is easy to evade with $VARS, ~, globs or relative paths); it is an extra layer.

The student's extensions (`enabledPlugins`, `extraKnownMarketplaces`, from
<workdir>/.hokma/extensions.json) are merged in AFTER the template and can only add those two keys:
they never touch permissions, sandbox or hooks.

Key names (sandbox.enabled, failIfUnavailable, allowUnsandboxedCommands, filesystem.allowWrite,
filesystem.denyWrite, filesystem.denyRead) were found in the Claude Code 2.1.317 binary [VERIFIED].
Their exact semantics and the permission-rule path syntax follow the public docs [ASSUMED] and must
be confirmed on real machines.
"""
import json
import shlex
import sys
from pathlib import Path

from hokma.core.config import (claude_config_dir, hokma_data_dir,
                               launcher_path, workdir_hokma_dir)
from hokma.kit import extensions
from hokma.kit import writable


def _abs_rule_path(p: Path) -> str:
    """Absolute path in Claude Code rule syntax: leading '//' and forward slashes."""
    s = str(p).replace("\\", "/")
    if len(s) > 1 and s[1] == ":":  # C:/Users/x -> /C:/Users/x so it becomes //C:/Users/x
        s = "/" + s
    return "/" + s if s.startswith("/") else "//" + s


TEMPLATE = Path(__file__).resolve().parent / "templates" / "settings.base.json"
GUARD_FILE = Path(__file__).resolve().parent / "guard.py"


def protected_paths() -> list[Path]:
    """Runtime and config roots (the data dir contains both), the PATH launcher and the student's
    `.hokma` folder (mirror of the extensions; changed only by Hokma, never by the agent)."""
    return [hokma_data_dir(), claude_config_dir(), launcher_path(), workdir_hokma_dir()]


def guard_command() -> str:
    """Hook command: `cd` into the protected data dir, then run the guard by ABSOLUTE PATH with an
    isolated interpreter (-I: no cwd, no script dir, no PYTHON* variables on sys.path), so nothing
    planted in the working folder can replace it. `|| exit 2` makes a failed cd, a crash or a missing
    interpreter block the tool call (any other non-zero hook exit code would not block). Hooks run
    through a POSIX shell (Git Bash on Windows) [ASSUMED]."""
    args = " ".join(f"--protect {shlex.quote(str(p))}" for p in protected_paths())
    return (f"cd {shlex.quote(str(hokma_data_dir()))} && "
            f"{shlex.quote(sys.executable)} -I {shlex.quote(str(GUARD_FILE))} {args} || exit 2")


def sandbox_available() -> bool:
    return sys.platform != "win32"


def build_settings() -> dict:
    settings = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    deny = settings["permissions"]["deny"]
    for p in protected_paths():
        rule = _abs_rule_path(p)
        for tool in ("Edit", "Write"):
            deny.append(f"{tool}({rule})")
            deny.append(f"{tool}({rule}/**)")
        deny.append(f"Bash(*{str(p)}*)")  # best effort only, see module docstring

    sandbox = settings["sandbox"]
    sandbox["filesystem"]["allowWrite"] = [str(p) for p in writable.sandbox_allow_write()]
    sandbox["filesystem"]["denyWrite"] = [str(p) for p in protected_paths()]
    if sandbox_available():
        sandbox["failIfUnavailable"] = True  # never run unsandboxed silently
    else:
        sandbox["enabled"] = False  # native Windows has no sandbox; doctor warns

    hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]
    hook["command"] = hook["command"].replace("{{GUARD_COMMAND}}", guard_command())

    # Student extensions: only these two keys, merged last, never able to touch the locks above.
    student, _problems = extensions.load()
    if student["enabledPlugins"]:
        settings["enabledPlugins"] = student["enabledPlugins"]
    if student["extraKnownMarketplaces"]:
        settings["extraKnownMarketplaces"] = student["extraKnownMarketplaces"]
    return settings


def settings_path() -> Path:
    return claude_config_dir() / "settings.json"


def write_settings() -> Path:
    """Regenerate settings.json from the package template, replacing whatever is there."""
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(build_settings(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path

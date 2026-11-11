"""settings.json that Hokma generates for its own CLAUDE_CONFIG_DIR.

The agent has four locks (real-money orders, secrets, deleting, writing outside the working folder; see
`hokma/kit/hardblock.py`) and the protection that keeps them on (`hokma/kit/guard.py`). Everything else is free, so
there is no sandbox and no `permissions.deny` list here: a sandbox would stop the installers (npm, pip, winget, brew)
and the plugins' own first-start downloads from writing where they must.

1. PreToolUse hooks, both fail CLOSED:
   `hokma/kit/guard.py` resolves real paths for Edit, MultiEdit, Write and NotebookEdit and blocks the files of the
   hooks and the settings registration; `hokma/kit/hardblock.py` (matcher `.*`, every tool) applies the four locks of
   both permission modes. The template pins `"disableAllHooks": false` and Hokma launches `claude` with
   `--setting-sources user`, so a settings file in the working folder cannot switch the hooks off.
2. Regeneration: the file is rebuilt from the package template at EVERY Claude launch and passed
   explicitly with --settings, so an edit by the agent does not survive the next launch (and guard.py refuses an edit
   that would take the hooks out of it while the session is running).

The student's extensions (`enabledPlugins`, `extraKnownMarketplaces`, from
<workdir>/.hokma/extensions.json) are merged in AFTER the template and can only add those two keys:
they never touch the hooks. Plugins installed from inside Claude Code are absorbed from the
live settings.json before it is rebuilt (`extensions.absorb_live`), so a launch or an update keeps them.
"""
import json
import shlex
import sys
from pathlib import Path

from hokma.core.config import claude_config_dir, hokma_data_dir, hokma_home
from hokma.kit import extensions


TEMPLATE = Path(__file__).resolve().parent / "templates" / "settings.base.json"
GUARD_FILE = Path(__file__).resolve().parent / "guard.py"
HARDBLOCK_FILE = Path(__file__).resolve().parent / "hardblock.py"


def settings_path() -> Path:
    return claude_config_dir() / "settings.json"


def protected_paths() -> list[Path]:
    """What keeps the locks on: the code of the hooks (the `hokma` package they run from) and the interpreter that runs
    them (`<data>/runtime`, `<data>/python`), the folders that lead the engine's PATH (`<data>/bin`, `<data>/tools`: only
    `hokma install-tools` writes them; a plugin installs its dependencies in its own folders, `~/.bun`, or the user's
    npm and pip prefixes, which go at the end of the PATH), the settings files that register them (`settings.json` and the copy
    `settings.engine.json` made for key helpers) and the folder of the block log. Nothing else is protected: Hokma's
    data folder and its config are the agent's to change."""
    package = Path(__file__).resolve().parent.parent
    config, data = claude_config_dir(), hokma_data_dir()
    return [package, data / "runtime", data / "python", data / "bin", data / "tools",
            settings_path(), config / "settings.engine.json", data / "guard"]


def guard_command() -> str:
    """Hook command: `cd` into the data dir, then run the guard by ABSOLUTE PATH with an
    isolated interpreter (-I: no cwd, no script dir, no PYTHON* variables on sys.path), so nothing
    planted in the working folder can replace it. `|| exit 2` makes a failed cd, a crash or a missing
    interpreter block the tool call (any other non-zero hook exit code would not block). Hooks run
    through a POSIX shell (Git Bash on Windows) [ASSUMED]."""
    args = " ".join(f"--protect {shlex.quote(str(p))}" for p in protected_paths())
    return (f"cd {shlex.quote(str(hokma_data_dir()))} && "
            f"{shlex.quote(sys.executable)} -I {shlex.quote(str(GUARD_FILE))} {args} || exit 2")


def hardblock_command() -> str:
    """Hook command of the always-on protections: same launch as the guard (absolute path, `-I`, from the data
    dir, `|| exit 2`), plus the folders it needs: data dir, working folder and the protected paths."""
    args = [f"--data {shlex.quote(str(hokma_data_dir()))}", f"--workdir {shlex.quote(str(hokma_home()))}"]
    args += [f"--protect {shlex.quote(str(p))}" for p in protected_paths()]
    return (f"cd {shlex.quote(str(hokma_data_dir()))} && "
            f"{shlex.quote(sys.executable)} -I {shlex.quote(str(HARDBLOCK_FILE))} {' '.join(args)} || exit 2")


def build_settings() -> dict:
    settings = json.loads(TEMPLATE.read_text(encoding="utf-8"))

    commands = {"{{GUARD_COMMAND}}": guard_command(), "{{HARDBLOCK_COMMAND}}": hardblock_command()}
    for group in settings["hooks"]["PreToolUse"]:
        for hook in group["hooks"]:
            for placeholder, command in commands.items():
                hook["command"] = hook["command"].replace(placeholder, command)

    # Student extensions: only these two keys, merged last, never able to touch the locks above.
    # MERGE, never replace: the authoritative copy wins, and plugins/marketplaces the student installed from
    # inside Claude Code (live settings.json, plugins/known_marketplaces.json) are kept after validation.
    student, _problems = extensions.load()
    live, _live_problems = extensions.live_additions(student)
    merged = {key: {**live[key], **student[key]} for key in ("enabledPlugins", "extraKnownMarketplaces")}
    extensions.dedupe_marketplaces(merged)       # one entry per repository
    for key, value in merged.items():
        if value:
            settings[key] = value
    return settings


def write_settings() -> Path:
    """Regenerate settings.json from the package template. Before replacing it, the plugins and marketplaces
    Claude Code recorded there are absorbed into the student's extensions; nothing else is carried over."""
    path = settings_path()
    extensions.absorb_live()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(build_settings(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path

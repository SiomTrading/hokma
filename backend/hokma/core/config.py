"""Hokma configuration and paths.

Two separate places, on purpose:
- the working folder (HOKMA_HOME, default ~/Hokma): where the student and the agent work;
- the data folder (HOKMA_DATA_DIR): Hokma runtime and the isolated Claude config. It lives OUTSIDE
  the working folder so the agent cannot edit its own settings, hooks or Hokma code.

Runtime layout inside the data folder (blue-green, so an update can be built next to the running
version and switched with one atomic file replace):

    <data>/current                 text file: "a" or "b", the active slot
    <data>/runtime/a/{app,venv,manifest.json}
    <data>/runtime/b/{app,venv,manifest.json}
    <data>/bin/uv, <data>/python, <data>/cache   shared by both slots
    <data>/claude-config                         isolated CLAUDE_CONFIG_DIR
    <data>/kit-baseline.json                     kit files as last installed (for kit updates)
    <data>/owner.json                            owner and fund names filled into the kit text
    <data>/workdir                               working folder chosen in the app (wins over HOKMA_HOME)
"""
import os
import sys
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
SLOTS = ("a", "b")


def hokma_home() -> Path:
    """Working folder: the one the owner chose in the app (`<data>/workdir`), else HOKMA_HOME, else ~/Hokma.

    The installer exports HOKMA_HOME on every launch, so a folder chosen in the app has to win over it or the
    choice would never take effect."""
    chosen = chosen_workdir()
    if chosen is not None:
        return chosen
    return Path(os.environ.get("HOKMA_HOME", Path.home() / "Hokma"))


def workdir_pointer() -> Path:
    """Text file with the working folder chosen in the app. Lives in the protected data dir."""
    return hokma_data_dir() / "workdir"


def chosen_workdir() -> Path | None:
    try:
        value = workdir_pointer().read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return Path(value) if value and Path(value).is_absolute() else None


def hokma_data_dir() -> Path:
    """Runtime + Claude config folder, outside the working folder."""
    override = os.environ.get("HOKMA_DATA_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Hokma"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Hokma"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "hokma"


def claude_config_dir() -> Path:
    """Isolated Claude config dir Hokma expects (never ~/.claude, never inside the workdir)."""
    return hokma_data_dir() / "claude-config"


def launcher_path() -> Path:
    """The `hokma` command placed on the user's PATH."""
    name = "hokma.cmd" if sys.platform == "win32" else "hokma"
    return Path.home() / ".local" / "bin" / name


def runtime_root() -> Path:
    return hokma_data_dir() / "runtime"


def slot_dir(slot: str) -> Path:
    if slot not in SLOTS:
        raise ValueError(f"invalid slot {slot!r}")
    return runtime_root() / slot


def other_slot(slot: str) -> str:
    return "b" if slot == "a" else "a"


def current_pointer() -> Path:
    return hokma_data_dir() / "current"


def current_slot() -> str | None:
    """Active slot, or None if Hokma is not installed (dev checkout) or the pointer is invalid."""
    try:
        value = current_pointer().read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return value if value in SLOTS else None


def extensions_path() -> Path:
    """AUTHORITATIVE student extensions, in the protected data dir (the agent cannot write there)."""
    return hokma_data_dir() / "extensions.json"


def workdir_hokma_dir() -> Path:
    """<workdir>/.hokma: protected from the agent as a whole; holds the informational mirror."""
    return hokma_home() / ".hokma"


def extensions_mirror_path() -> Path:
    """Informational copy of the extensions, versioned in the student's Git. Never read for settings."""
    return workdir_hokma_dir() / "extensions.json"


def kit_report_path() -> Path:
    """What the last kit sync did (added, updated, conflicts); read by the UI later."""
    return hokma_data_dir() / "kit-report.json"


def owner_path() -> Path:
    """Owner and fund names (owner.json), in the protected data dir, never in the working folder."""
    return hokma_data_dir() / "owner.json"


def kit_baseline_path() -> Path:
    return hokma_data_dir() / "kit-baseline.json"


def dashboard_dist() -> Path:
    """Compiled dashboard build (dashboard/dist in the app folder)."""
    return Path(__file__).resolve().parents[3] / "dashboard" / "dist"


def requests_dir() -> Path:
    """Inbox for extension requests: a sibling of the data dir that the agent's sandbox may write.
    Contents are untrusted proposals; applying one needs the student (see hokma.kit.requests)."""
    data = hokma_data_dir()
    return data.parent / f"{data.name}-requests"

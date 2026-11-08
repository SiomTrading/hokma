"""Working folder: where it can live, the in-app folder browser, and putting a chosen folder to work.

Choosing a folder creates it if needed, runs `git init` when it is not a repository, points Hokma at it
(`<data>/workdir`), installs the kit there and writes the Claude settings. The previous folder is never touched.
Git runs only in a folder that has no `.git` yet, with the owner's and the system Git config ignored, so nothing a
repository could plant is executed.
"""
from __future__ import annotations

import os
import re
import string
import subprocess
import sys
from pathlib import Path

from hokma import __version__
from hokma.core.proc import spawn_kwargs
from hokma.core.config import hokma_data_dir, hokma_home, kit_baseline_path, requests_dir, workdir_pointer
from hokma.setup.messages import SetupError, t

MAX_DIRS = 1000
_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_SYNC_FOLDERS = (
    ("OneDrive", ("OneDrive",)),
    ("Dropbox", ("Dropbox",)),
    ("Google Drive", ("Google Drive", "GoogleDrive", "My Drive", "Mi unidad")),
    ("iCloud Drive", ("Library/Mobile Documents/com~apple~CloudDocs",)),
)


# ---------------------------------------------------------------- safety

def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _protected_roots() -> list[Path]:
    from hokma.kit.integrity import app_root

    roots = [hokma_data_dir(), requests_dir(), app_root()]
    resolved: list[Path] = []
    for root in roots:
        try:
            resolved.append(root.resolve())
        except OSError:
            resolved.append(root)
    return resolved


def check_allowed(path: Path) -> None:
    """Refuse a folder that is, contains or sits inside Hokma's own data or program."""
    if path.parent == path:
        raise SetupError(400, t("setup.err.path_root"))
    for root in _protected_roots():
        if _inside(path, root) or _inside(root, path):
            raise SetupError(400, t("setup.err.path_protected"))


def parse_path(raw: object) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise SetupError(400, t("setup.err.path_required"))
    text = raw.strip()
    if "\x00" in text:
        raise SetupError(400, t("setup.err.path_required"))
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise SetupError(400, t("setup.err.path_not_absolute"))
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        raise SetupError(400, t("setup.err.path_required")) from None


# ---------------------------------------------------------------- where a folder can live

def _documents() -> Path:
    home = Path.home()
    for name in ("Documents", "Documentos"):
        if (home / name).is_dir():
            return home / name
    return home


def _writable(path: Path) -> bool:
    return path.is_dir() and os.access(path, os.W_OK | os.X_OK)


def _other_disk() -> Path | None:
    if sys.platform == "win32":
        system = (os.environ.get("SystemDrive") or "C:").upper()[:1]
        for letter in string.ascii_uppercase:
            if letter == system:
                continue
            drive = Path(f"{letter}:\\")
            if drive.exists() and _writable(drive):
                return drive / "Hokma"
        return None
    roots: list[Path] = []
    user = os.environ.get("USER") or Path.home().name
    for base in (Path("/media") / user, Path("/run/media") / user, Path("/mnt"), Path("/Volumes")):
        try:
            roots += sorted(p for p in base.iterdir() if p.is_dir())
        except OSError:
            continue
    for root in roots:
        try:
            if root.resolve() == Path("/"):
                continue
        except OSError:
            continue
        if _writable(root):
            return root / "Hokma"
    return None


def _sync_folder() -> tuple[str, Path] | None:
    home = Path.home()
    candidates: list[tuple[str, Path]] = []
    one_drive = os.environ.get("OneDrive")
    if one_drive:
        candidates.append(("OneDrive", Path(one_drive)))
    for provider, names in _SYNC_FOLDERS:
        candidates += [(provider, home / n) for n in names]
    for provider, folder in candidates:
        if _writable(folder):
            return provider, folder / "Hokma"
    return None


def options() -> list[dict]:
    """Up to three places for the working folder, computed for this computer."""
    found = [{"id": "documents", "provider": None, "path": str(_documents() / "Hokma")}]
    disk = _other_disk()
    if disk is not None:
        found.append({"id": "disk", "provider": None, "path": str(disk)})
    sync = _sync_folder()
    if sync is not None:
        found.append({"id": "sync", "provider": sync[0], "path": str(sync[1])})
    return found


# ---------------------------------------------------------------- folder browser

def roots() -> list[dict]:
    found = [{"name": "", "kind": "home", "path": str(Path.home())}]
    if sys.platform == "win32":
        for letter in string.ascii_uppercase:
            drive = Path(f"{letter}:\\")
            if drive.exists():
                found.append({"name": f"{letter}:", "kind": "drive", "path": str(drive)})
        return found
    found.append({"name": "/", "kind": "drive", "path": "/"})
    user = os.environ.get("USER") or Path.home().name
    for base in (Path("/media") / user, Path("/run/media") / user, Path("/mnt"), Path("/Volumes")):
        try:
            found += [{"name": p.name, "kind": "drive", "path": str(p)} for p in sorted(base.iterdir()) if p.is_dir()]
        except OSError:
            continue
    return found


def list_dirs(raw: str | None) -> dict:
    """Folders only (hidden ones left out). Without a path: the places a browse starts from."""
    if not raw:
        return {"path": None, "parent": None, "dirs": [], "roots": roots()}
    path = parse_path(raw)
    if not path.exists():                       # a suggested folder that does not exist yet: browse from its nearest parent
        while not path.exists() and path.parent != path:
            path = path.parent
    if not path.is_dir():
        raise SetupError(400, t("setup.err.not_directory"))
    dirs: list[dict] = []
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.name.startswith(".") or len(dirs) >= MAX_DIRS:
                    continue
                try:
                    if entry.is_dir():
                        dirs.append({"name": entry.name, "path": str(Path(entry.path))})
                except OSError:
                    continue
    except OSError:
        pass
    dirs.sort(key=lambda d: d["name"].lower())
    parent = str(path.parent) if path.parent != path else None
    return {"path": str(path), "parent": parent, "dirs": dirs, "roots": roots()}


def make_dir(parent_raw: object, name: object) -> dict:
    parent = parse_path(parent_raw)
    if not parent.is_dir():
        raise SetupError(400, t("setup.err.not_directory"))
    if not isinstance(name, str):
        raise SetupError(400, t("setup.err.folder_name"))
    clean = name.strip()
    if (not clean or clean in (".", "..") or _BAD_NAME.search(clean) or clean.endswith((".", " "))
            or clean.split(".")[0].lower() in _RESERVED or len(clean) > 100):
        raise SetupError(400, t("setup.err.folder_name"))
    target = parent / clean
    check_allowed(target)
    if target.exists():
        raise SetupError(409, t("setup.err.folder_exists"))
    try:
        target.mkdir()
    except OSError as exc:
        raise SetupError(500, t("setup.err.create_failed", error=exc.strerror or str(exc))) from exc
    return {"path": str(target)}


# ---------------------------------------------------------------- applying a folder

def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        env.pop(name, None)
    return env


def is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def git_init(path: Path) -> bool:
    """`git init` unless the folder already has a `.git`. True when it ran."""
    if is_git_repo(path):
        return False
    try:
        proc = subprocess.run(["git", "init", "--quiet"], cwd=str(path), env=_git_env(), capture_output=True,
                              text=True, timeout=30, stdin=subprocess.DEVNULL, **spawn_kwargs())
    except FileNotFoundError:
        raise SetupError(409, t("setup.err.git_missing")) from None
    except (OSError, subprocess.SubprocessError) as exc:
        raise SetupError(500, t("setup.err.git_failed", error=str(exc)[:200])) from exc
    if proc.returncode != 0:
        raise SetupError(500, t("setup.err.git_failed", error=(proc.stderr or proc.stdout).strip()[:200]))
    return True


def _write_pointer(path: Path) -> None:
    pointer = workdir_pointer()
    pointer.parent.mkdir(parents=True, exist_ok=True)
    tmp = pointer.with_suffix(".tmp")
    tmp.write_text(str(path) + "\n", encoding="utf-8")
    tmp.replace(pointer)


def apply(raw: object) -> dict:
    """Create/prepare the folder and make it the working folder. Returns what was done."""
    from hokma.kit.integrity import app_root
    from hokma.kit.settings import write_settings
    from hokma.kit.sync import sync_kit

    path = parse_path(raw)
    check_allowed(path)
    if path.exists() and not path.is_dir():
        raise SetupError(400, t("setup.err.not_directory"))
    created = not path.exists()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SetupError(500, t("setup.err.create_failed", error=exc.strerror or str(exc))) from exc
    if not _writable(path):
        raise SetupError(400, t("setup.err.not_writable"))
    initialized = git_init(path)

    previous = hokma_home()
    try:
        same = previous.resolve() == path
    except OSError:
        same = False
    _write_pointer(path)
    if not same:
        # The baseline remembers what was installed in the OLD folder; with it the new folder would count as
        # "the owner deleted the kit files" and receive nothing.
        try:
            kit_baseline_path().unlink()
        except FileNotFoundError:
            pass
    try:
        report = sync_kit(app_root() / "kit" / "template", version=__version__)
        write_settings()
    except (OSError, ValueError) as exc:
        raise SetupError(500, t("setup.err.kit_sync_failed", error=str(exc)[:200])) from exc
    return {"path": str(path), "created": created, "git_initialized": initialized, "git": is_git_repo(path),
            "kit": report.to_dict(), "previous": str(previous)}

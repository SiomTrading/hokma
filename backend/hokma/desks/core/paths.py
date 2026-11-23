"""Where a desk keeps its own files (SPEC-017: AH-63).

Everything a desk writes for itself (model caches, audit trails, run records, sealed credentials, delivered files)
lives under `hokma_data_dir()`, never inside the installed package: an update replaces the package, and the owner's
data must survive it. Every function here is pure path arithmetic; nothing is created until `ensure_dir` is called.

The containment check works on the text of the path with the rules of the platform that wrote it, so a Windows path with
spaces or accents is judged with Windows rules even when the check runs on Unix.
"""
from __future__ import annotations

import ntpath
import os
import posixpath
from pathlib import Path
from typing import Any

import hokma
from hokma.core.config import hokma_data_dir

DESKS_SUBDIR = "desks"
CACHE_SUBDIR = "cache"
LLM_CACHE_SUBDIR = "llm"
RUNS_SUBDIR = "runs"
SEALED_FILE = "sealed.json"
MAX_PART = 120


class PathEscape(ValueError):
    """A path part tried to leave the folder it belongs to (absolute path, `..`, a separator, a drive letter)."""


def package_dir() -> Path:
    """The folder of the installed `hokma` package: user data must never be inside it."""
    return Path(hokma.__file__).resolve().parent


def _module(flavour: str) -> Any:
    if flavour == "windows":
        return ntpath
    if flavour == "posix":
        return posixpath
    raise ValueError(f"unknown path flavour: {flavour!r}")


def is_within(base: str, candidate: str, *, flavour: str | None = None) -> bool:
    """True when `candidate` is `base` itself or sits below it. Lexical: it does not touch the disk.

    `flavour` is "windows" or "posix"; by default the one of the running platform."""
    flavour = flavour or ("windows" if os.name == "nt" else "posix")
    mod = _module(flavour)
    norm_base = mod.normcase(mod.normpath(base))
    norm_candidate = mod.normcase(mod.normpath(candidate))
    if mod.splitdrive(norm_base)[0] != mod.splitdrive(norm_candidate)[0]:
        return False
    try:
        return mod.commonpath([norm_base, norm_candidate]) == norm_base
    except ValueError:
        return False


def check_part(part: str, *, flavour: str | None = None) -> str:
    """One path component. Rejects empty names, separators, drive letters, NUL, dot names and very long names."""
    if not isinstance(part, str) or not part or part != part.strip():
        raise PathEscape(f"bad path part: {part!r}")
    if len(part) > MAX_PART or "\x00" in part:
        raise PathEscape(f"bad path part: {part[:40]!r}")
    if part in (".", "..") or set(part) == {"."}:
        raise PathEscape(f"bad path part: {part!r}")
    if any(sep in part for sep in ("/", "\\", ":")):
        raise PathEscape(f"a path part cannot hold a separator or a drive: {part!r}")
    return part


def user_path(*parts: str) -> Path:
    """A path under `hokma_data_dir()`. Every part is checked; the result is guaranteed to be inside it."""
    base = hokma_data_dir()
    target = base.joinpath(*(check_part(p) for p in parts))
    if not is_within(str(base), str(target)):
        raise PathEscape(f"path leaves the data folder: {target}")
    return target


def assert_outside_package(path: Path) -> Path:
    """Raises PathEscape when `path` is inside the installed package."""
    if is_within(str(package_dir()), str(Path(path).resolve())):
        raise PathEscape(f"user data cannot live inside the package: {path}")
    return path


def desks_dir() -> Path:
    return assert_outside_package(user_path(DESKS_SUBDIR))


def desk_dir(desk_id: int) -> Path:
    """The folder of one desk: `<data>/desks/<id>`."""
    if isinstance(desk_id, bool) or not isinstance(desk_id, int) or desk_id < 1:
        raise PathEscape(f"bad desk id: {desk_id!r}")
    return assert_outside_package(user_path(DESKS_SUBDIR, str(desk_id)))


def cache_dir() -> Path:
    return assert_outside_package(user_path(CACHE_SUBDIR))


def llm_cache_dir() -> Path:
    """Where model answers are cached and audited."""
    return assert_outside_package(user_path(CACHE_SUBDIR, LLM_CACHE_SUBDIR))


def runs_dir() -> Path:
    """Where unattended runs leave their record."""
    return assert_outside_package(user_path(RUNS_SUBDIR))


def sealed_file() -> Path:
    """The file that holds sealed credentials."""
    return assert_outside_package(user_path(SEALED_FILE))


def ensure_dir(path: Path) -> Path:
    """Create a folder (and its parents) if it is missing; returns it."""
    path.mkdir(parents=True, exist_ok=True)
    return path

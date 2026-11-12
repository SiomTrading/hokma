"""The real folder of the Python that uv installed for Hokma, never the link uv puts next to it (bug 1093).

uv keeps `<data>/python/cpython-3.12.13-windows-x86_64-none` (the real folder) and makes `cpython-3.12-windows-x86_64-none`
a junction to it. Windows Redirection Guard can refuse to cross that junction (WinError 448, "untrusted mount point"),
and a venv whose `home` goes through it cannot start Python. Hokma builds its venvs from the real folder and removes
the junction: deleting a junction removes only the link, never the folder it points to.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path


def _minor(version: str) -> str:
    return ".".join(version.strip().split(".")[:2])


def is_link(path: Path) -> bool:
    """A symlink or a junction. An unreadable path counts as a link (Windows answers 448 for the ones it distrusts)."""
    try:
        junction = getattr(os.path, "isjunction", None)
        return path.is_symlink() or bool(junction and junction(path))
    except OSError:
        return True


def real_python(root: Path, version: str, windows: bool | None = None) -> Path | None:
    """The interpreter of the newest real patch folder `cpython-<minor>.<patch>-*` under `root`, or None."""
    windows = sys.platform == "win32" if windows is None else windows
    pattern = re.compile(r"^cpython-" + re.escape(_minor(version)) + r"\.(\d+)-")
    best: tuple[int, Path] | None = None
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return None
    for entry in entries:
        match = pattern.match(entry.name)
        if not match or is_link(entry):
            continue
        exe = entry / ("python.exe" if windows else "bin/python3")
        try:
            usable = exe.is_file()
        except OSError:
            usable = False
        if usable and (best is None or int(match.group(1)) > best[0]):
            best = (int(match.group(1)), exe)
    return best[1] if best else None


def drop_links(root: Path) -> list[Path]:
    """Remove the links `cpython-*` directly under `root` (the minor version junction). Returns the ones removed."""
    removed: list[Path] = []
    try:
        entries = sorted(root.glob("cpython-*"))
    except OSError:
        return removed
    for entry in entries:
        if not is_link(entry):
            continue
        try:
            os.rmdir(entry)                      # a junction: only the link goes
        except OSError:
            try:
                entry.unlink()                   # a symlink
            except OSError:
                continue
        removed.append(entry)
    return removed

"""The ONE list of places the agent writes as a matter of course: the working folder and the requests inbox.

There is no Bash sandbox any more (task 3169); the list stays because the extension validators (marketplace sources,
MCP commands) compare against `agent_writable_roots()` with `hokma.kit.guard.under_any`, the single path comparator of
Hokma: a plugin source or an MCP program that the agent itself can rewrite is called out. `agent_writable_roots()` adds
the temp folders, with every path resolved by realpath.
"""
import os
import tempfile
from pathlib import Path

from hokma.core.config import hokma_home, requests_dir


def sandbox_allow_write() -> list[Path]:
    return [hokma_home(), requests_dir()]


def temp_dirs() -> list[Path]:
    return [Path(tempfile.gettempdir()), Path("/tmp"), Path("/var/tmp"), Path("/private/tmp")]


def agent_writable_roots() -> list[Path]:
    """Sandbox allowWrite plus temp folders, as real paths, without duplicates."""
    roots: list[Path] = []
    for p in [*sandbox_allow_write(), *temp_dirs()]:
        real = Path(os.path.realpath(p))
        if real not in roots:
            roots.append(real)
    return roots

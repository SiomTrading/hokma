"""The ONE list of places the agent can write.

`sandbox_allow_write()` is what goes into the sandbox `allowWrite`; `agent_writable_roots()` adds the temp
folders the sandbox also lets commands use, with every path resolved by realpath. The extension validators
(marketplace sources, MCP commands) compare against `agent_writable_roots()` with `hokma.kit.guard.under_any`,
the single path comparator of Hokma, so they can never disagree with the sandbox.
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

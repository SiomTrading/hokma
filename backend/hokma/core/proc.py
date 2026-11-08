"""One place that decides how Hokma launches child processes.

On Windows a child started from a windowless parent (the packaged app) would pop a console window.
`spawn_kwargs()` returns the keyword arguments that prevent it; every process launch in this package
spreads it: `subprocess.run(argv, **spawn_kwargs())`. A static test enforces that.
"""
from __future__ import annotations

import subprocess
import sys
from typing import Any

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)


def spawn_kwargs(detached: bool = False, extra_flags: int = 0) -> dict[str, Any]:
    """Popen/run/asyncio keyword arguments that keep a child from opening a console window.

    Windows: `creationflags` with CREATE_NO_WINDOW (detached adds DETACHED_PROCESS and a new process
    group) OR-ed with `extra_flags`. POSIX: nothing, or `start_new_session` when detached.
    The platform is read at call time.
    """
    if sys.platform == "win32":
        flags = CREATE_NO_WINDOW | extra_flags
        if detached:
            flags |= DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        return {"creationflags": flags}
    return {"start_new_session": True} if detached else {}

"""The permission mode of the owner and what it means for a `claude` launch.

`ask`  : `--permission-mode default`, the owner answers each permission request in the chat.
`auto` : `--permission-mode bypassPermissions`, nothing is asked. The always-on protections (hokma.kit.hardblock)
         apply in both modes: a hook that exits with code 2 still blocks the call under bypassPermissions.

The mode lives in `profile.permission_mode`. Only the owner's endpoint (session token) and the first-run step write
it; there is no tool, setting file or command an agent can use to change it.
"""
from __future__ import annotations

import os
from typing import Any

MODES = ("ask", "auto")
DEFAULT_MODE = "ask"
FLAG = {"ask": "default", "auto": "bypassPermissions"}


def flag_for(mode: str) -> str:
    return FLAG.get(mode, FLAG[DEFAULT_MODE])


def current_mode(db: Any = None) -> str:
    """The saved mode; `ask` when there is no profile or no database yet (the safe, old behaviour)."""
    try:
        from hokma.db import current_version, get_database

        if db is None:
            if current_version() == 0:
                return DEFAULT_MODE
            db = get_database()
        with db.read() as u:
            profile = u.profile.get()
        return profile.permission_mode if profile is not None and profile.permission_mode in MODES else DEFAULT_MODE
    except Exception:
        return DEFAULT_MODE


def work_env(project_path: str, conversation_id: str = "", task_id: str = "", agent: str = "") -> dict[str, str]:
    """Variables the hooks and the MCP server read. They are set on the `claude` process by the engine, so the agent
    cannot change them for its own hooks."""
    env = {"HOKMA_WORK_ROOT": os.path.abspath(project_path)}
    if conversation_id:
        env["HOKMA_CONVERSATION_ID"] = str(conversation_id)
    if task_id:
        env["HOKMA_TASK_ID"] = str(task_id)
    if agent:
        env["HOKMA_AGENT"] = str(agent)
    return env


def snapshots_wanted(mode: str) -> bool:
    """A restore point is taken before every turn in `auto` mode."""
    return mode == "auto"

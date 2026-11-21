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
# What leaves the computer on the owner's behalf, and what moves the envelope of the money: asked in `auto` too. The owner fixes the
# envelope and the agents operate inside it (SPEC-015 bug 1220): raising the capital of a desk, loosening or turning off a limit
# or adding an asset under `assets_only` (desk_money_set), and assigning or taking off the account of a desk. Lowering the
# capital or tightening a limit is done with desk_configure, which refuses anything that loosens (the service compares with the
# saved value inside the transaction), so the permission does not need to read the arguments. No tool turns the orders of a
# broker on: that is the owner's switch in the app.
ALWAYS_ASK = ("mcp__hokma__gmail_send", "mcp__hokma__desk_money_set", "mcp__hokma__desk_assign_account",
              "mcp__hokma__desk_unassign_account")
FLAG = {"ask": "default", "auto": "bypassPermissions"}


def flag_for(mode: str) -> str:
    return FLAG.get(mode, FLAG[DEFAULT_MODE])


def asks(mode: str, tool: str) -> bool:
    """Whether the owner must answer before this call runs. `tool` is the plain or the `mcp__hokma__` name."""
    return mode != "auto" or tool.removeprefix("mcp__hokma__") in (x.removeprefix("mcp__hokma__") for x in ALWAYS_ASK)


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

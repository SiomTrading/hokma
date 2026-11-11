"""The environment an engine process gets: an allow list, the same for every engine (Claude Code included).

Anything not named here is dropped: provider keys the student has in the shell that do not belong to the active
connection, Hokma's session token, market keys. The credential of the active connection is added by the caller,
and only when the engine accepts no other way in.

What a plugin or an installer needs to run is not dropped: the places it installs into (`BUN_INSTALL`, the npm
prefix and cache, `CARGO_HOME`, `GOPATH`, ...) and the PATH, which always starts with Hokma's own tool folders (uv,
Node, Bun, Python; see `hokma.core.tools`) so the hooks of plugins find them.
"""
from __future__ import annotations

import fnmatch
import os
from typing import Mapping

from hokma.core import tools

_SYSTEM = ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR",
           "LANG", "LC_*", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES*", "XDG_*", "SHELL", "TERM",
           "USER", "USERNAME", "LOGNAME", "HOMEDRIVE", "HOMEPATH", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS",
           "OS", "COMPUTERNAME", "SYSTEMDRIVE")
_NETWORK = ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "ALL_PROXY", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
            "NODE_EXTRA_CA_CERTS")
_TOOLING = ("BUN_INSTALL", "BUN_INSTALL_CACHE_DIR", "NPM_CONFIG_PREFIX", "NPM_CONFIG_CACHE", "NPM_CONFIG_USERCONFIG",
            "NPM_CONFIG_REGISTRY", "NPM_CONFIG_UPDATE_NOTIFIER", "NPM_CONFIG_FUND", "NPM_CONFIG_AUDIT", "COREPACK_HOME",
            "CARGO_HOME", "RUSTUP_HOME", "GOPATH", "GOROOT", "PYTHONUSERBASE", "PNPM_HOME", "NVM_DIR", "VOLTA_HOME",
            "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "ProgramW6432", "ALLUSERSPROFILE",
            "PUBLIC")
_HOKMA = ("HOKMA_COMMAND_TIMEOUT", "HOKMA_HOME", "HOKMA_DATA_DIR", "HOKMA_WORK_ROOT", "HOKMA_CONVERSATION_ID", "HOKMA_TASK_ID", "HOKMA_AGENT", "HOKMA_LANG",
          "CLAUDE_CODE_GIT_BASH_PATH")

ALLOWED = _SYSTEM + _NETWORK + _TOOLING + _HOKMA


def _wanted(name: str) -> bool:
    return any(fnmatch.fnmatchcase(name.upper(), pattern) for pattern in ALLOWED)


# Nothing an engine starts may wait for someone to type (task 3175): a `git clone` on Windows waited for Git
# Credential Manager with no window to show, and the chat stayed "running". Git, GCM, pip and the GitHub CLI fail at
# once instead, with their own message ("could not read Username", "authentication failed").
NON_INTERACTIVE = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "PIP_NO_INPUT": "1",
                   "GH_PROMPT_DISABLED": "1"}


def command_limits() -> dict[str, str]:
    """Claude Code's own Bash tool honours the same time limit as the commands Hokma runs itself."""
    from hokma.core.config import command_timeout

    ms = str(command_timeout() * 1000)
    return {"BASH_DEFAULT_TIMEOUT_MS": ms, "BASH_MAX_TIMEOUT_MS": ms}


def filtered_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """The allow-listed part of `source` (default: the process environment), never interactive, with the command
    time limit."""
    env = {k: v for k, v in (os.environ if source is None else source).items() if _wanted(k)}
    env.update(NON_INTERACTIVE)
    env.update(command_limits())
    return tools.with_tools(env)

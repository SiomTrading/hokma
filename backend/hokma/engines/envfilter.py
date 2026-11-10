"""The environment an engine process gets: an allow list, the same for every engine (Claude Code included).

Anything not named here is dropped: provider keys the student has in the shell that do not belong to the active
connection, Hokma's session token, market keys. The credential of the active connection is added by the caller,
and only when the engine accepts no other way in.
"""
from __future__ import annotations

import fnmatch
import os
from typing import Mapping

_SYSTEM = ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR",
           "LANG", "LC_*", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES*", "XDG_*", "SHELL", "TERM",
           "USER", "USERNAME", "LOGNAME", "HOMEDRIVE", "HOMEPATH", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS",
           "OS", "COMPUTERNAME", "SYSTEMDRIVE")
_NETWORK = ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "ALL_PROXY", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
            "NODE_EXTRA_CA_CERTS")
_HOKMA = ("HOKMA_HOME", "HOKMA_DATA_DIR", "HOKMA_WORK_ROOT", "HOKMA_CONVERSATION_ID", "HOKMA_TASK_ID", "HOKMA_AGENT", "HOKMA_LANG",
          "CLAUDE_CODE_GIT_BASH_PATH")

ALLOWED = _SYSTEM + _NETWORK + _HOKMA


def _wanted(name: str) -> bool:
    return any(fnmatch.fnmatchcase(name.upper(), pattern) for pattern in ALLOWED)


def filtered_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """The allow-listed part of `source` (default: the process environment)."""
    return {k: v for k, v in (os.environ if source is None else source).items() if _wanted(k)}

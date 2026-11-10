"""Codex sign-in by device code, run from the panel: `codex login --device-auth` as a managed child process.

The CLI prints a link and a one-time code. Hokma reads them (colors removed) and the panel shows them; the student
enters the code in the browser. The CLI process finishes by itself when the sign-in completes. Hokma's own
CODEX_HOME is used, never the student's personal one."""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from typing import Any

from hokma.core.config import codex_home_dir
from hokma.core.proc import spawn_kwargs
from hokma.engines.envfilter import filtered_env

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_URL = re.compile(r"https://\S+")
_CODE = re.compile(r"\b[A-Z0-9]{4,}-[A-Z0-9]{4,}\b")
_lock = threading.Lock()
_state: dict[str, Any] = {"state": "idle", "url": "", "code": "", "proc": None}


def _reader(proc: subprocess.Popen) -> None:
    for line in proc.stdout:                                   # type: ignore[union-attr]
        clean = _ANSI.sub("", line)
        with _lock:
            if proc is not _state["proc"]:
                return
            if not _state["url"] and (m := _URL.search(clean)):
                _state["url"] = m.group(0)
            if not _state["code"] and _state["url"] and (m := _CODE.search(clean)):
                _state["code"] = m.group(0)
    code = proc.wait()
    with _lock:
        if proc is _state["proc"]:
            _state["state"] = "done" if code == 0 else "failed"


def start() -> dict[str, Any]:
    binary = shutil.which("codex")
    if not binary:
        return {"state": "no_cli", "url": "", "code": ""}
    with _lock:
        old = _state["proc"]
        if old is not None and old.poll() is None:
            old.terminate()
        env = filtered_env()
        env["CODEX_HOME"] = str(codex_home_dir())
        proc = subprocess.Popen([binary, "login", "--device-auth"], env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **spawn_kwargs())
        _state.update(state="waiting", url="", code="", proc=proc)
    threading.Thread(target=_reader, args=(proc,), daemon=True).start()
    for _ in range(100):                                       # up to 10 s for the link and the code
        time.sleep(0.1)
        if status()["code"] or _state["state"] != "waiting":
            break
    return status()


def status() -> dict[str, Any]:
    with _lock:
        url = _state["url"] if _state["url"].startswith("https://") else ""
        return {"state": _state["state"], "url": url, "code": _state["code"]}


def cancel() -> None:
    with _lock:
        proc = _state["proc"]
        if proc is not None and proc.poll() is None:
            proc.terminate()
        _state.update(state="idle", url="", code="", proc=None)

"""`<data>/engine.lock`: which supervisor is running Hokma now.

The file holds `{"pid", "port", "started_at"}` and nothing else: no token, no one-time code, so anyone who can
read it learns only where the page is. It is written when the supervisor starts and removed when it ends. A lock
whose pid is not alive is stale and is ignored (and replaced by the next start).

`claim()` creates the file exclusively, so two starts that race cannot both become the supervisor.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from hokma.core import config, control

FRESH_SECONDS = 15.0           # a lock this young belongs to a supervisor that is still opening its control channel


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION, STILL_ACTIVE = 0x1000, 259
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_uint32()
            return bool(kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))) and code.value == STILL_ACTIVE
        finally:
            kernel.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                                     # it exists and belongs to somebody else
    except OSError:
        return False
    return not _is_zombie(pid)


def _is_zombie(pid: int) -> bool:
    """A process that ended but was not collected yet still answers kill(0); it is not running (Linux)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat[stat.rindex(")") + 2:].split()[0] == "Z"
    except (OSError, ValueError, IndexError):
        return False


def read() -> dict | None:
    """The lock as written, or None when there is none or it is not a well formed lock."""
    try:
        data = json.loads(config.engine_lock_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (not isinstance(data, dict) or set(data) != {"pid", "port", "started_at"}
            or not isinstance(data["pid"], int) or isinstance(data["pid"], bool)
            or not isinstance(data["port"], int) or isinstance(data["port"], bool)
            or not isinstance(data["started_at"], str) or not 0 < data["port"] < 65536):
        return None
    return data


def age_seconds(lock: dict) -> float:
    try:
        started = datetime.strptime(lock["started_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return float("inf")
    return (datetime.now(timezone.utc) - started).total_seconds()


def holder_alive(lock: dict) -> bool:
    """True when the lock names a live supervisor of Hokma. The pid alone could have been reused by an unrelated
    program, so unless the lock is fresh the supervisor must also answer `status` on the control channel with the
    same pid."""
    if not pid_alive(lock["pid"]):
        return False
    if age_seconds(lock) < FRESH_SECONDS:
        return True
    reply = control.request("status")
    return bool(reply and reply.get("ok") and reply.get("pid") == lock["pid"])


def _payload(pid: int, port: int) -> bytes:
    return json.dumps({"pid": pid, "port": port, "started_at": _now()}).encode("utf-8")


def claim(port: int, pid: int | None = None) -> bool:
    """Become the supervisor: create the lock exclusively. False when a live supervisor already holds it.
    A stale lock (dead pid, or unreadable) is removed and the claim is tried again."""
    pid = os.getpid() if pid is None else pid
    path = config.engine_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(3):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            held = read()
            if held is not None and held["pid"] != pid and holder_alive(held):
                return False
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(fd, "wb") as f:
            f.write(_payload(pid, port))
        return True
    return False


def take_over(port: int, pid: int | None = None) -> None:
    """Replace the lock unconditionally (the supervisor handed over to a new slot owns it now)."""
    pid = os.getpid() if pid is None else pid
    path = config.engine_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="engine.lock.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(_payload(pid, port))
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def release(pid: int | None = None) -> None:
    """Remove the lock, but only when it is ours: a supervisor that was replaced must not delete its successor's."""
    pid = os.getpid() if pid is None else pid
    held = read()
    if held is not None and held["pid"] == pid:
        try:
            config.engine_lock_path().unlink()
        except FileNotFoundError:
            pass


def remove_stale() -> None:
    """Delete a lock whose pid is dead."""
    held = read()
    if held is not None and not pid_alive(held["pid"]):
        try:
            config.engine_lock_path().unlink()
        except FileNotFoundError:
            pass

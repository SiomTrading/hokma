"""Keep-awake: hold a single machine-wide block on system sleep while Hokma is working.

Windows asks for `ES_SYSTEM_REQUIRED` from a thread of its own (the request lives as long as that thread), macOS runs
`caffeinate -i` tied to this process, other systems do nothing. The display may still dim. The block is released when
the work ends, when the preference is turned off, or when the process exits.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from typing import Any, Protocol

log = logging.getLogger("hokma.power")

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class Blocker(Protocol):
    def start(self) -> bool: ...
    def stop(self) -> None: ...
    def is_started(self) -> bool: ...


class WindowsBlocker:
    """`SetThreadExecutionState` from a dedicated thread, released by the same thread."""

    def __init__(self, kernel32: Any = None) -> None:
        self._kernel32 = kernel32
        self._thread: threading.Thread | None = None
        self._release = threading.Event()
        self._ready = threading.Event()
        self._ok = False

    def _kernel(self) -> Any:
        if self._kernel32 is None:
            import ctypes

            self._kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        return self._kernel32

    def _hold(self) -> None:
        kernel = self._kernel()
        self._ok = bool(kernel.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED))
        self._ready.set()
        if self._ok:
            self._release.wait()
            kernel.SetThreadExecutionState(ES_CONTINUOUS)

    def start(self) -> bool:
        if self.is_started():
            return True
        self._release.clear()
        self._ready.clear()
        self._ok = False
        self._thread = threading.Thread(target=self._hold, name="hokma-keep-awake", daemon=True)
        self._thread.start()
        self._ready.wait(5)
        return self._ok

    def stop(self) -> None:
        thread = self._thread
        self._release.set()
        if thread is not None:
            thread.join(5)
        self._thread = None
        self._ok = False

    def is_started(self) -> bool:
        return self._ok and self._thread is not None and self._thread.is_alive()


class MacBlocker:
    """`caffeinate -i -w <pid>`: no idle sleep while this process lives."""

    def __init__(self, popen: Any = subprocess.Popen) -> None:
        self._popen = popen
        self._proc: Any = None

    def start(self) -> bool:
        if self.is_started():
            return True
        try:
            self._proc = self._popen(["caffeinate", "-i", "-w", str(os.getpid())],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            log.warning("caffeinate could not be started")
            self._proc = None
        return self.is_started()

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def is_started(self) -> bool:
        return self._proc is not None and self._proc.poll() is None


class NullBlocker:
    """Systems without a supported way to hold the block: never active."""

    def start(self) -> bool:
        return False

    def stop(self) -> None:
        return None

    def is_started(self) -> bool:
        return False


def platform_blocker(platform: str | None = None) -> Blocker:
    name = platform or sys.platform
    if name == "win32":
        return WindowsBlocker()
    if name == "darwin":
        return MacBlocker()
    return NullBlocker()


def supported(platform: str | None = None) -> bool:
    return (platform or sys.platform) in ("win32", "darwin")


class KeepAwake:
    """Turn the block on or off (idempotent). `set` returns the resulting state."""

    def __init__(self, blocker: Blocker | None = None) -> None:
        self._blocker = blocker or platform_blocker()
        self._lock = threading.Lock()

    def is_active(self) -> bool:
        return self._blocker.is_started()

    def set(self, on: bool) -> bool:
        with self._lock:
            if on and not self._blocker.is_started():
                self._blocker.start()
            elif not on:
                self._blocker.stop()
            return self._blocker.is_started()

    def sync(self, preference: bool, working: bool) -> bool:
        """The block is held only when the preference is on AND something is running."""
        return self.set(bool(preference) and bool(working))


_instance: dict[str, KeepAwake | None] = {"keep_awake": None}


def keep_awake() -> KeepAwake:
    """The one block of this process."""
    if _instance["keep_awake"] is None:
        _instance["keep_awake"] = KeepAwake()
    return _instance["keep_awake"]  # type: ignore[return-value]

"""Kill a process and everything it started (task 3175).

A Stop in the chat, a command over its time limit and a conversation that is closed all end here. What used to go
wrong: on Windows `taskkill /T` only follows parent links, so a `git` or `uv` whose parent shell had already died
survived, kept the engine's output pipe open and the turn never ended; on POSIX a child that started its own session
left the process group the same way.

* Windows: the process goes into its own Job Object right after it starts; every process it starts joins the job,
  and `TerminateJobObject` ends all of them. `taskkill /T /F` runs as well, for a child created before the job
  existed.
* POSIX: the process leads its own group (`start_new_session`). The descendants are listed before the kill (by
  parent pid, so one that left the group is found too), then the group and each descendant get the signal. On Linux
  the processes that still hold the child's output pipe are found through /proc and killed as well: that catches a
  grandchild that left the group and whose parent already ended, the one that kept a turn open.

`kill()` and `terminate()` block (taskkill, waiting on nothing else): from asyncio call them with `asyncio.to_thread`.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from typing import Any


def _log(message: str) -> None:
    try:
        from hokma.engine import errlog

        errlog.log(message)
    except Exception:                    # noqa: BLE001
        pass


def popen_handle(proc: Any) -> int | None:
    """The Windows process handle of a `subprocess.Popen` or an `asyncio.subprocess.Process`, else None."""
    popen = proc
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        try:
            popen = transport.get_extra_info("subprocess")
        except Exception:                # noqa: BLE001
            popen = None
    handle = getattr(popen, "_handle", None)
    return int(handle) if handle else None


def _children_map() -> dict[int, list[int]]:
    """parent pid -> child pids, from /proc (Linux) or `ps` (macOS and the rest)."""
    out: dict[int, list[int]] = {}
    if os.path.isdir("/proc/self"):
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/stat", "rb") as f:
                    stat = f.read().decode("utf-8", "replace")
                ppid = int(stat[stat.rindex(")") + 2:].split()[1])
            except (OSError, ValueError, IndexError):
                continue
            out.setdefault(ppid, []).append(int(name))
        return out
    from hokma.core.proc import spawn_kwargs

    try:
        done = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=5,
                              stdin=subprocess.DEVNULL, check=False, **spawn_kwargs())
    except (OSError, subprocess.SubprocessError):
        return out
    for line in done.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            out.setdefault(int(parts[1]), []).append(int(parts[0]))
    return out


def descendants(pid: int) -> list[int]:
    """Every process below `pid`, children first. POSIX only; [] on Windows (the job covers them there)."""
    if sys.platform == "win32":
        return []
    tree = _children_map()
    found: list[int] = []
    stack = list(tree.get(pid, []))
    while stack:
        child = stack.pop()
        if child in found or child == pid:
            continue
        found.append(child)
        stack.extend(tree.get(child, []))
    return found


def _pipe_inode(fd: int) -> int | None:
    try:
        return os.fstat(fd).st_ino
    except OSError:
        return None


def start_time(pid: int) -> str | None:
    """A token that changes when the pid is reused: the start time (Linux /proc, else `ps`); None when unknown."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            stat = f.read().decode("utf-8", "replace")
        return stat[stat.rindex(")") + 2:].split()[19]
    except (OSError, ValueError, IndexError):
        pass
    if os.path.isdir("/proc/self") or sys.platform == "win32":
        return None
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5,
                             check=False, stdin=subprocess.DEVNULL).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out or None


def pipe_holders(inode: int) -> list[int]:
    """Linux: the processes (other than this one) with a file descriptor on the pipe `inode`."""
    if not os.path.isdir("/proc/self/fd"):
        return []
    target, me, found = f"pipe:[{inode}]", os.getpid(), []
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == me:
            continue
        try:
            fds = os.listdir(f"/proc/{name}/fd")
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink(f"/proc/{name}/fd/{fd}") == target:
                    found.append(int(name))
                    break
            except OSError:
                continue
    return found


class ProcessTree:
    """The tree under one child process. Create it right after the child starts; `kill()` is safe to call twice."""

    def __init__(self, pid: int, handle: int | None = None) -> None:
        self.pid = pid
        self._job = None
        self._known: dict[int, str | None] = {}
        self._pipes: set[int] = set()
        if sys.platform == "win32" and handle:
            try:
                from hokma.core.winjob import TreeJob

                job = TreeJob()
                job.assign(handle)
                self._job = job
            except OSError as exc:
                _log(f"process {pid}: no job object ({exc}); the kill falls back to taskkill /T")

    @classmethod
    def of(cls, proc: Any) -> "ProcessTree":
        return cls(proc.pid, popen_handle(proc))

    def watch_pipe(self, fd: int | None) -> None:
        """The kill also ends whoever still holds this pipe (the child's stdout), wherever it went (Linux)."""
        inode = _pipe_inode(fd) if fd is not None else None
        if inode is not None:
            self._pipes.add(inode)

    def remember(self) -> None:
        """Note the current descendants (POSIX): a child whose parent dies later is still found by the kill."""
        if sys.platform != "win32":
            self._note(descendants(self.pid))

    def _note(self, pids: Any) -> None:
        for pid in pids:
            if pid not in self._known:
                self._known[pid] = start_time(pid)

    def _same(self, pid: int, started: str | None) -> bool:
        """False when the pid now belongs to another process (it ended and the number was reused)."""
        if started is None:
            return start_time(pid) is None or pid == self.pid
        return start_time(pid) == started

    def terminate(self) -> None:
        """Ask nicely (SIGTERM to the group) on POSIX; Windows has no polite signal for a console-less tree, so it is
        the same as kill()."""
        if sys.platform == "win32":
            self.kill()
            return
        self.remember()
        self._signal_all(signal.SIGTERM)

    def kill(self) -> None:
        if sys.platform == "win32":
            if self._job is not None:
                try:
                    self._job.terminate()
                except OSError as exc:
                    _log(f"process {self.pid}: TerminateJobObject failed: {exc}")
            try:
                from hokma.core.proc import spawn_kwargs

                subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.pid)], capture_output=True, timeout=10,
                               check=False, stdin=subprocess.DEVNULL, **spawn_kwargs())
            except (OSError, subprocess.SubprocessError):
                pass
            return
        self.remember()
        self._signal_all(signal.SIGKILL)

    def _signal_all(self, sig: int) -> None:
        try:                                           # a group id stays reserved while any member lives: no revalidation
            os.killpg(self.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        for inode in self._pipes:
            self._note(pipe_holders(inode))
        for pid, started in list(self._known.items()):
            if not self._same(pid, started):
                self._known.pop(pid, None)             # the number was reused by someone else: never touched
                continue
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError, OSError):
                self._known.pop(pid, None)

    def close(self) -> None:
        if self._job is not None:
            self._job.close()
            self._job = None

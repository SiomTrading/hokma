"""One `claude` child process: spawn, JSON lines in and out, and a clean kill of the whole process tree."""
import asyncio
import collections
import json
import os
import shutil
import signal
import subprocess
from typing import Any

from hokma.core.proc import CREATE_NEW_PROCESS_GROUP, spawn_kwargs
from hokma.engine.stream import parse_line

LINE_LIMIT = 64 * 1024 * 1024          # one stream-json line can carry a whole file or an image
STDERR_LINES = 40


def find_claude(env: dict[str, str] | None = None) -> str | None:
    return shutil.which("claude", path=(env or os.environ).get("PATH"))


class ClaudeProcess:
    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc
        self._stderr: collections.deque[str] = collections.deque(maxlen=STDERR_LINES)
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    @classmethod
    async def spawn(cls, argv: list[str], env: dict[str, str], cwd: str) -> "ClaudeProcess":
        own_group: dict[str, Any] = {"start_new_session": True} if os.name == "posix" else {}   # the kill reaches the children
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=LINE_LIMIT,
            **spawn_kwargs(extra_flags=CREATE_NEW_PROCESS_GROUP), **own_group)
        return cls(proc)

    @property
    def alive(self) -> bool:
        return self._proc.returncode is None

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr)[-2000:]

    async def _drain_stderr(self) -> None:
        stream = self._proc.stderr
        if stream is None:
            return
        try:
            async for raw in stream:
                self._stderr.append(raw.decode("utf-8", "replace").rstrip())
        except (ValueError, asyncio.LimitOverrunError):
            pass

    async def write(self, message: dict[str, Any]) -> None:
        stdin = self._proc.stdin
        if stdin is None or stdin.is_closing():
            raise BrokenPipeError("claude stdin is closed")
        stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        await stdin.drain()

    async def read(self) -> dict[str, Any] | None:
        """Next message, or None when the process closed its output. Lines that are not JSON are skipped."""
        stdout = self._proc.stdout
        while stdout is not None:
            raw = await stdout.readline()
            if not raw:
                return None
            msg = parse_line(raw)
            if msg is not None:
                return msg
        return None

    async def terminate(self, grace: float = 2.0) -> None:
        """Kill the process and its children; safe to call twice."""
        if self._proc.returncode is None:
            self._signal(signal.SIGTERM if os.name == "posix" else None)
            try:
                await asyncio.wait_for(self._proc.wait(), grace)
            except asyncio.TimeoutError:
                self._signal(signal.SIGKILL if os.name == "posix" else None, force=True)
                try:
                    await asyncio.wait_for(self._proc.wait(), grace)
                except asyncio.TimeoutError:
                    pass
        if self._proc.stdin is not None and not self._proc.stdin.is_closing():
            self._proc.stdin.close()
        await asyncio.gather(self._stderr_task, return_exceptions=True)

    def _signal(self, sig: int | None, force: bool = False) -> None:
        pid = self._proc.pid
        try:
            if os.name == "posix":
                os.killpg(pid, sig)                       # type: ignore[arg-type]
            else:
                cmd = ["taskkill", "/T", "/PID", str(pid)] + (["/F"] if force or sig is None else [])
                subprocess.run(cmd, capture_output=True, timeout=10, check=False, **spawn_kwargs())
        except (ProcessLookupError, PermissionError, OSError, subprocess.SubprocessError):
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass


async def claude_version(claude: str | None = None) -> str | None:
    claude = claude or find_claude()
    if not claude:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(claude, "--version", stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL, **spawn_kwargs())
        out, _ = await asyncio.wait_for(proc.communicate(), 10)
    except (OSError, asyncio.TimeoutError):
        return None
    return out.decode("utf-8", "replace").strip() or None


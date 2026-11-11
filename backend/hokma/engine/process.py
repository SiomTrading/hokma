"""One `claude` child process: spawn, JSON lines in and out, and a clean kill of the whole process tree."""
import asyncio
import collections
import json
import os
import shutil
from typing import Any

from hokma.core.proc import CREATE_NEW_PROCESS_GROUP, spawn_kwargs
from hokma.core.proctree import ProcessTree
from hokma.engine.stream import parse_line

LINE_LIMIT = 64 * 1024 * 1024          # one stream-json line can carry a whole file or an image
STDERR_LINES = 40
DRAIN_WAIT = 2.0                       # seconds the stderr reader gets after the kill before it is left behind


def _trusted_path(path: str | None) -> str:
    """PATH without entries the agent can write: relative or empty ones (the working folder) and anything inside
    Hokma's data folder that is not protected (`<data>/bin`, `<data>/tools`, `<data>/python` are).

    Windows (task 3171): an entry written with a variable (`%USERPROFILE%\\.local\\bin`) or in quotes is read the way
    the shell reads it, and an entry the comparison cannot judge (a disconnected network drive, a cloud folder that
    makes realpath fail) is kept and logged instead of stopping the conversation: this filter is not one of the locks."""
    from hokma.core.config import hokma_data_dir
    from hokma.kit.guard import under_any
    from hokma.kit.settings import protected_paths

    try:
        data, protect = str(hokma_data_dir()), [str(p) for p in protected_paths()]
    except Exception as exc:                 # noqa: BLE001
        _log(f"PATH filter skipped: {type(exc).__name__}: {exc}")
        return path or ""
    keep = []
    for raw in (path or "").split(os.pathsep):
        entry = os.path.expandvars(raw.strip().strip('"')) if raw else raw
        if not entry or not os.path.isabs(entry):
            continue
        try:
            if under_any(entry, [data]) and not under_any(entry, protect):
                continue
        except Exception as exc:             # noqa: BLE001
            _log(f"PATH entry kept without a check ({entry!r}): {type(exc).__name__}: {exc}")
        keep.append(entry)
    return os.pathsep.join(keep)


def _log(message: str) -> None:
    from hokma.engine import errlog

    errlog.log(message)


def find_claude(env: dict[str, str] | None = None) -> str | None:
    """`claude` from a PATH with no folder the agent can write (the protected tool folders stay)."""
    source = env or os.environ
    path = next((v for k, v in source.items() if k.upper() == "PATH"), None)
    return shutil.which("claude", path=_trusted_path(path))


class ClaudeProcess:
    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc
        self._stderr: collections.deque[str] = collections.deque(maxlen=STDERR_LINES)
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        self._tree = ProcessTree.of(proc)              # Windows: its own job object; POSIX: its own group
        self._terminated = False
        for stream in (1, 2):                          # whoever keeps the output open is part of the tree
            try:
                pipe = proc._transport.get_pipe_transport(stream).get_extra_info("pipe")   # type: ignore[union-attr]
                self._tree.watch_pipe(pipe.fileno())
            except Exception:                          # noqa: BLE001
                pass

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
        """Kill the process and every process below it; safe to call twice, never waits forever (task 3175).

        The kill itself runs in a thread (taskkill on Windows can take seconds and must not freeze the other chats).
        After it, the pipes are closed from this side: a grandchild that escaped the kill and still holds stdout can
        no longer keep `read()` waiting, so the turn always ends."""
        if self._terminated:
            return
        self._terminated = True
        if self._proc.returncode is None:
            await asyncio.to_thread(self._tree.terminate)
            try:
                await asyncio.wait_for(self._proc.wait(), grace)
            except asyncio.TimeoutError:
                pass
        await asyncio.to_thread(self._tree.kill)         # also what is left of the tree after the parent ended
        if self._proc.returncode is None:
            try:
                await asyncio.wait_for(self._proc.wait(), grace)
            except asyncio.TimeoutError:
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass
        transport = getattr(self._proc, "_transport", None)
        if transport is not None:
            try:
                transport.close()
            except Exception:                            # noqa: BLE001
                pass
        if self._proc.stdin is not None and not self._proc.stdin.is_closing():
            self._proc.stdin.close()
        try:
            await asyncio.wait_for(asyncio.gather(self._stderr_task, return_exceptions=True), DRAIN_WAIT)
        except asyncio.TimeoutError:
            self._stderr_task.cancel()
        self._tree.close()


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


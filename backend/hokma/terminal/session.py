"""One terminal session: shell choice, folder, environment, limits and lifetime."""
import asyncio
import codecs
import os
import shutil
import sys
from pathlib import Path

from hokma.core.config import claude_config_dir, hokma_home, launcher_path
from hokma.terminal.pty_backend import IS_WINDOWS, open_pty

MAX_SESSIONS = 16
MIN_COLS, MAX_COLS, MIN_ROWS, MAX_ROWS = 2, 500, 1, 200
MAX_INPUT_BYTES = 1024 * 1024
DEFAULT_COLS, DEFAULT_ROWS = 80, 10


def clamp_size(cols: object, rows: object) -> tuple[int, int]:
    def num(value: object, default: int) -> int:
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default
    return (max(MIN_COLS, min(MAX_COLS, num(cols, DEFAULT_COLS))),
            max(MIN_ROWS, min(MAX_ROWS, num(rows, DEFAULT_ROWS))))


def resolve_shell() -> list[str]:
    if IS_WINDOWS:
        for name in ("pwsh.exe", "powershell.exe"):
            found = shutil.which(name)
            if found:
                return [found, "-NoLogo"]
        return [os.environ.get("COMSPEC", "cmd.exe")]
    candidate = os.environ.get("SHELL", "")
    if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return [candidate]
    for fallback in ("/bin/bash", "/bin/zsh", "/bin/sh"):
        if os.access(fallback, os.X_OK):
            return [fallback]
    return ["/bin/sh"]


def _existing_dir(path: str | None) -> str | None:
    if path and Path(path).is_dir():
        return str(Path(path))
    return None


def resolve_cwd(project_id: str | None) -> str:
    """The named project folder; with none named (or unusable) the active project saved in the database,
    read at the moment the shell opens so it never races the projects panel; then the working folder."""
    try:
        from hokma.engine.dbstore import DbProjects
        projects = DbProjects()
        found = _existing_dir(projects.get(project_id).path) if project_id else None
        if found is None:
            found = _existing_dir(projects.default().path)
        if found is not None:
            return found
    except Exception:
        pass
    home = hokma_home()
    if home.is_dir():
        return str(home)
    return str(Path.home())


def build_env() -> dict[str, str]:
    env = dict(os.environ)
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    env["CLAUDE_CONFIG_DIR"] = str(claude_config_dir())
    launcher_dir = launcher_path().parent
    parts = env["PATH"].split(os.pathsep) if env.get("PATH") else []
    if str(launcher_dir) not in parts and launcher_dir.is_dir():
        parts.append(str(launcher_dir))
        env["PATH"] = os.pathsep.join(parts)
    return env


class TerminalSession:
    def __init__(self, pty_proc, cwd: str) -> None:
        self.pty = pty_proc
        self.cwd = cwd
        self.pid: int = pty_proc.pid
        self.queue: asyncio.Queue = asyncio.Queue()
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.closed = False

    @classmethod
    def open(cls, loop: asyncio.AbstractEventLoop, cols: int, rows: int, project_id: str | None) -> "TerminalSession":
        cwd = resolve_cwd(project_id)
        proc = open_pty(resolve_shell(), cwd, build_env(), cols, rows)
        session = cls(proc, cwd)
        proc.start_reading(loop, session.queue)
        return session

    async def next_output(self) -> str | None:
        """Next decoded chunk of terminal output, or None when the shell closed its side."""
        while True:
            chunk = await self.queue.get()
            self.pty.drained()
            if chunk is None:
                return None
            text = self._decoder.decode(chunk)
            if text:
                return text

    def write(self, text: str) -> None:
        data = text.encode("utf-8", "replace")
        if len(data) <= MAX_INPUT_BYTES:
            self.pty.write(data)

    def resize(self, cols: object, rows: object) -> None:
        self.pty.resize(*clamp_size(cols, rows))

    async def exit_code(self) -> int | None:
        return await self.pty.wait_exit(1.0)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self.pty.terminate()


class Registry:
    """Live sessions, so the app can stop every shell when it exits."""

    def __init__(self) -> None:
        self.sessions: set[TerminalSession] = set()

    def full(self) -> bool:
        return len(self.sessions) >= MAX_SESSIONS

    def add(self, session: TerminalSession) -> None:
        self.sessions.add(session)

    def discard(self, session: TerminalSession) -> None:
        self.sessions.discard(session)

    def kill_all_now(self) -> None:
        for session in list(self.sessions):
            session.closed = True
            session.pty.kill_now()
        self.sessions.clear()


registry = Registry()

if "pytest" not in sys.modules:
    import atexit
    atexit.register(registry.kill_all_now)

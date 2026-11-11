"""One terminal session: shell choice, folder, environment, limits and lifetime."""
import asyncio
import codecs
import os
import shutil
import sys
from pathlib import Path

from hokma.core import tools
from hokma.core.config import claude_config_dir, hokma_home, launcher_path
from hokma.terminal.pty_backend import IS_WINDOWS, PtyUnavailable, open_pty

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


def _windows_shells() -> list[list[str]]:
    """PowerShell 7, Windows PowerShell and cmd, in that order, each once. `shutil.which` can return the Store's
    `pwsh.exe` alias (a reparse point a pseudo-terminal cannot start), so the caller tries the next one when it fails."""
    found: list[list[str]] = []
    for name in ("pwsh.exe", "powershell.exe"):
        path = shutil.which(name)
        if path:
            found.append([path, "-NoLogo"])
    root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or r"C:\Windows"
    for cmd in (os.environ.get("COMSPEC", ""), os.path.join(root, "System32", "cmd.exe"), "cmd.exe"):
        if cmd and [cmd] not in found:
            found.append([cmd])
    return found


def shell_candidates() -> list[list[str]]:
    if IS_WINDOWS:
        return _windows_shells()
    return [resolve_shell()]


def resolve_shell() -> list[str]:
    if IS_WINDOWS:
        return _windows_shells()[0]
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


def build_env(plain: bool = False) -> dict[str, str]:
    """The environment of the owner's shell. `plain=True` is the last resort when the shell would not start with the
    full one: what the app itself runs with, plus the terminal type, nothing of Hokma's PATH additions."""
    env = dict(os.environ)
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    if IS_WINDOWS:                         # a shell without these starts with nothing, or not at all
        root = env.get("SYSTEMROOT") or env.get("WINDIR") or r"C:\Windows"
        env.setdefault("SYSTEMROOT", root)
        env.setdefault("WINDIR", root)
        env.setdefault("COMSPEC", os.path.join(root, "System32", "cmd.exe"))
    if plain:
        return env
    env["CLAUDE_CONFIG_DIR"] = str(claude_config_dir())
    key = next((k for k in env if k.upper() == "PATH"), "PATH")
    launcher_dir = launcher_path().parent
    parts = env[key].split(os.pathsep) if env.get(key) else []
    if str(launcher_dir) not in parts and launcher_dir.is_dir():
        parts.append(str(launcher_dir))
        env[key] = os.pathsep.join(parts)
    return tools.with_tools(env, first=False, only_existing=True)   # the tools never shadow what the shell already finds


class TerminalStartError(OSError):
    """No shell would start. `detail` carries every attempt with its real error, for the screen and engine.log."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _reason(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}".strip()[:300]


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
        """Starts the first shell that works: each candidate with the full environment, then with the plain one. A
        missing pywinpty stops at once (PtyUnavailable). When all fail, TerminalStartError lists every attempt."""
        from hokma.engine import errlog

        cwd = resolve_cwd(project_id)
        attempts: list[str] = []
        for plain in (False, True):
            try:
                env = build_env(plain)
            except Exception as exc:       # noqa: BLE001
                attempts.append(f"environment: {_reason(exc)}")
                continue
            for argv in shell_candidates():
                try:
                    proc = open_pty(argv, cwd, env, cols, rows)
                except PtyUnavailable:
                    raise
                except Exception as exc:   # noqa: BLE001  (pywinpty raises its own error types, not only OSError)
                    attempts.append(f"{argv[0]} ({'plain' if plain else 'full'} environment, folder {cwd}, "
                                    f"PATH {len(env.get('PATH', env.get('Path', '')))} characters): {_reason(exc)}")
                    continue
                if attempts:
                    errlog.log("the terminal started after failures:\n" + "\n".join(attempts), scope="terminal")
                session = cls(proc, cwd)
                proc.start_reading(loop, session.queue)
                return session
        detail = "\n".join(attempts) or "no shell found"
        errlog.log("the terminal could not start:\n" + detail, scope="terminal")
        raise TerminalStartError(detail)

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

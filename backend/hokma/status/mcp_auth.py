"""Remote MCP servers with OAuth (for example Lauz), added and authorized from the panel.

Claude Code owns the token: `claude mcp login` stores and refreshes it inside Hokma's isolated CLAUDE_CONFIG_DIR.
Hokma never reads, copies or logs it. `claude mcp login` only runs on a terminal, so it runs on a pseudo-terminal:
the panel shows the authorization link, the owner pastes the final address back and it is forwarded to the child.
`claude mcp login` has no --mcp-config in 2.1.x, so the server is also registered with `claude mcp add --scope user`
in that same folder, and `write_mcp_config()` leaves registered names out of hokma-mcp.json.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

from hokma.core.config import claude_config_dir, hokma_data_dir
from hokma.core.proc import spawn_kwargs
from hokma.db import Actor
from hokma.i18n import t
from hokma.kit import extensions
from hokma.kit.mcp_config import HTTPS_URL
from hokma.setup.claude import _clean_output, claude_path
from hokma.status.extensions_service import LOCAL_MARKETPLACE, ServiceError, _db, _owner, sync_table

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
RESERVED = ("hokma",)
CMD_TIMEOUT = 10
LOGIN_WAIT = 60
LINK_WAIT = 15
LOGIN_LIFETIME = 600
OUTPUT_LIMIT = 8000
PASTE = re.compile(r"^\S{1,4000}$")
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_URL = re.compile(r"https://[^\s\"'<>\x1b\x07]+")


def _strip(text: str) -> str:
    return _ANSI.sub("", _OSC.sub("", text)).replace("\r", "")


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(claude_config_dir())
    return env


def _require_claude() -> str:
    path = claude_path()
    if path is None:
        raise ServiceError(409, t("setup.err.claude_missing"))
    return path


def _run(argv: list[str]) -> tuple[int, str]:
    claude_config_dir().mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=CMD_TIMEOUT, stdin=subprocess.DEVNULL, env=_env(),
                              cwd=str(hokma_data_dir()), **spawn_kwargs())
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return proc.returncode, _strip(proc.stdout + proc.stderr)


def _server(name: str) -> dict:
    data, _ = extensions.load()
    entry = data["mcpServers"].get(name)
    if not isinstance(entry, dict) or entry.get("type") != "http":
        raise ServiceError(404, t("status.mcp.unknown", name=name))
    return entry


# ---------------------------------------------------------------- add

def add_server(name: str, url: str) -> dict:
    name = (name or "").strip()
    url = (url or "").strip()
    if not NAME.match(name) or name in RESERVED:
        raise ServiceError(400, t("status.mcp.bad_name"))
    if not HTTPS_URL.match(url) or len(url) > 2000:
        raise ServiceError(400, t("status.mcp.bad_url"))
    data, _ = extensions.load()
    if name in data["mcpServers"]:
        raise ServiceError(409, t("status.mcp.exists", name=name))
    db = _db()
    with db.write() as unit:
        owner = _owner(unit)
        request = unit.extension_requests.create("install", "mcp", name, LOCAL_MARKETPLACE,
                                                 f"dashboard: add remote MCP {name}", actor=Actor.human(owner))
        unit.extension_requests.approve(request.id, decided_by=owner)
        path = claude_path()
        if path is not None:
            code, output = _run([path, "mcp", "add", "--transport", "http", "--scope", "user", name, url])
            if code != 0:
                message = t("status.mcp.add_failed", output=_clean_output(output) or "-")
                unit.extension_requests.mark_failed(request.id, message)
                raise ServiceError(400, message)
        try:
            warnings = extensions.set_mcp(name, {"type": "http", "url": url})
        except ValueError as exc:
            unit.extension_requests.mark_failed(request.id, str(exc))
            raise ServiceError(400, str(exc)) from exc
        data, _ = extensions.load()
        sync_table(unit, data)
        ext = unit.extensions.find(LOCAL_MARKETPLACE, name)
        unit.extension_requests.mark_applied(request.id, ext.id)
    return {"name": name, "url": url, "warnings": warnings}


# ---------------------------------------------------------------- state

def server_state(name: str) -> dict:
    """needs_auth | connected | error, from `claude mcp get`. Never reads the credentials file."""
    entry = _server(name)
    info = {"name": name, "url": entry.get("url"), "state": "error", "message": ""}
    path = claude_path()
    if path is None:
        info["state"] = "no_cli"
        return info
    code, output = _run([path, "mcp", "get", name])
    low = output.lower()
    if "needs authentication" in low or "401" in low or "not authenticated" in low:
        info["state"] = "needs_auth"
    elif "connected" in low and "failed" not in low:
        info["state"] = "connected"
    else:
        info["message"] = _clean_output(output)[-200:] or "-"
    return info


def servers_state() -> list[dict]:
    data, _ = extensions.load()
    return [server_state(n) for n, e in sorted(data["mcpServers"].items())
            if isinstance(e, dict) and e.get("type") == "http"]


# ---------------------------------------------------------------- login on a pseudo-terminal

class _Pty:
    """Minimal blocking pseudo-terminal child: POSIX pty or pywinpty."""

    def __init__(self, argv: list[str], cwd: str, env: dict[str, str]) -> None:
        self._win = None
        self.proc = None
        if sys.platform == "win32":
            from winpty import PtyProcess      # pywinpty, installed only on Windows
            self._win = PtyProcess.spawn(argv, cwd=cwd, env=env, dimensions=(30, 200))
            return
        import pty
        master, slave = pty.openpty()
        self._master = master
        try:
            self.proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=env,
                                         close_fds=True, start_new_session=True, **spawn_kwargs())
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)

    def read(self) -> str:
        if self._win is not None:
            return self._win.read(4096)
        try:
            return os.read(self._master, 4096).decode("utf-8", "replace")
        except OSError:
            return ""

    def write(self, text: str) -> None:
        if self._win is not None:
            self._win.write(text)
        else:
            os.write(self._master, text.encode("utf-8"))

    def exit_code(self) -> int | None:
        if self._win is not None:
            return None if self._win.isalive() else (self._win.exitstatus or 0)
        return self.proc.poll()

    def kill(self) -> None:
        try:
            if self._win is not None:
                self._win.terminate(force=True)
            elif self.proc.poll() is None:
                self.proc.kill()
                self.proc.wait(timeout=5)
        except Exception:
            pass
        if self._win is None:
            try:
                os.close(self._master)
            except OSError:
                pass


@dataclass
class _Login:
    name: str
    pty: _Pty
    started: float = field(default_factory=time.monotonic)
    output: str = ""
    url: str | None = None


_login: _Login | None = None
_lock = threading.Lock()


def _pump(login: _Login) -> None:
    while True:
        chunk = login.pty.read()
        if not chunk:
            if login.pty.exit_code() is not None:
                return
            time.sleep(0.05)
            continue
        with _lock:
            login.output = (login.output + chunk)[-OUTPUT_LIMIT:]
            if login.url is None:
                found = _URL.findall(_strip(login.output))
                if found:
                    login.url = found[0].rstrip(".,)")


def stop_login() -> None:
    global _login
    with _lock:
        login, _login = _login, None
    if login is not None:
        login.pty.kill()


def start_login(name: str) -> dict:
    global _login
    _server(name)
    path = _require_claude()
    stop_login()
    claude_config_dir().mkdir(parents=True, exist_ok=True)
    try:
        child = _Pty([path, "mcp", "login", name, "--no-browser"], str(hokma_data_dir()), _env())
    except (OSError, ImportError) as exc:
        raise ServiceError(500, t("status.mcp.login_failed", output=str(exc))) from exc
    login = _Login(name, child)
    with _lock:
        _login = login
    threading.Thread(target=_pump, args=(login,), daemon=True, name="hokma-mcp-login").start()
    deadline = time.monotonic() + LINK_WAIT
    while login.url is None and child.exit_code() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    if login.url is None:
        with _lock:
            shown = _clean_output(_strip(login.output))
        stop_login()
        raise ServiceError(500, t("status.mcp.login_failed", output=shown or "-"))
    return {"auth_url": login.url}


def submit_login(name: str, pasted: str) -> dict:
    pasted = (pasted or "").strip()
    if not PASTE.match(pasted):
        raise ServiceError(400, t("status.mcp.paste_invalid"))
    with _lock:
        login = _login
    if (login is None or login.name != name or login.pty.exit_code() is not None
            or time.monotonic() - login.started > LOGIN_LIFETIME):
        raise ServiceError(409, t("status.mcp.no_login"))
    try:
        login.pty.write(pasted + "\r")
    except OSError as exc:
        raise ServiceError(409, t("status.mcp.no_login")) from exc
    deadline = time.monotonic() + LOGIN_WAIT
    code = login.pty.exit_code()
    while code is None and time.monotonic() < deadline:
        time.sleep(0.1)
        code = login.pty.exit_code()
    with _lock:
        shown = _clean_output(_strip(login.output))
    stop_login()
    if code != 0:
        raise ServiceError(400, t("status.mcp.login_failed", output=shown or "-"))
    return server_state(name)


# ---------------------------------------------------------------- revoke

def revoke(name: str) -> dict:
    _server(name)
    path = _require_claude()
    stop_login()
    code, output = _run([path, "mcp", "logout", name])
    if code != 0:
        raise ServiceError(400, t("status.mcp.logout_failed", output=_clean_output(output) or "-"))
    return server_state(name)

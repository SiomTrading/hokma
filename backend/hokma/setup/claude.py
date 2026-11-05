"""Claude Code detection and sign-in, always against Hokma's own config folder (never the owner's personal one).

State comes from `claude auth status`. The plan is reported only when the CLI says it (`subscriptionType`).
Sign-in runs `claude auth login` as one managed child: the CLI opens the browser by itself; when it cannot, it
prints a link and waits for a code, so the panel can show the link and pass the code to the child's input.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field

from hokma.core.config import claude_config_dir, hokma_data_dir
from hokma.setup.messages import SetupError, t

STATUS_TIMEOUT = 15
LOGIN_LIFETIME = 600
OUTPUT_LIMIT = 8000
_URL = re.compile(r"https://[^\s\"'<>]+")
_ALLOWED_HOSTS = ("claude.com", "claude.ai", "anthropic.com")
_CODE = re.compile(r"^[A-Za-z0-9._~#%+/=\-]{1,600}$")
_PLANS = {"pro": "Pro", "max": "Max", "team": "Team", "enterprise": "Enterprise", "free": "Free"}


@dataclass
class ClaudeState:
    state: str                      # connected | no_session | no_cli
    installed: bool
    plan: str | None = None
    version: str | None = None

    def to_dict(self) -> dict:
        return {"state": self.state, "installed": self.installed, "plan": self.plan, "version": self.version,
                "login": login_info()}


def claude_path() -> str | None:
    return shutil.which("claude")


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(claude_config_dir())
    return env


def _plan_name(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    key = value.strip().lower()
    return _PLANS.get(key, value.strip()[:24])


def _version(path: str) -> str | None:
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=STATUS_TIMEOUT,
                             stdin=subprocess.DEVNULL, env=_env()).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out.split()[0] if out else None


def detect() -> ClaudeState:
    path = claude_path()
    if path is None:
        return ClaudeState("no_cli", False)
    claude_config_dir().mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run([path, "auth", "status"], capture_output=True, text=True, timeout=STATUS_TIMEOUT,
                              stdin=subprocess.DEVNULL, env=_env(), cwd=str(hokma_data_dir()))
        data = json.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return ClaudeState("no_session", True, version=_version(path))
    if not isinstance(data, dict) or data.get("loggedIn") is not True:
        return ClaudeState("no_session", True, version=_version(path))
    return ClaudeState("connected", True, plan=_plan_name(data.get("subscriptionType")), version=_version(path))


# ---------------------------------------------------------------- sign-in

@dataclass
class _Login:
    proc: subprocess.Popen
    started: float = field(default_factory=time.monotonic)
    output: str = ""
    url: str | None = None


_login: _Login | None = None
_lock = threading.Lock()


def _safe_url(text: str) -> str | None:
    for found in _URL.findall(text):
        host = found.split("/")[2].lower() if found.count("/") >= 2 else ""
        if any(host == h or host.endswith("." + h) for h in _ALLOWED_HOSTS):
            return found.rstrip(".,)")
    return None


def _read(login: _Login) -> None:
    assert login.proc.stdout is not None
    for line in login.proc.stdout:
        with _lock:
            login.output = (login.output + line)[-OUTPUT_LIMIT:]
            if login.url is None:
                login.url = _safe_url(login.output)


def _alive(login: _Login | None) -> bool:
    return login is not None and login.proc.poll() is None and time.monotonic() - login.started < LOGIN_LIFETIME


def stop_login() -> None:
    global _login
    with _lock:
        login, _login = _login, None
    if login is not None and login.proc.poll() is None:
        login.proc.kill()
        try:
            login.proc.wait(timeout=5)
        except subprocess.SubprocessError:
            pass


def start_login(wait_for_link: float = 4.0) -> dict:
    global _login
    path = claude_path()
    if path is None:
        raise SetupError(409, t("setup.err.claude_missing"))
    with _lock:
        running = _login if _alive(_login) else None
    if running is None:
        stop_login()
        claude_config_dir().mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.Popen([path, "auth", "login", "--claudeai"], env=_env(), cwd=str(hokma_data_dir()),
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace", bufsize=1)
        except OSError as exc:
            raise SetupError(500, t("setup.err.login_failed", error=exc.strerror or str(exc))) from exc
        running = _Login(proc)
        with _lock:
            _login = running
        threading.Thread(target=_read, args=(running,), daemon=True).start()
    deadline = time.monotonic() + wait_for_link
    while running.url is None and running.proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    return login_info()


def login_info() -> dict:
    with _lock:
        login = _login
    if login is None:
        return {"running": False, "url": None}
    return {"running": _alive(login), "url": login.url}


def submit_code(code: str) -> dict:
    code = (code or "").strip()
    if not _CODE.match(code):
        raise SetupError(400, t("setup.err.code_invalid"))
    with _lock:
        login = _login
    if not _alive(login):
        raise SetupError(409, t("setup.err.no_login"))
    assert login is not None and login.proc.stdin is not None
    try:
        login.proc.stdin.write(code + "\n")
        login.proc.stdin.flush()
    except (OSError, ValueError) as exc:
        raise SetupError(409, t("setup.err.no_login")) from exc
    return {"sent": True}

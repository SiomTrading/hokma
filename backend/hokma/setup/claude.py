"""Claude Code detection and sign-in, always against Hokma's own config folder (never the owner's personal one).

State comes from `claude auth status`. The plan is reported only when the CLI says it (`subscriptionType`).
Sign-in runs `claude auth login` as one managed child: the CLI opens the browser by itself; when it cannot, it
prints a link and waits for a code, so the panel can show the link and pass the code to the child's input. The
whole pasted text (`code#state`) goes to that same child: the code is bound to its PKCE verifier and state.

The sign-in child gets the environment of 0.0.21 on purpose (task 3171): Hokma's process environment plus
CLAUDE_CONFIG_DIR, with Hokma's own tool folders (`<data>/bin`, `<data>/tools/...`, `<data>/python`) taken off the PATH
and none of the variables Hokma sets for the engine. Each attempt leaves in `<data>/logs/engine.log` the command, the
environment (names, and values only for what is not secret), the exit code, the full output with secrets hidden, and
the errors of Claude Code's own debug log (`--debug-file`), so a refused sign-in can be explained after the class.
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

from hokma.core.proc import spawn_kwargs
from hokma.core.config import claude_config_dir, hokma_data_dir
from hokma.i18n import language
from hokma.setup.messages import Msg, SetupError, t

STATUS_TIMEOUT = 15
LOGIN_LIFETIME = 600
OUTPUT_LIMIT = 8000
_URL = re.compile(r"https://[^\s\"'<>]+")
_ALLOWED_HOSTS = ("claude.com", "claude.ai", "anthropic.com")
_CODE = re.compile(r"^[A-Za-z0-9._~#%+/=\-]{1,600}$")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_SECRET_URL = re.compile(r"(https?://[^\s\"'<>?#]+)[?#][^\s\"'<>]*")
_SECRET_TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_\-]{8,}|[A-Za-z0-9_\-~.+/=]{32,})\b")
_SHOWN_CHARS = 600
_PLANS = {"pro": "Pro", "max": "Max", "team": "Team", "enterprise": "Enterprise", "free": "Free"}


@dataclass
class ClaudeState:
    state: str                      # connected | no_session | no_cli
    installed: bool
    plan: str | None = None
    version: str | None = None

    def to_dict(self, lang: str | None = None) -> dict:
        return {"state": self.state, "installed": self.installed, "plan": self.plan, "version": self.version,
                "login": login_info(lang)}


def claude_path() -> str | None:
    return shutil.which("claude")


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(claude_config_dir())
    return env


# Variables Hokma sets for its engine (kit/launch.py, engines/modes.py): never part of a sign-in.
_ENGINE_ONLY = ("ENABLE_CLAUDEAI_MCP_SERVERS", "HOKMA_CONVERSATION_ID", "HOKMA_TASK_ID", "HOKMA_AGENT", "CLAUDECODE",
                "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")


def _hokma_tool_dirs() -> set[str]:
    try:
        from hokma.core import tools

        dirs = [*tools.tool_dirs(), tools.tools_dir()]
    except Exception:          # noqa: BLE001  (a broken tools module must not stop the sign-in)
        dirs = [hokma_data_dir() / "bin", hokma_data_dir() / "tools"]
    return {os.path.normcase(os.path.normpath(str(d))) for d in dirs}


def _under(path: str, roots: set[str]) -> bool:
    p = os.path.normcase(os.path.normpath(path))
    return any(p == r or p.startswith(r.rstrip("\\/") + os.sep) for r in roots)


def login_env() -> dict[str, str]:
    """The environment of `claude auth login`: the one of 0.0.21 (Hokma's process environment and CLAUDE_CONFIG_DIR),
    without Hokma's tool folders on the PATH (Claude Code keeps running with its own Node or binary) and without the
    engine-only variables."""
    env = {k: v for k, v in _env().items() if k.upper() not in _ENGINE_ONLY}
    ours = _hokma_tool_dirs() | {os.path.normcase(os.path.normpath(str(hokma_data_dir() / "python")))}
    for key in [k for k in env if k.upper() == "PATH"]:
        env[key] = os.pathsep.join(p for p in env[key].split(os.pathsep) if p and not _under(p, ours))
    return env


_SHOWN_VALUES = ("PATH", "CLAUDE_CONFIG_DIR", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "COMSPEC", "SHELL",
                 "LANG", "TEMP", "TMP", "TMPDIR", "SYSTEMROOT", "PROCESSOR_ARCHITECTURE", "ANTHROPIC_BASE_URL",
                 "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_GIT_BASH_PATH", "NO_PROXY",
                 "NODE_OPTIONS", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE")
_PROXY = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY")
_USERINFO = re.compile(r"//[^/@\s]+@")


def _env_for_log(env: dict[str, str]) -> str:
    """Every variable name; the value only for the ones that are not secret (proxies without their user:password)."""
    shown, hidden = [], []
    for key in sorted(env, key=str.upper):
        if key.upper() in _SHOWN_VALUES or key.upper() in _PROXY:
            # even a shown value loses any user:password, link parameters and token-like strings
            value = _SECRET_URL.sub(r"\1", _USERINFO.sub("//***@", env[key]))
            shown.append(f"  {key}=" + (value if key.upper() == "PATH" else _SECRET_TOKEN.sub("[hidden]", value)))
        else:
            hidden.append(key)
    return "\n".join(shown + ["  (value not shown) " + " ".join(hidden)] if hidden else shown)


def _plan_name(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    key = value.strip().lower()
    return _PLANS.get(key, value.strip()[:24])


def _version(path: str) -> str | None:
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=STATUS_TIMEOUT,
                             stdin=subprocess.DEVNULL, env=_env(), **spawn_kwargs()).stdout.strip()
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
                              stdin=subprocess.DEVNULL, env=_env(), cwd=str(hokma_data_dir()), **spawn_kwargs())
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
    reader: threading.Thread | None = None
    full: str = ""                      # everything it printed (bounded), for engine.log
    debug_file: str | None = None


_login: _Login | None = None
_lock = threading.Lock()


def _safe_url(text: str) -> str | None:
    for found in _URL.findall(text):
        host = found.split("/")[2].lower() if found.count("/") >= 2 else ""
        if any(host == h or host.endswith("." + h) for h in _ALLOWED_HOSTS):
            return found.rstrip(".,)")
    return None


FULL_LIMIT = 64000
DEBUG_LINES = 60


def _log(message: str) -> None:
    """One block in `<data>/logs/engine.log`. Never raises."""
    try:
        from hokma.core import config
        from hokma.core.supervisor import EngineLog

        EngineLog(config.engine_log_path()).write("claude login: " + message)
    except Exception:          # noqa: BLE001
        pass


def _scrub(text: str) -> str:
    """Like `_clean_output` but keeping the lines: no terminal codes, no link parameters, no token-like strings."""
    text = _ANSI.sub("", text)
    text = _SECRET_URL.sub(r"\1", text)
    return _SECRET_TOKEN.sub("[hidden]", text)


def _debug_errors(path: str | None) -> str:
    """The [ERROR] and [WARN] lines of Claude Code's debug log for this attempt (the request that failed), scrubbed."""
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = [ln.rstrip() for ln in f if "[ERROR]" in ln or "[WARN]" in ln]
    except OSError:
        return ""
    finally:
        try:
            os.remove(path)     # what matters went to engine.log; the full debug log is not kept on disk
        except OSError:
            pass
    return _scrub("\n".join(lines[-DEBUG_LINES:]))


def _read(login: _Login) -> None:
    assert login.proc.stdout is not None
    for line in login.proc.stdout:
        with _lock:
            login.output = (login.output + line)[-OUTPUT_LIMIT:]
            login.full = (login.full + line)[-FULL_LIMIT:]
            if login.url is None:
                login.url = _safe_url(login.output)
    try:
        code = login.proc.wait(timeout=10)
    except subprocess.SubprocessError:
        code = None
    with _lock:
        full = login.full
    debug = _debug_errors(login.debug_file)
    _log(f"pid {login.proc.pid} ended with code {code} after {time.monotonic() - login.started:.0f}s\n"
         f"output:\n{_scrub(full).strip() or '-'}" + (f"\nClaude Code debug errors:\n{debug}" if debug else ""))


def _clean_output(text: str) -> str:
    """The tail of what `claude auth login` printed, safe to show: no terminal codes, no link parameters
    (they carry the OAuth state), no long token-like strings."""
    text = _ANSI.sub("", text)
    text = _SECRET_URL.sub(r"\1", text)
    text = _SECRET_TOKEN.sub("[hidden]", text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return " | ".join(lines)[-_SHOWN_CHARS:]


def _failure(login: _Login) -> Msg | None:
    """The sign-in child ended without success: its real output as a translated message, else None."""
    code = login.proc.poll()
    if code is None or code == 0:
        return None
    if login.reader is not None:
        login.reader.join(timeout=1.0)
    with _lock:
        shown = _clean_output(login.output)
    return t("setup.err.login_exited", code=code, output=shown or "-")


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


def start_login(wait_for_link: float = 4.0, *, debug: bool = True) -> dict:
    global _login
    path = claude_path()
    if path is None:
        raise SetupError(409, t("setup.err.claude_missing"))
    with _lock:
        running = _login if _alive(_login) else None
    if running is None:
        stop_login()
        claude_config_dir().mkdir(parents=True, exist_ok=True)
        debug_file = _debug_file() if debug else None
        argv = [path, *(["--debug-file", debug_file] if debug_file else []), "auth", "login", "--claudeai"]
        env = login_env()
        _log(f"start: {subprocess.list2cmdline(argv) if os.name == 'nt' else ' '.join(argv)}\n"
             f"Claude Code {_version(path) or '?'}, cwd: {hokma_data_dir()}\nenvironment:\n{_env_for_log(env)}")
        try:
            proc = subprocess.Popen(argv, env=env, cwd=str(hokma_data_dir()),
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace", bufsize=1, **spawn_kwargs())
        except OSError as exc:
            _log(f"could not start: {exc}")
            raise SetupError(500, t("setup.err.login_failed", error=exc.strerror or str(exc))) from exc
        _log(f"pid {proc.pid} started")
        running = _Login(proc, debug_file=debug_file)
        with _lock:
            _login = running
        running.reader = threading.Thread(target=_read, args=(running,), daemon=True)
        running.reader.start()
    deadline = time.monotonic() + wait_for_link
    while running.url is None and running.proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    failure = _failure(running)
    if failure is not None and running.debug_file and "unknown option" in running.output.lower():
        _log("this Claude Code does not know --debug-file: starting again without it")
        stop_login()
        return start_login(wait_for_link, debug=False)
    if failure is not None:
        raise SetupError(500, failure)
    return login_info()


def _debug_file() -> str | None:
    """A fresh `<data>/logs/claude-login-debug.txt` (0600) for Claude Code's own debug log, or None if it cannot be made."""
    try:
        logs = hokma_data_dir() / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        target = logs / "claude-login-debug.txt"
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(fd)
        return str(target)
    except OSError:
        return None


def login_info(lang: str | None = None) -> dict:
    with _lock:
        login = _login
    if login is None:
        return {"running": False, "url": None}
    info: dict = {"running": _alive(login), "url": login.url}
    failure = _failure(login)
    if failure is not None:
        info["error"] = failure.text(lang or language())
    return info


def submit_code(code: str) -> dict:
    code = (code or "").strip()
    if not _CODE.match(code):
        raise SetupError(400, t("setup.err.code_invalid"))
    with _lock:
        login = _login
    if not _alive(login):
        raise SetupError(409, t("setup.err.no_login"))
    assert login is not None and login.proc.stdin is not None
    # Never the code itself: its length and whether it carries the state after '#', which Claude Code needs.
    _log(f"code sent to pid {login.proc.pid}: {len(code)} characters, "
         f"{'with' if '#' in code else 'WITHOUT'} the '#state' part")
    try:
        login.proc.stdin.write(code + "\n")
        login.proc.stdin.flush()
    except (OSError, ValueError) as exc:
        _log(f"code could not reach pid {login.proc.pid}: {exc}")
        raise SetupError(409, t("setup.err.no_login")) from exc
    return {"sent": True}

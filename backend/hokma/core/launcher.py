"""`hokma start`, `hokma stop` and `hokma restart`: the commands that run Hokma without a terminal.

Start: when `engine.lock` names a live supervisor, the browser is opened on the plain address
(`http://hokma.localhost:{port}/`, no secrets) after the listener proved it is this engine (`identity.py`) and nothing else is created. Otherwise the supervisor is launched
DETACHED (Windows: the slot's `pythonw.exe` with DETACHED_PROCESS, CREATE_NO_WINDOW and CREATE_NEW_PROCESS_GROUP;
POSIX: a new session, standard input from /dev/null, standard output and error to `<data>/logs/supervisor.out`)
and this command returns as soon as /health answers. The terminal can be closed.

Where the one-time address comes from (decision): the detached supervisor creates the session token and the
one-time code, and it opens the browser itself, with `#t=` and `&o=`, as soon as /health answers (the same
`open_when_ready` the foreground start has always used). Nothing is handed to this parent process: no file, no
argument, no environment variable, no pipe. The parent only learns the port from `engine.lock` (which holds no
secret) and checks /health, so the secrets exist only in the supervisor's memory, the engine's standard input and
the browser's address bar. A pipe handshake would have put the secrets in one more process and needed handle
inheritance rules that differ between Windows and POSIX.

Open Hokma always in the browser is a marker file (`config.browser_pref_path`) that the desktop window and the icon read;
`hokma start` already opens the browser, so it keeps doing it with or without the marker.

Stop: one `stop` word on the control channel (`hokma.core.control`), then wait for the supervisor to end. The
supervisor itself closes everything by force after 10 seconds, so this command waits a little longer than that.
Restart: stop, then start; the new start makes a new session and a new one-time code.
"""
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

from hokma.core import config, control, enginelock, identity
from hokma.core.proc import spawn_kwargs
from hokma.core.supervisor import EXIT_NO_PORT, LOG_LIMIT_BYTES, STOP_GRACE_SECONDS
from hokma.i18n import t

START_WAIT_SECONDS = 60.0
STOP_WAIT_SECONDS = STOP_GRACE_SECONDS + 5.0


def healthy(port: int) -> bool:
    try:
        urllib.request.urlopen(f"http://{config.DEFAULT_HOST}:{port}/health", timeout=1).close()
        return True
    except OSError:
        return False


def running_lock() -> dict | None:
    """The lock of a live supervisor, or None. A lock whose pid is dead is deleted."""
    lock = enginelock.read()
    if lock is None:
        return None
    if enginelock.holder_alive(lock):
        return lock
    enginelock.remove_stale()
    return None


def supervisor_argv(port: int | None) -> list[str]:
    """The foreground supervisor, to be run detached. Windows uses the slot's `pythonw.exe` (no console at all)."""
    args = ["start", "--foreground"] + (["--port", str(port)] if port is not None else [])
    slot = config.current_slot()
    if sys.platform == "win32":
        python = None
        if slot is not None:
            candidate = config.slot_dir(slot) / "venv" / "Scripts" / "pythonw.exe"
            python = candidate if candidate.exists() else None
        if python is None:
            sibling = Path(sys.executable).with_name("pythonw.exe")
            python = sibling if sibling.exists() else Path(sys.executable)
        return [str(python), "-m", "hokma_cli.main", *args]
    if slot is not None:
        return [str(config.slot_exe(slot)), *args]
    return [sys.executable, "-m", "hokma_cli.main", *args]


def _open_output():
    path = config.supervisor_out_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.exists() and path.stat().st_size > LOG_LIMIT_BYTES:
            path.replace(path.with_name(path.name + ".1"))
    except OSError:
        pass
    return open(path, "ab")


def start(port: int | None) -> int:
    lock = running_lock()
    if lock is not None:
        if not identity.listener_is_this_engine(lock["port"]):
            print(f"{t('cli.tag.fail')} " + t("cli.start.address_taken", port=lock["port"]), file=sys.stderr)
            return 1                                              # another program holds the address: no browser
        print(t("cli.start.already_running", port=lock["port"]))
        webbrowser.open(f"http://{config.browser_host()}:{lock['port']}/")   # no secrets: the saved session enters by itself
        return 0
    out = _open_output()
    try:
        proc = subprocess.Popen(supervisor_argv(port), stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                                close_fds=True, **spawn_kwargs(detached=True))
    finally:
        out.close()
    deadline = time.monotonic() + START_WAIT_SECONDS
    while time.monotonic() < deadline:
        code = proc.poll()
        if code is not None:
            lock = running_lock() if code == 0 else None        # 0: another start won the race and is serving
            if lock is not None:
                print(t("cli.start.already_running", port=lock["port"]))
                return 0
            if code == EXIT_NO_PORT:
                print(f"{t('cli.tag.fail')} " + t("supervisor.no_port", first=config.DEFAULT_PORT,
                                                  last=config.LAST_PORT), file=sys.stderr)
                return code
            print(f"{t('cli.tag.fail')} " + t("cli.start.failed", path=config.supervisor_out_path()), file=sys.stderr)
            return code or 1
        lock = enginelock.read()
        if lock is not None and enginelock.pid_alive(lock["pid"]) and healthy(lock["port"]):
            print(t("cli.start.running", port=lock["port"]))
            return 0
        time.sleep(0.2)
    print(f"{t('cli.tag.fail')} " + t("cli.start.failed", path=config.supervisor_out_path()), file=sys.stderr)
    return 1


def stop() -> int:
    lock = running_lock()
    if lock is None:
        print(t("cli.stop.not_running"))
        return 0
    reply = control.request("stop")
    if not reply or not reply.get("ok"):
        print(f"{t('cli.tag.fail')} " + t("cli.stop.unreachable"), file=sys.stderr)
        return 1
    deadline = time.monotonic() + STOP_WAIT_SECONDS
    while enginelock.pid_alive(lock["pid"]) and time.monotonic() < deadline:
        time.sleep(0.1)
    if enginelock.pid_alive(lock["pid"]):
        print(f"{t('cli.tag.fail')} " + t("cli.stop.timeout"), file=sys.stderr)
        return 1
    print(t("cli.stop.done"))
    return 0


def restart(port: int | None) -> int:
    code = stop()
    return code if code else start(port)

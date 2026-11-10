"""`hokma start`: a light supervisor that keeps the Hokma engine running.

- The supervisor creates the session token once and writes it to the engine's standard input. The token is never
  an argument, never in the environment and never in a file, and a relaunched engine gets the same one.
- Exit code 75 means "I updated myself, start me again": the engine is relaunched at once from the slot that
  `current` names, on the same port and with the same token.
- Any other end is a crash: relaunch after 1, 2 and 4 seconds; the fourth crash inside 60 seconds stops the
  supervisor and writes the reason to `<data>/logs/engine.log`.
- The engine dies with the supervisor: on Windows through a job object (`KILL_ON_JOB_CLOSE`), elsewhere because
  the engine watches its standard input, which closes when the supervisor ends.
- The supervisor runs from the slot that was current when it started. An update builds the OTHER slot, so after
  one update the supervisor would be sitting in the slot the next update overwrites. Before every launch the
  supervisor therefore compares its own slot with `current` and, when they differ, hands itself over to the
  `hokma` of the current slot (see `hand_over`), so two updates in a row never touch a running supervisor.

- `hokma start` runs this supervisor DETACHED from the terminal (see `hokma.core.launcher`). Whoever runs it,
  it writes `<data>/engine.lock` (pid, port, start time: no secrets) when it starts and removes it when it ends,
  and it serves the local control channel (`hokma.core.control`) that `hokma stop` uses.
- A clean stop (`stop` on the control channel) is a line sent to the engine's standard input and then its end: the
  engine reads it as "stop", ends its server (turns in progress save what they received, the planner and the
  terminals close, the database is checkpointed) and exits 0. The same pipe is already the engine's lifeline, so
  no new endpoint and no secret are needed. When the engine has not ended after 10 seconds the supervisor closes
  the job (Windows) or the process group (POSIX) and ends.

Standard library only apart from Hokma's own path helpers.
"""
import collections
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TextIO

from hokma.core import config, control, enginelock
from hokma.core.proc import spawn_kwargs
from hokma.core.session import OWNER_MARKER, new_channel_key, new_owner_secret, new_token, parse_owner_note, secret_hash
from hokma.i18n import t

EXIT_RESTART = 75
EXIT_NOT_RETRIED = (3, 4)          # integrity check and database migration: running again cannot fix them
BACKOFF_SECONDS = (1.0, 2.0, 4.0)
MAX_CRASHES = 4
CRASH_WINDOW_SECONDS = 60.0
STOP_GRACE_SECONDS = 10.0
STOP_LINE = b"stop\n"                # what a clean stop writes to the engine's standard input before closing it
EXIT_NO_PORT = 5                     # every port from DEFAULT_PORT to LAST_PORT is taken
LOG_LIMIT_BYTES = 256 * 1024
STDERR_TAIL_LINES = 30


class EngineLog:
    """Append-only text log of what the supervisor did, in the data folder."""

    def __init__(self, path: Path, secrets: tuple[str, ...] = ()) -> None:
        self.path = path
        self._secrets = secrets

    def write(self, message: str) -> None:
        for secret in self._secrets:
            message = message.replace(secret, "***")
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > LOG_LIMIT_BYTES:
                os.replace(self.path, self.path.with_name(self.path.name + ".1"))
            with open(self.path, "a", encoding="utf-8") as f:
                for line in message.splitlines() or [""]:
                    f.write(f"{stamp} {line}\n")
        except OSError:
            pass                                        # a log that cannot be written must not stop Hokma


def engine_argv(port: int) -> list[str]:
    """How to start one engine: the `hokma` of the current slot, or this interpreter in a dev checkout."""
    slot = config.current_slot()
    if slot is not None:
        return [str(config.slot_exe(slot)), "engine", "--port", str(port)]
    return [sys.executable, "-m", "hokma_cli.main", "engine", "--port", str(port)]


def open_when_ready(url: str, health: str) -> None:
    for _ in range(50):
        try:
            urllib.request.urlopen(health, timeout=1)
            webbrowser.open(url)
            return
        except OSError:
            time.sleep(0.2)


def port_in_use(port: int) -> bool:
    """True when something already listens on the port (another Hokma, most likely)."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if sys.platform != "win32":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)     # the same way the server binds
        try:
            probe.bind((config.DEFAULT_HOST, port))
        except OSError:
            return True
    return port_in_use_on_ipv6(port)


def port_in_use_on_ipv6(port: int) -> bool:
    """True when another program already holds [::1]:<port>. The browser may reach `hokma.localhost` through either
    loopback address and the engine only listens on 127.0.0.1, so a port that is taken on [::1] would let that program
    answer in Hokma's name: that port is skipped. A computer with no IPv6 loopback has nothing to hold."""
    import errno
    import socket

    try:
        probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    except OSError:
        return False
    with probe:
        if sys.platform != "win32":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("::1", port))
        except OSError as error:
            return error.errno in (errno.EADDRINUSE, errno.EACCES, getattr(errno, "WSAEADDRINUSE", errno.EADDRINUSE))
    return False


def slot_changed() -> bool:
    mine, current = config.running_slot(), config.current_slot()
    return mine is not None and current is not None and mine != current


def handoff_payload(token: str, owner_hash: str, owner_secret_hash: str = "", channel_key: str = "") -> bytes:
    """Four lines: the session token, the owner confirmation hash (empty when none yet), the hash of the one-time
    secret still waiting for its registration (empty when consumed) and the key that signs the engine's note
    (empty in a hand over: the new supervisor makes its own for each engine)."""
    return "\n".join((token, owner_hash, owner_secret_hash, channel_key)).encode("ascii") + b"\n"


CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def windowless_python(slot: str) -> Path | None:
    """The slot's `pythonw.exe` on Windows (a Python with no console at all), or None (other systems, or missing)."""
    if sys.platform != "win32":
        return None
    candidate = config.slot_dir(slot) / "venv" / "Scripts" / "pythonw.exe"
    return candidate if candidate.is_file() else None


def slot_command(slot: str, args: list[str]) -> list[str]:
    """How to run `hokma <args>` from a slot without any window (task 3151).

    Windows: the slot's `pythonw.exe -m hokma_cli.main`, as `hokma start` already does. Never `hokma.exe`: it is a
    console launcher that runs the venv's `python.exe` as its child, and when the launcher is started detached (no
    console to share) Windows gives that child a NEW console window, empty and visible, that holds the supervisor.
    Other systems, or a slot without `pythonw.exe`: the slot's `hokma`."""
    pythonw = windowless_python(slot)
    if pythonw is not None:
        return [str(pythonw), "-m", "hokma_cli.main", *args]
    return [str(config.slot_exe(slot)), *args]


def detached_popen(argv: list[str], *, popen: Callable[..., subprocess.Popen] | None = None,
                   **kwargs) -> subprocess.Popen:
    """Start `argv` on its own: no window, its own process group and, on Windows, outside any job of this process
    (CREATE_BREAKAWAY_FROM_JOB), so it outlives whoever started it. A job that does not allow leaving refuses that
    flag; the start is then tried inside the job."""
    popen = popen or subprocess.Popen
    if sys.platform == "win32":
        try:
            return popen(argv, **kwargs, **spawn_kwargs(detached=True, extra_flags=CREATE_BREAKAWAY_FROM_JOB))
        except OSError:
            pass
    return popen(argv, **kwargs, **spawn_kwargs(detached=True))


def console_window_shown() -> bool:
    """True on Windows when this process is attached to a console that has a window (it should never have one)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except (AttributeError, OSError):
        return False


def hand_over(port: int, token: str, owner_hash: str = "", owner_secret_hash: str = "", *,
              popen: Callable[..., subprocess.Popen] | None = None) -> bool:
    """Start the `hokma` of the current slot as the new supervisor with the same port and token.

    POSIX: this process BECOMES the new supervisor (exec); the token travels in a pipe whose number is passed,
    not the token. Windows: the new supervisor is started with the token on its standard input and this one
    ends (the terminal prompt may come back; the new supervisor keeps running). It runs with the slot's
    `pythonw.exe`, detached and outside this process's job, so no console window appears and it does not end with
    this one (task 3151). The second line carries the owner confirmation hash (empty when none), the third the hash
    of the one-time owner secret (empty once used). Returns True when the caller must end this supervisor
    (Windows), False when the hand over was not possible."""
    slot = config.current_slot()
    if slot is None:
        return False
    args = ["start", "--port", str(port), "--no-browser"]
    try:
        if sys.platform == "win32":
            out_path = config.supervisor_out_path()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "ab") as out:
                proc = detached_popen(slot_command(slot, args + ["--handoff-stdin"]), popen=popen,
                                      stdin=subprocess.PIPE, stdout=out, stderr=out, close_fds=True)
            assert proc.stdin is not None
            proc.stdin.write(handoff_payload(token, owner_hash, owner_secret_hash))
            proc.stdin.flush()
            proc.stdin.close()
            return True
        exe = str(config.slot_exe(slot))
        read_end, write_end = os.pipe()
        os.write(write_end, handoff_payload(token, owner_hash, owner_secret_hash))
        os.close(write_end)
        os.set_inheritable(read_end, True)
        os.execv(exe, [exe, *args, "--handoff-fd", str(read_end)])
    except OSError:
        return False
    return False


class Supervisor:
    def __init__(self, *, port: int, token: str, argv: Callable[[], list[str]] | None = None,
                 log: EngineLog | None = None, stderr: TextIO | None = None,
                 popen: Callable[..., subprocess.Popen] = subprocess.Popen,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], object] | None = None,
                 handover: Callable[[int, str, str, str], bool] = hand_over,
                 needs_handover: Callable[[], bool] = slot_changed,
                 job: object | None = None, owner_hash: str = "", owner_secret_hash: str = "",
                 kill_group: Callable[[], None] | None = None, before_force: Callable[[], None] | None = None) -> None:
        self.port, self.token = port, token
        self.owner_hash = owner_hash          # SHA-256 of the owner confirmation the tab registered; memory only
        self.owner_secret_hash = owner_secret_hash    # SHA-256 of the one-time secret; dropped when it is consumed
        self._argv = argv or (lambda: engine_argv(port))
        self.log = log or EngineLog(config.engine_log_path(), (token,))
        self.stderr = stderr or sys.stderr
        self._popen, self._clock = popen, clock
        self._stop = threading.Event()
        self._sleep = sleep or (lambda seconds: self._stop.wait(seconds))
        self._handover, self._needs_handover = handover, needs_handover
        self._job = job
        self._kill_group = kill_group or self._close_job_or_group
        self._before_force = before_force            # releases the lock and the control channel before the group dies
        self._child: subprocess.Popen | None = None
        self._graceful = False                       # a clean stop was asked on the control channel
        self._force_timer: threading.Timer | None = None
        self._started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.handed_over = False

    # ---- stopping ---------------------------------------------------------------------------------------

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
        """Ask the engine to end and make the loop return. Safe to call from a signal handler."""
        self._stop.set()
        child = self._child
        if child is not None and child.poll() is None:
            try:
                child.terminate()
            except OSError:
                pass

    def request_shutdown(self) -> None:
        """Clean stop (the `stop` word of the control channel): tell the engine to end by itself and make the loop
        return when it does. After STOP_GRACE_SECONDS the engine, and everything that belongs to Hokma's job or
        process group, is closed by force. Returns at once; safe to call twice."""
        if self._graceful:
            return
        self._graceful = True
        self._stop.set()
        child = self._child
        if child is not None and child.poll() is None:
            self._ask_engine_to_stop(child)
        timer = threading.Timer(STOP_GRACE_SECONDS, self._force_close)
        timer.daemon = True
        self._force_timer = timer
        timer.start()

    @staticmethod
    def _ask_engine_to_stop(proc) -> None:
        """A line and then the end of the engine's standard input. A line means "stop cleanly"; the end alone would
        mean "the supervisor is gone", which gives the engine less time."""
        try:
            proc.stdin.write(STOP_LINE)
            proc.stdin.flush()
            proc.stdin.close()
        except (OSError, ValueError):
            pass

    def _force_close(self) -> None:
        self.log.write(f"stop: the engine did not end in {int(STOP_GRACE_SECONDS)} s; closing everything of Hokma")
        child = self._child
        if child is not None:
            try:
                child.kill()
            except OSError:
                pass
        if self._before_force is not None:
            self._before_force()
        self._kill_group()

    def _close_job_or_group(self) -> None:
        """Windows: closing the job ends every process in it. POSIX: SIGKILL to the process group, which is only
        done when this supervisor leads its own group (detached), never a terminal's group. It ends this process too."""
        if self._job is not None:
            self._job.close()
        elif sys.platform != "win32" and os.getpgrp() == os.getpid():
            os.killpg(os.getpgrp(), signal.SIGKILL)

    def status(self) -> dict:
        child = self._child
        return {"pid": os.getpid(), "port": self.port, "started_at": self._started_at,
                "engine": "up" if child is not None and child.poll() is None else "down"}

    def _install_signals(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"):
            number = getattr(signal, name, None)
            if number is not None:
                signal.signal(number, lambda *_: self.request_stop())

    # ---- one engine -------------------------------------------------------------------------------------

    def _pump_stderr(self, stream, tail: collections.deque, key: str) -> None:
        """Copy the engine's standard error to the terminal. A line with the owner marker never reaches it: the
        first one signed with this engine's key (and only while no hash is held) is taken, every other one is
        dropped, whoever wrote it (a child process of the engine writes to the same stream)."""
        taken = False
        for raw in iter(stream.readline, b""):
            text = raw.decode("utf-8", "replace")
            if OWNER_MARKER in text:
                digest = parse_owner_note(text, key)
                if digest is not None and not taken and not self.owner_hash:
                    taken = True
                    self.owner_hash, self.owner_secret_hash = digest, ""
                continue
            try:
                self.stderr.write(text)
                self.stderr.flush()
            except (OSError, ValueError):
                pass
            tail.append(text.rstrip())

    def _run_engine(self) -> tuple[int, list[str]]:
        proc = self._popen(self._argv(), stdin=subprocess.PIPE, stderr=subprocess.PIPE, **spawn_kwargs())
        self._child = proc
        key = new_channel_key()                          # this engine only; it reaches the engine on standard input
        tail: collections.deque = collections.deque(maxlen=STDERR_TAIL_LINES)
        pump = threading.Thread(target=self._pump_stderr, args=(proc.stderr, tail, key), daemon=True)
        pump.start()
        try:
            if self._job is not None:
                self._job.assign(proc._handle)           # Windows only: the engine dies with the supervisor
            proc.stdin.write(handoff_payload(self.token, self.owner_hash, self.owner_secret_hash, key))
            proc.stdin.flush()                           # stdin stays open: it is the engine's lifeline
        except (OSError, ValueError):
            pass                                         # the engine already ended; its exit code says why
        if self._graceful:
            self._ask_engine_to_stop(proc)
        elif self.stopping:
            self.request_stop()
        try:
            code = proc.wait()
        except KeyboardInterrupt:
            self.request_stop()
            code = self._finish(proc)
        if self.stopping:
            code = self._finish(proc)
        pump.join(2.0)
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass
        self._child = None
        return code, list(tail)

    def _finish(self, proc: subprocess.Popen) -> int:
        try:
            return proc.wait(STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            return proc.wait()

    # ---- the loop ---------------------------------------------------------------------------------------

    def run(self) -> int:
        try:
            return self._loop()
        finally:
            if self._force_timer is not None:
                self._force_timer.cancel()

    def _loop(self) -> int:
        self._install_signals()
        crashes: list[float] = []
        user_ends = (-getattr(signal, "SIGINT", 2), -getattr(signal, "SIGHUP", 1))
        while True:
            if self.stopping and self._graceful:
                return 0                                  # a clean stop arrived while no engine was running
            if not self.stopping and self._needs_handover():
                self.log.write("handing the supervisor over to the current slot")
                if self._handover(self.port, self.token, self.owner_hash, self.owner_secret_hash):
                    self.handed_over = True
                    return 0
                self.log.write("the hand over was not possible; staying in this slot")
            code, tail = self._run_engine()
            if self.stopping or code == 0 or code in user_ends:
                self.log.write(f"engine ended (code {code}); supervisor stops")
                return 0
            if code == EXIT_RESTART:
                self.log.write("engine asked for a restart after an update; starting it again")
                print(t("supervisor.restarting"), file=self.stderr, flush=True)
                continue
            if code in EXIT_NOT_RETRIED:
                self.log.write(f"engine ended with code {code}, which a new attempt cannot fix\n" + "\n".join(tail))
                return code
            now = self._clock()
            crashes = [moment for moment in crashes if now - moment < CRASH_WINDOW_SECONDS] + [now]
            self.log.write(f"engine crashed (code {code}); crash {len(crashes)} in the last "
                           f"{int(CRASH_WINDOW_SECONDS)} s\n" + "\n".join(tail))
            if len(crashes) >= MAX_CRASHES:
                self.log.write(f"stopping: {MAX_CRASHES} crashes in {int(CRASH_WINDOW_SECONDS)} s. "
                               "Start Hokma again with: hokma start")
                print(t("supervisor.gave_up", path=self.log.path), file=self.stderr, flush=True)
                return 1
            wait = BACKOFF_SECONDS[min(len(crashes), len(BACKOFF_SECONDS)) - 1]
            print(t("supervisor.crashed", seconds=int(wait)), file=self.stderr, flush=True)
            self._sleep(wait)


def run_supervisor(*, port: int | None, token: str | None = None, print_url: bool = False, owner_hash: str = "",
                   owner_secret_hash: str = "") -> int:
    """Entry of `hokma start --foreground` (and of the detached supervisor that `hokma start` launches).

    `token` is given only by a hand over; otherwise a new one is created. `port` None means "the first free port
    from 8765 to 8775" (exit code 5 when none is free); a number means exactly that port (exit code 1 when busy).
    Host and Origin are accepted for the real port only, and `engine.lock` names the real port.

    Opening the browser: a plain start prints the address (print_url, for `--no-browser`) or opens it itself once
    /health answers. The address with `#t=` and `&o=` lives only in this process: a detached start does not hand
    it to its parent through a file, an argument or the environment; this process opens the browser (the one place
    the address has always been used) and the parent only waits for /health and for the lock."""
    handover_mode = token is not None
    if not handover_mode:
        if port is None:
            port = next((p for p in range(config.DEFAULT_PORT, config.LAST_PORT + 1) if not port_in_use(p)), None)
            if port is None:
                print(t("supervisor.no_port", first=config.DEFAULT_PORT, last=config.LAST_PORT),
                      file=sys.stderr, flush=True)
                return EXIT_NO_PORT
        elif port_in_use(port):
            print(t("supervisor.port_busy", port=port), file=sys.stderr, flush=True)
            return 1
        if not enginelock.claim(port):
            held = enginelock.read()
            print(t("supervisor.already_running", port=held["port"] if held else port), file=sys.stderr, flush=True)
            return 0
    else:
        assert port is not None
        enginelock.take_over(port)                      # the supervisor that handed over is ending; this one owns it now
    token = token or new_token()
    job = None
    server: control.ControlServer | None = None
    sup: Supervisor | None = None
    try:
        if sys.platform == "win32":
            from hokma.core.winjob import KillOnCloseJob

            job = KillOnCloseJob()                      # held until this function returns
        once = "" if handover_mode else new_owner_secret()     # printed once, in the address; only its hash is kept

        def release() -> None:
            if server is not None:
                server.close()
            enginelock.release()

        sup = Supervisor(port=port, token=token, job=job, owner_hash=owner_hash, before_force=release,
                         owner_secret_hash=owner_secret_hash or (secret_hash(once) if once else ""))
        server = control.ControlServer(sup.status, sup.request_shutdown)
        try:
            server.start()
        except OSError as exc:
            sup.log.write(f"the control channel could not be opened: {exc}")
            print(f"{t('cli.tag.fail')} {t('supervisor.no_control', path=sup.log.path)}", file=sys.stderr, flush=True)
            return 1
        if not handover_mode:
            base = f"http://{config.DEFAULT_HOST}:{port}/"
            url = f"{base}#t={token}&o={once}"        # the link with secrets only goes to the numeric loopback
            if print_url:
                print(url, flush=True)
            else:
                threading.Thread(target=open_when_ready, args=(url, base + "health"), daemon=True).start()
        return sup.run()
    finally:
        if server is not None:
            server.close()
        enginelock.release()
        if job is not None:
            job.close()

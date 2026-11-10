"""The detached updater: how Hokma replaces itself without holding its own files (task 3146).

Why. On Windows a file that a running process loaded (a `.pyd` of the server, the venv's Python) cannot be deleted.
An update that runs inside the server, or in any process started from a runtime slot, cannot rebuild a slot that some
Hokma process still uses: `shutil.rmtree` fails with WinError 5. So the update runs in a process that lives OUTSIDE
both slots:

- `launch()` runs in the engine (the Update button) or in `hokma update` on Windows. It copies the `hokma` package and
  a small entry script to a new temporary folder outside `runtime/a` and `runtime/b`, and starts it with the shared
  Python under `<data>/python` (never a file inside a slot), detached: on Windows DETACHED_PROCESS,
  CREATE_NEW_PROCESS_GROUP and CREATE_NO_WINDOW, plus CREATE_BREAKAWAY_FROM_JOB because it must outlive the job that
  ends with the supervisor; on POSIX a new session. The session secrets (token and owner hashes) travel on its
  standard input only, as in the supervisor hand over, so the restarted Hokma serves the same session and the open
  tab reconnects by itself.
- The engine then shuts down the way "Shut down Hokma" does, closing the sockets with the reason `updating`.
- `Updater.run()` runs in the detached process, in this order: stop Hokma (wait for the engine and the supervisor to
  end, asking the supervisor to stop if it is still there), kill whatever still runs from runtime/a, runtime/b or
  python (as Instalar-Hokma.cmd does), remove a stale `update.lock`, run `run_update` (same code and retries as
  before), then start Hokma again: the new version, or the previous one when the update failed.
- Every step is written to `<data>/update-status.json`. The outcome is written BEFORE Hokma starts again, so the
  restarted engine can already tell the panel how it went. People read catalog messages; a traceback goes only to
  `<data>/logs/updater.log`.

Standard library only: the copy runs without the slot's venv.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request
from pathlib import Path
from typing import Callable, Iterable, NamedTuple

from hokma import __version__
from hokma.core import config
from hokma.core.proc import spawn_kwargs
from hokma.i18n import t
from hokma.update.source import UpdateError, parse_version, resolve_source

CREATE_BREAKAWAY_FROM_JOB = 0x01000000
ENTRY_NAME = "hokma_updater.py"
FOLDER_PREFIX = "hokma-updater-"
OLD_FOLDER_SECONDS = 24 * 60 * 60
WAIT_EXIT_SECONDS = 30.0             # the engine and the supervisor end by themselves within this time
ASK_STOP_AFTER_SECONDS = 5.0         # a supervisor still alive after this is asked to stop on its control channel
KILL_ROUNDS = 2
KILL_PAUSE_SECONDS = 0.8
HEALTH_WAIT_SECONDS = 90.0
START_WAIT_SECONDS = 70.0
FAILED_SHOWN_SECONDS = 24 * 60 * 60  # a failed outcome is shown to the panel for one day
POWERSHELL_LIST = ("Get-CimInstance Win32_Process | Select-Object ProcessId,ExecutablePath,CommandLine | "
                   "ConvertTo-Json -Compress")

ENTRY_SCRIPT = '''"""Entry of the detached Hokma updater: a temporary copy, outside the runtime slots."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hokma.update.detached import main  # noqa: E402

sys.exit(main(sys.argv[1:]))
'''


# ---- where things are -------------------------------------------------------------------------------------------

def status_path() -> Path:
    return config.hokma_data_dir() / "update-status.json"


def updater_log_path() -> Path:
    return config.hokma_data_dir() / "logs" / "updater.log"


def leftover_roots() -> list[Path]:
    """Folders whose processes must be gone before a slot is rebuilt: both slots and the shared Python."""
    data = config.hokma_data_dir()
    return [config.slot_dir("a"), config.slot_dir("b"), data / "python"]


# ---- the status file --------------------------------------------------------------------------------------------

def read_status() -> dict | None:
    try:
        body = json.loads(status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def clear_status() -> None:
    try:
        status_path().unlink()
    except OSError:
        pass


def failed_outcome(now: float | None = None) -> str | None:
    """The message of an update that failed during the last day, for the panel; None otherwise."""
    body = read_status()
    if not body or body.get("state") != "failed" or not isinstance(body.get("message"), str):
        return None
    try:
        age = (now if now is not None else time.time()) - float(body.get("updated_at", 0))
    except (TypeError, ValueError):
        return None
    return body["message"] if 0 <= age <= FAILED_SHOWN_SECONDS else None


class StatusFile:
    """Progress and outcome of the detached update, replaced atomically. A write that fails is not an error."""

    def __init__(self, path: Path | None = None, clock: Callable[[], float] = time.time) -> None:
        self.path = path or status_path()
        self.clock = clock
        self.started_at = clock()
        self.body: dict = {"from_version": __version__, "to_version": None}

    def write(self, state: str, **fields) -> None:
        self.body.update(fields)
        self.body.update(state=state, started_at=self.started_at, updated_at=self.clock(), pid=os.getpid())
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self.body, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def stage(self, stage: str) -> None:
        """The `progress` callback of `run_update`."""
        self.write("updating", stage=stage)


# ---- before launching: is there anything to install? ------------------------------------------------------------

def check_newer(source: str | None = None, force: bool = False) -> str | None:
    """The version an update would install, or None when Hokma already runs it. Same rules as `run_update`, so an
    up-to-date Hokma is never shut down for nothing."""
    if config.current_slot() is None:
        raise UpdateError(t("update.not_installed"))
    manifest = resolve_source(source).fetch()
    new, current = parse_version(manifest.version), parse_version(__version__)
    if new == current and not force:
        return None
    if new < current and not force:
        raise UpdateError(t("update.older", new=manifest.version, current=__version__))
    return manifest.version


# ---- launching the detached process -----------------------------------------------------------------------------

def _inside(path: Path, roots: Iterable[Path]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return True
    for root in roots:
        try:
            root = root.resolve()
        except OSError:
            continue
        if resolved == root or root in resolved.parents:
            return True
    return False


def updater_python() -> Path:
    """The shared Python the slots' venvs were built from (under `<data>/python` once installed), never a file inside
    runtime/a or runtime/b. Windows prefers `pythonw.exe`, which has no console."""
    base = Path(getattr(sys, "_base_executable", "") or sys.executable)
    candidates = ([base.with_name("pythonw.exe")] if sys.platform == "win32" else []) + [base]
    slots = [config.slot_dir("a"), config.slot_dir("b")]
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_file() and not _inside(resolved, slots):
            return resolved
    raise UpdateError(t("update.detached.no_python"))


def _sweep_old_folders(parent: Path, now: float) -> None:
    try:
        old = [p for p in parent.glob(FOLDER_PREFIX + "*") if now - p.stat().st_mtime > OLD_FOLDER_SECONDS]
    except OSError:
        return
    for folder in old:
        shutil.rmtree(folder, ignore_errors=True)


def stage_updater(parent: Path | None = None) -> Path:
    """Copy the `hokma` package and the entry script to a new temporary folder outside the runtime slots."""
    parent = Path(parent or tempfile.gettempdir())
    parent.mkdir(parents=True, exist_ok=True)
    _sweep_old_folders(parent, time.time())
    folder = Path(tempfile.mkdtemp(prefix=FOLDER_PREFIX, dir=parent))
    if _inside(folder, [config.runtime_root()]):
        shutil.rmtree(folder, ignore_errors=True)
        raise UpdateError(t("update.detached.launch_failed"))
    package = Path(__file__).resolve().parents[1]
    shutil.copytree(package, folder / "hokma", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (folder / ENTRY_NAME).write_text(ENTRY_SCRIPT, encoding="utf-8")
    return folder


def child_env() -> dict[str, str]:
    """The environment of the updater and of the Hokma it starts again: no Python redirection, explicit folders."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONSAFEPATH", "VIRTUAL_ENV")}
    env.update(HOKMA_DATA_DIR=str(config.hokma_data_dir()), HOKMA_HOME=str(config.hokma_home()))
    return env


def handoff_lines(handoff: tuple[str, str, str] | None) -> bytes:
    """Token, owner confirmation hash and one-time secret hash, one per line (empty lines when there is no session)."""
    return ("\n".join(handoff or ("", "", "")) + "\n").encode("ascii")


def _open_append(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    return open(path, "ab")


def launch(*, handoff: tuple[str, str, str] | None, port: int | None, wait_pids: Iterable[int], restart: bool,
           source: str | None = None, force: bool = False, to_version: str | None = None,
           popen: Callable[..., subprocess.Popen] = subprocess.Popen, temp_parent: Path | None = None) -> int:
    """Start the detached updater and hand it the session secrets on its standard input. Returns its pid.
    Raises UpdateError with a message for people when it cannot start."""
    python = updater_python()
    folder = stage_updater(temp_parent)
    argv = [str(python), "-I", "-B", str(folder / ENTRY_NAME), "--folder", str(folder)]
    if port:
        argv += ["--port", str(port)]
    for pid in wait_pids:
        argv += ["--wait-pid", str(int(pid))]
    if restart:
        argv.append("--restart")
    if source:
        argv += ["--from", source]
    if force:
        argv.append("--force")
    StatusFile().write("waiting", to_version=to_version, stage=None, message=None)
    common = dict(stdin=subprocess.PIPE, cwd=str(config.hokma_data_dir()), env=child_env(), close_fds=True)
    log = _open_append(updater_log_path())
    try:
        try:
            if sys.platform == "win32":
                try:
                    proc = popen(argv, stdout=log, stderr=log, **common,
                                 **spawn_kwargs(detached=True, extra_flags=CREATE_BREAKAWAY_FROM_JOB))
                except OSError:                       # a job that does not allow leaving: try inside it
                    proc = popen(argv, stdout=log, stderr=log, **common, **spawn_kwargs(detached=True))
            else:
                proc = popen(argv, stdout=log, stderr=log, **common, **spawn_kwargs(detached=True))
        except OSError as exc:
            shutil.rmtree(folder, ignore_errors=True)
            clear_status()
            raise UpdateError(t("update.detached.launch_failed")) from exc
    finally:
        log.close()
    try:
        proc.stdin.write(handoff_lines(handoff))
        proc.stdin.flush()
        proc.stdin.close()
    except (OSError, ValueError, AttributeError):
        pass
    return proc.pid


# ---- what the detached process does -----------------------------------------------------------------------------

class Proc(NamedTuple):
    pid: int
    exe: str
    cmdline: str


def _list_windows() -> list[Proc]:
    system = os.environ.get("SystemRoot", r"C:\Windows")
    shell = Path(system) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    r = subprocess.run([str(shell), "-NoProfile", "-NonInteractive", "-Command", POWERSHELL_LIST],
                       capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL, **spawn_kwargs())
    rows = json.loads(r.stdout or "[]")
    rows = rows if isinstance(rows, list) else [rows]
    return [Proc(int(row.get("ProcessId") or 0), row.get("ExecutablePath") or "", row.get("CommandLine") or "")
            for row in rows if isinstance(row, dict)]


def _list_posix() -> list[Proc]:
    r = subprocess.run(["ps", "-A", "-ww", "-o", "pid=", "-o", "args="], capture_output=True, text=True, timeout=30,
                       stdin=subprocess.DEVNULL, **spawn_kwargs())
    out = []
    for line in r.stdout.splitlines():
        head, _, args = line.strip().partition(" ")
        if not head.isdigit():
            continue
        pid = int(head)
        try:
            exe = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            exe = ""
        out.append(Proc(pid, exe, args.strip()))
    return out


def list_processes() -> list[Proc]:
    try:
        return _list_windows() if sys.platform == "win32" else _list_posix()
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def runs_from(proc: Proc, roots: Iterable[Path], windows: bool | None = None) -> bool:
    """True when the executable or the command line of `proc` is under one of `roots` (case-insensitive on Windows)."""
    windows = sys.platform == "win32" if windows is None else windows
    sep = "\\" if windows else "/"
    fold = (lambda s: s.lower()) if windows else (lambda s: s)
    exe, cmdline = fold(proc.exe or ""), fold(proc.cmdline or "")
    for root in roots:
        prefix = fold(str(root).rstrip("\\/") + sep)
        if (exe and exe.startswith(prefix)) or prefix in cmdline:
            return True
    return False


def _kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM if sys.platform == "win32" else signal.SIGKILL)   # Windows: TerminateProcess
    except OSError:
        pass


def kill_leftovers(*, roots: Iterable[Path] | None = None, exclude: Iterable[int] = (),
                   list_fn: Callable[[], list[Proc]] = list_processes, kill_fn: Callable[[int], None] = _kill,
                   sleep: Callable[[float], object] = time.sleep, windows: bool | None = None) -> list[int]:
    """Kill every process that runs from the slots or the shared Python, except `exclude` (this updater). Two rounds,
    like the installer, because a killed parent can leave a child that was just starting."""
    roots = list(roots if roots is not None else leftover_roots())
    skip = set(exclude) | {os.getpid()}
    killed: list[int] = []
    for _ in range(KILL_ROUNDS):
        left = [p for p in list_fn() if p.pid > 0 and p.pid not in skip and runs_from(p, roots, windows)]
        for proc in left:
            kill_fn(proc.pid)
            killed.append(proc.pid)
        if not left:
            break
        sleep(KILL_PAUSE_SECONDS)
    return killed


def _pid_alive(pid: int) -> bool:
    from hokma.core.enginelock import pid_alive

    return pid_alive(pid)


def stop_hokma(wait_pids: Iterable[int], *, alive: Callable[[int], bool] = _pid_alive,
               clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], object] = time.sleep) -> bool:
    """Wait for the engine (and whatever else asked to be waited for) and the supervisor to end. A supervisor still
    alive after a few seconds is asked to stop, exactly as `hokma stop` does. True when all ended in time; the next
    step kills what did not."""
    from hokma.core import control, enginelock

    pids = {int(p) for p in wait_pids if int(p) > 0}
    lock = enginelock.read()
    if lock is not None:
        pids.add(lock["pid"])
    start, asked = clock(), lock is None
    while True:
        pids = {p for p in pids if alive(p)}
        if not pids:
            return True
        elapsed = clock() - start
        if not asked and elapsed >= ASK_STOP_AFTER_SECONDS:
            asked = True
            try:
                control.request("stop")
            except (OSError, ValueError):
                pass
        if elapsed >= WAIT_EXIT_SECONDS:
            return False
        sleep(0.2)


def clear_stale_locks() -> None:
    """`update.lock` left by an update that was killed, and `engine.lock` of a supervisor that is gone."""
    try:
        (config.hokma_data_dir() / "update.lock").unlink()
    except OSError:
        pass
    try:
        from hokma.core import enginelock

        enginelock.remove_stale()
    except OSError:
        pass


def _healthy(port: int) -> bool:
    try:
        urllib.request.urlopen(f"http://{config.DEFAULT_HOST}:{port}/health", timeout=1).close()
        return True
    except OSError:
        return False


def start_hokma(port: int | None, handoff: tuple[str, str, str] | None, *,
                popen: Callable[..., subprocess.Popen] = subprocess.Popen,
                healthy: Callable[[int], bool] = _healthy, clock: Callable[[], float] = time.monotonic,
                sleep: Callable[[float], object] = time.sleep) -> bool:
    """Start the Hokma of the slot `current` names. With the session secrets and the port it is a hand over: the new
    supervisor serves the same session on the same port, so the open tab reconnects by itself. Otherwise it is a
    plain `hokma start`, which opens the browser. True once /health answers."""
    slot = config.current_slot()
    if slot is None:
        return False
    exe = str(config.slot_exe(slot))
    env = child_env()
    out = _open_append(config.supervisor_out_path())
    try:
        if handoff and handoff[0] and port:
            proc = popen([exe, "start", "--port", str(port), "--no-browser", "--handoff-stdin"], stdin=subprocess.PIPE,
                         stdout=out, stderr=out, cwd=str(config.hokma_data_dir()), env=env, close_fds=True,
                         **spawn_kwargs(detached=True))
            try:
                proc.stdin.write(handoff_lines(handoff))
                proc.stdin.flush()
            except (OSError, ValueError, AttributeError):
                pass
        else:
            proc = popen([exe, "start"] + (["--port", str(port)] if port else []), stdin=subprocess.DEVNULL,
                         stdout=out, stderr=out, cwd=str(config.hokma_data_dir()), env=env, close_fds=True,
                         **spawn_kwargs())
            try:
                proc.wait(START_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                pass
    finally:
        out.close()
    deadline = clock() + HEALTH_WAIT_SECONDS
    while clock() < deadline:
        target = port
        if target is None:
            from hokma.core import enginelock

            lock = enginelock.read()
            target = lock["port"] if lock else None
        if target is not None and healthy(target):
            return True
        sleep(0.5)
    return False


def _run_update(source: str | None, force: bool, progress: Callable[[str], None]):
    from hokma.update.runner import run_update

    return run_update(resolve_source(source), force=force, progress=progress)


class Steps(NamedTuple):
    stop: Callable[[list[int]], object]
    kill: Callable[[], object]
    clear_locks: Callable[[], object]
    update: Callable[[str | None, bool, Callable[[str], None]], object]
    start: Callable[[int | None, tuple[str, str, str] | None], bool]


def real_steps() -> Steps:
    return Steps(stop=stop_hokma, kill=kill_leftovers, clear_locks=clear_stale_locks, update=_run_update,
                 start=start_hokma)


class Updater:
    """The detached update, step by step. Never raises and never prints a traceback where a person reads."""

    def __init__(self, *, port: int | None, wait_pids: list[int], restart: bool,
                 handoff: tuple[str, str, str] | None, source: str | None = None, force: bool = False,
                 status: StatusFile | None = None, steps: Steps | None = None, log=None) -> None:
        self.port, self.wait_pids, self.restart = port, wait_pids, restart
        self.handoff, self.source, self.force = handoff, source, force
        if status is None:
            status = StatusFile()
            status.body["to_version"] = (read_status() or {}).get("to_version")     # what launch() announced
        self.status = status
        self.steps = steps or real_steps()
        self.log = log if log is not None else sys.stderr

    def _note(self, text: str) -> None:
        try:
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            print(f"{stamp} {text}", file=self.log, flush=True)
        except (OSError, ValueError):
            pass

    def run(self) -> int:
        st = self.status
        failure: str | None = None
        result = None
        try:
            st.write("stopping")
            self._note("stopping Hokma")
            self.steps.stop(list(self.wait_pids))
            self._note("killing what still runs from the runtime")
            self.steps.kill()
            self.steps.clear_locks()
            st.write("updating")
            self._note("updating")
            result = self.steps.update(self.source, self.force, st.stage)
        except UpdateError as exc:
            failure = str(exc)
            self._note(f"update failed: {failure}")
        except BaseException:                                  # noqa: BLE001  the detail goes to the log only
            self._note("update failed with an unexpected error:\n" + traceback.format_exc())
            failure = t("update.detached.unexpected", path=updater_log_path())
        to_version = getattr(result, "to_version", None) or st.body.get("to_version")
        outcome = getattr(result, "status", None)
        if failure is not None:
            st.write("failed", message=failure, stage=None, restarting=self.restart)
        else:
            st.write("done", to_version=to_version, outcome=outcome, stage=None, message=None,
                     restarting=self.restart)
        if not self.restart:
            return 1 if failure else 0
        try:
            self._note("starting Hokma again")
            started = bool(self.steps.start(self.port, self.handoff))
        except BaseException:                                  # noqa: BLE001
            self._note("Hokma could not be started again:\n" + traceback.format_exc())
            started = False
        st.write(st.body["state"], restarting=False, started=started)
        if not started:
            self._note("Hokma did not answer after the restart")
            if failure is None:
                st.write("failed", message=t("update.detached.not_started", version=to_version or __version__))
        return 1 if failure or not started else 0


def read_handoff(stream) -> tuple[str, str, str] | None:
    """Three lines on standard input: token, owner hash, one-time secret hash. None when there is no session."""
    from hokma.core import session

    try:
        lines = [stream.readline().strip() for _ in range(3)]
    except (OSError, ValueError, AttributeError):
        return None
    token, owner, once = lines
    if not token or not session.TOKEN_PATTERN.fullmatch(token):
        return None
    if any(v and not session.OWNER_HASH_PATTERN.fullmatch(v) for v in (owner, once)):
        return None
    return token, owner, once


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="hokma-updater")
    parser.add_argument("--folder")
    parser.add_argument("--port", type=int)
    parser.add_argument("--wait-pid", type=int, action="append", default=[])
    parser.add_argument("--restart", action="store_true")
    parser.add_argument("--from", dest="source")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    handoff = read_handoff(sys.stdin)
    try:
        return Updater(port=args.port, wait_pids=args.wait_pid, restart=args.restart or handoff is not None,
                       handoff=handoff, source=args.source, force=args.force).run()
    finally:
        if args.folder and Path(args.folder).name.startswith(FOLDER_PREFIX):
            shutil.rmtree(args.folder, ignore_errors=True)

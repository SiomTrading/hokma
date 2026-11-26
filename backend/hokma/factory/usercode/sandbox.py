"""The parent side of the isolated process: starts it, limits it, talks to it by the pipe and kills it (SPEC-031 sections 5.5, 6.1 and 6.3).

One Sandbox is one interpreter that runs the code of ONE rule version. It is never started with fork (a fork would hand over the memory of the server,
with its keys), never with a shell, never with an environment but the five variables below, always in an empty folder and with `close_fds`.
On Windows the process is put in a Job Object before the first line of generated code is sent; if that fails, no generated code runs.
The answers are data from a process that runs untrusted code: the caller validates them again.

NOT PROVEN on this author's machine (Linux only): the Job Object path with a real `pythonw.exe` (Windows: no console, no stdout handle but the pipe), the
macOS memory watch with a real `ps`, and Windows paths of `argv`. The tests that touch them play the platform with fakes and say so; the ones marked for
Windows must be run there.

Who creates the process matters on Linux: PR_SET_PDEATHSIG fires when the THREAD that created the child dies, not when the parent process does. So the
child is always created by a thread that lives as long as the child: the reader thread of the Sandbox (it does the Popen, then reads the pipe until the
child's end), never the thread of the caller, which may be a short-lived worker of a pool.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from collections.abc import Mapping
from typing import Any

from hokma.core.proc import spawn_kwargs
from hokma.factory.usercode import static_check

MAX_MESSAGE = 8 * 1024 * 1024
CALL_TIMEOUT = 0.25            # seconds per call (6.1); the delegates read this constant
LOAD_TIMEOUT = 15.0
START_TIMEOUT = 20.0
MEMORY_MB = 768                # resident memory watched by the parent
ADDRESS_SPACE_MB = 1536        # RLIMIT_AS: numpy maps more than it uses
CPU_SECONDS = 90
WORKER_MODULE = "hokma.factory.usercode.worker"
PS_EVERY = 0.25                # seconds between two `ps` calls where there is no /proc (macOS)

# What the parent may run for a version of a rule, by its status (SPEC-031 5.6). Testing a rule (coder_test) needs the code of a draft, a failed or a
# generated version; a rejected or retired version never runs again.
CLOSE_WAIT = 20.0                                      # seconds to wait for the process to end by itself after {"close":true}, before it is killed
LOADABLE_STATUSES = frozenset({"draft", "failed", "generated", "owner_reviewed"})
# SESSION 2/4: the ONE status that may run in a backtest, in a paper trial or live. Whoever puts a rule on the road to operate (the desks, the runner of
# a strategy spec) must call `require_operable(status)` on the row it loaded; nothing else may start a Sandbox for a strategy.
OPERATING_STATUS = "owner_reviewed"
WATCH_EVERY = 0.05

ERROR_CODES = {"static_check", "bad_params", "rule_error", "bad_input", "not_loaded", "already_loaded", "load_error", "contract", "bad_answer",
               "message_too_large", "bad_message"}


class SandboxError(Exception):
    """The isolated process refused, failed or had to be killed. `code` is stable (the failure codes of coder_test)."""

    def __init__(self, code: str, message: str = "", findings: list[dict[str, Any]] | None = None) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
        self.findings = findings or []


def default_workdir() -> Path:
    from hokma.core.config import hokma_data_dir

    return hokma_data_dir() / "sandbox" / "empty"


def require_operable(status: str) -> None:
    """Session 2/4 calls this before a rule operates: only `owner_reviewed` (the owner read this exact hash) may run in a backtest, a trial or live."""
    if status != OPERATING_STATUS:
        raise SandboxError("not_reviewed", f"a rule version in status {status!r} cannot operate: only {OPERATING_STATUS!r}")


class Sandbox:
    def __init__(self, *, workdir: Path | None = None, python: str | None = None, worker_argv: list[str] | None = None,
                 call_timeout: float = CALL_TIMEOUT, load_timeout: float = LOAD_TIMEOUT, start_timeout: float = START_TIMEOUT,
                 memory_mb: int = MEMORY_MB, cpu_seconds: int = CPU_SECONDS) -> None:
        self.workdir = Path(workdir) if workdir is not None else default_workdir()
        self._python = python or sys.executable
        self._worker_argv = worker_argv
        self.call_timeout, self.load_timeout, self.start_timeout = call_timeout, load_timeout, start_timeout
        self.memory_mb, self.cpu_seconds = memory_mb, cpu_seconds
        self.layers: dict[str, Any] = {}
        self._proc: subprocess.Popen[bytes] | None = None
        self._lines: queue.Queue[tuple[str, bytes | None]] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._job: Any = None
        self._dead = False
        self._killed_for: str | None = None
        self.exit_code: int | None = None
        self.in_job = False
        self._ps_at, self._ps_last = -1e9, None
        self._platform = sys.platform           # read once, so that a test can play another platform without touching sys

    # ---------------------------------------------------------------- state
    @property
    def alive(self) -> bool:
        return self._proc is not None and not self._dead and self._proc.poll() is None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def limits(self) -> dict[str, int]:
        return {"as": ADDRESS_SPACE_MB * 1024 * 1024, "cpu": self.cpu_seconds, "fsize": 0, "core": 0}

    def __enter__(self) -> "Sandbox":
        if self._proc is None:
            self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- start
    def argv(self) -> list[str]:
        if self._worker_argv is not None:
            return list(self._worker_argv)
        python = self._python
        if sys.platform == "win32" and python.lower().endswith("python.exe"):
            candidate = python[:-10] + "pythonw.exe"
            python = candidate if os.path.exists(candidate) else python
        # `-s` no user site, `-P` no current folder on sys.path. Not `-I`: it implies `-E`, which would ignore PYTHONHASHSEED. The environment is the five
        # variables of `environment()` and nothing else, so there is no PYTHONPATH or PYTHONHOME for `-E` to ignore.
        return [python, "-s", "-P", "-X", "utf8", "-m", WORKER_MODULE, "--as-mb", str(ADDRESS_SPACE_MB), "--cpu", str(self.cpu_seconds)]

    @staticmethod
    def environment() -> dict[str, str]:
        """Five variables and the thread limits of the math libraries (a pool of threads would block the network namespace). Never a copy of os.environ."""
        env = {"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0",
               "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}
        if sys.platform == "win32" and os.environ.get("SYSTEMROOT"):
            env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        return env

    def start(self) -> dict[str, Any]:
        if self._proc is not None or self._reader is not None:
            raise SandboxError("unavailable", "this process was already started")
        try:
            self.workdir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SandboxError("unavailable", f"the empty working folder cannot be made: {exc}") from None
        job = self._make_job()
        # The child is created by the reader thread, which lives until the child's pipe ends: the death signal of Linux follows the creating thread.
        spawned: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._reader = threading.Thread(target=self._spawn_and_read, args=(spawned,), name="usercode-reader", daemon=True)
        self._reader.start()
        try:
            kind, value = spawned.get(timeout=self.start_timeout)
        except queue.Empty:
            kind, value = "error", OSError("the isolated process did not start in time")
        if kind != "proc":
            if job is not None:
                job.close()
            self._dead = True
            raise SandboxError("unavailable", f"the isolated process cannot start: {value}") from None
        self._proc = value
        if job is not None:
            try:
                job.assign(int(self._proc._handle))           # type: ignore[attr-defined]  # noqa: SLF001
                self._job, self.in_job = job, True
            except Exception:                                  # any failure at all: no generated code runs outside the job
                self._kill()
                job.close()
                raise SandboxError("unavailable", "the isolated process cannot be limited on this computer") from None
        answer = self._receive(self.start_timeout, during_start=True)
        if not answer.get("ready"):
            self._kill()
            raise SandboxError("unavailable", "the isolated process did not say it was ready")
        self.layers = self._report_layers(dict(answer.get("layers", {})))
        return self.layers

    def _report_layers(self, layers: dict[str, Any]) -> dict[str, Any]:
        """What protects this run, for coder_test to show: `net` is 'namespace' (the kernel refuses the network) or 'hook_only' (only the audit hook does);
        `files` is 'landlock' or 'hook_only'; `degraded` is true on Linux when either of them is missing, because Linux is where both are expected."""
        if self.in_job:
            layers["job_object"] = True
        layers["net"] = "namespace" if layers.get("net_namespace") == "active" else "hook_only"
        layers["files"] = "landlock" if layers.get("landlock") == "active" else "hook_only"
        layers["degraded"] = self._platform.startswith("linux") and "hook_only" in (layers["net"], layers["files"])
        return layers

    def _make_job(self) -> Any:
        if self._platform != "win32":
            return None
        from hokma.core.winjob import SandboxJob

        try:
            return SandboxJob(memory_bytes=self.memory_mb * 1024 * 1024, cpu_seconds=self.cpu_seconds)
        except Exception:
            raise SandboxError("unavailable", "the isolated process cannot be limited on this computer") from None

    # ---------------------------------------------------------------- the pipe
    def _spawn_and_read(self, spawned: "queue.Queue[tuple[str, Any]]") -> None:
        try:
            proc = subprocess.Popen(self.argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=str(self.workdir),
                                    env=self.environment(), close_fds=True, **spawn_kwargs())
        except OSError as exc:
            spawned.put(("error", exc))
            return
        spawned.put(("proc", proc))
        self._read_loop(proc)
        try:
            proc.wait(timeout=CLOSE_WAIT)      # this thread created the child: PDEATHSIG kills it when the thread ends, so the thread must outlive the exit
        except subprocess.TimeoutExpired:
            pass

    def _read_loop(self, proc: "subprocess.Popen[bytes]") -> None:
        assert proc.stdout is not None
        try:
            while True:
                line = proc.stdout.readline(MAX_MESSAGE + 2)
                if not line:
                    break
                if not line.endswith(b"\n") or len(line) - 1 > MAX_MESSAGE:
                    self._lines.put(("big", None))
                    break
                self._lines.put(("line", line))
        except (OSError, ValueError):
            pass
        self._lines.put(("eof", None))

    def send_raw(self, data: bytes) -> None:
        """One line to the process as it is (for the tests of the protocol)."""
        proc = self._proc
        if proc is None or not self.alive or proc.stdin is None:
            raise SandboxError("unavailable", "the isolated process is not running")
        try:
            proc.stdin.write(data)
            proc.stdin.flush()
        except (OSError, ValueError):
            self._reap()
            raise SandboxError("worker_died", "the isolated process closed its pipe") from None

    def read_raw(self, timeout: float | None = None) -> dict[str, Any]:
        """The next message of the process, parsed, with its error left in it (for the tests of the protocol)."""
        return self._receive(self.call_timeout if timeout is None else timeout, raw=True)

    def _rss_bytes(self) -> int | None:
        """Resident memory of the process: /proc on Linux, `ps` elsewhere (macOS has no /proc and RLIMIT_AS is only partly honoured there). On Windows the
        Job Object limits memory and this returns None."""
        if self._proc is None:
            return None
        if self._platform.startswith("linux"):
            try:
                with open(f"/proc/{self._proc.pid}/statm", "rb") as handle:
                    return int(handle.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
            except (OSError, ValueError, IndexError):
                return None
        if self._platform == "win32":
            return None
        return self._rss_from_ps()

    def _rss_from_ps(self) -> int | None:
        now = time.monotonic()
        if now - self._ps_at < PS_EVERY:                      # a process per look would cost more than the watch is worth
            return self._ps_last
        self._ps_at = now
        try:
            out = subprocess.run(["ps", "-o", "rss=", "-p", str(self._proc.pid)], capture_output=True, text=True, timeout=2, **spawn_kwargs())  # type: ignore[union-attr]
            self._ps_last = int(out.stdout.strip()) * 1024 if out.returncode == 0 and out.stdout.strip() else None      # ps gives KiB
        except (OSError, ValueError, subprocess.SubprocessError):
            self._ps_last = None
        return self._ps_last

    def _receive(self, timeout: float, *, raw: bool = False, during_start: bool = False) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._kill()
                raise SandboxError("time_limit", f"the isolated process did not answer within {timeout:g} s and was killed")
            try:
                kind, line = self._lines.get(timeout=min(WATCH_EVERY, remaining))
            except queue.Empty:
                rss = self._rss_bytes()
                if rss is not None and rss > self.memory_mb * 1024 * 1024:
                    self._kill()
                    raise SandboxError("memory_limit", f"the isolated process used more than {self.memory_mb} MB and was killed") from None
                continue
            if kind == "big":
                self._kill()
                raise SandboxError("bad_answer", "the answer is too large (over 8 MB)")
            if kind == "eof":
                code = self._reap()
                if during_start:
                    raise SandboxError("unavailable", f"the isolated process ended while starting (exit {code})")
                raise SandboxError("worker_died", f"the isolated process ended without an answer (exit {code})")
            try:
                message = json.loads(line.decode("utf-8"))                    # type: ignore[union-attr]
            except (ValueError, UnicodeDecodeError):
                self._kill()
                raise SandboxError("bad_answer", "the isolated process sent something that is not JSON") from None
            if not isinstance(message, dict):
                self._kill()
                raise SandboxError("bad_answer", "the isolated process sent something that is not an object")
            if raw:
                return message
            if "violation" in message:
                self._reap()
                raise SandboxError(str(message["violation"]), str(message.get("message", ""))[:300])
            return message

    # ---------------------------------------------------------------- the three operations
    def _request(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        if not self.alive:
            raise SandboxError("unavailable", "the isolated process is not running")
        try:
            data = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as exc:
            raise SandboxError("bad_input", f"the message is not plain finite JSON: {exc}") from None
        if len(data) - 1 > MAX_MESSAGE:
            raise SandboxError("bad_input", "the message is over 8 MB")
        self.send_raw(data)
        answer = self._receive(timeout)
        error = answer.get("error")
        if isinstance(error, dict):
            code = str(error.get("type", "bad_answer"))
            findings = error.get("findings") if isinstance(error.get("findings"), list) else None
            raise SandboxError(code if code in ERROR_CODES else "bad_answer", str(error.get("message", ""))[:340], findings)
        return answer

    def load(self, source: str, kind: str) -> dict[str, Any]:
        """Send the code. The worker checks it again, compiles it and gives back its manifest: schema, facts or fields, lookback and requests.
        The line ends are normalized here (CRLF to LF) and the hash of the stored rule is the hash of that text: `static_check.code_sha256`."""
        answer = self._request({"load": {"source": static_check.normalize_source(source), "kind": kind}}, self.load_timeout)
        manifest = answer.get("loaded")
        if not isinstance(manifest, dict):
            raise SandboxError("bad_answer", "the isolated process did not send a manifest")
        return manifest

    def load_version(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """Load the code of a stored version (a row of user_rule_versions): its status must be one that can run and the hash of its text must be the stored one,
        so a row edited by hand, a file that changed or a rejected version never reaches the interpreter. Whether the rule may OPERATE is another question,
        answered by `require_operable` in session 2/4."""
        status = row.get("status")
        if status not in LOADABLE_STATUSES:
            raise SandboxError("not_loadable", f"a rule version in status {status!r} does not run")
        text, expected = row.get("code_text"), row.get("code_sha256")
        if not isinstance(text, str) or not isinstance(expected, str) or static_check.code_sha256(text) != expected.lower():
            raise SandboxError("hash_mismatch", "the code of this version is not the code that was hashed")
        reviewed = row.get("reviewed_sha256")
        if status == OPERATING_STATUS and (not isinstance(reviewed, str) or reviewed.lower() != expected.lower()):      # a hash is hex: the case is not part of it
            raise SandboxError("hash_mismatch", "the owner read another text than the one stored")
        return self.load(text, str(row.get("kind", "")))

    def call(self, method: str, *, params: dict[str, Any] | None = None, ctx: dict[str, Any] | None = None, args: dict[str, Any] | None = None) -> dict[str, Any]:
        answer = self._request({"call": {"method": method, "params": params or {}, "ctx": ctx or {}, "args": args or {}}}, self.call_timeout)
        result = answer.get("ok")
        if not isinstance(result, dict):
            raise SandboxError("bad_answer", "the isolated process did not send an answer")
        return result

    # ---------------------------------------------------------------- the end
    def _kill(self) -> None:
        proc = self._proc
        self._dead = True
        if proc is None:
            return
        if self._job is not None:
            try:
                self._job.terminate()
            except OSError:
                pass
        try:
            proc.kill()
        except OSError:
            pass
        self._reap()

    def _reap(self) -> int | None:
        proc = self._proc
        self._dead = True
        if proc is None:
            return None
        try:
            self.exit_code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                self.exit_code = proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass
        if self._job is not None:
            self._job.close()
            self._job = None
        return self.exit_code

    def close(self) -> None:
        proc = self._proc
        if proc is None or self._dead:
            return
        try:
            if proc.poll() is None and proc.stdin is not None:
                proc.stdin.write(b'{"close":true}\n')
                proc.stdin.flush()
                try:
                    proc.wait(timeout=CLOSE_WAIT)          # an orderly close needs the process to answer and exit: on a loaded machine that takes seconds, not 2
                except subprocess.TimeoutExpired:
                    pass
        except (OSError, ValueError):
            pass
        if proc.poll() is None:
            self._kill()
        else:
            self._reap()

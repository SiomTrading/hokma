"""The fixes of the review of task 3414 on the parent side of the isolated process: memory on macOS, the hash seed, the layers report, the thread that
creates the child, CRLF, the stored version and the Windows job. What needs Windows or macOS is played with fakes and said so: nothing here ran there."""
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from hokma.factory.usercode import sandbox as sandbox_module
from hokma.factory.usercode import static_check
from hokma.factory.usercode.sandbox import Sandbox, SandboxError, require_operable
from usercode.support import alpha_ctx, INSIDE_UP, source

LINUX = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
UNSAFE = [sys.executable, "-I", "-X", "utf8", str(Path(__file__).with_name("unsafe_worker.py"))]
MB = 1024 * 1024


# ---------------------------------------------------------------- C: memory where there is no /proc

def test_rss_comes_from_ps_where_there_is_no_proc(tmp_path, monkeypatch):
    """MOCK: macOS is not available here. The call is the one macOS has: ps -o rss= -p PID, in KiB."""
    sb = Sandbox(workdir=tmp_path / "e")
    sb._platform = "darwin"
    sb._proc = types.SimpleNamespace(pid=4321)
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return types.SimpleNamespace(returncode=0, stdout="  2048\n")

    monkeypatch.setattr(sandbox_module.subprocess, "run", fake_run)
    assert sb._rss_bytes() == 2048 * 1024
    assert calls == [["ps", "-o", "rss=", "-p", "4321"]]
    assert sb._rss_bytes() == 2048 * 1024 and len(calls) == 1          # not another process within PS_EVERY


@pytest.mark.parametrize("stdout,code", [("", 0), ("abc", 0), ("100", 1)])
def test_a_ps_that_fails_or_says_nothing_is_no_measure_and_not_an_error(tmp_path, monkeypatch, stdout, code):
    sb = Sandbox(workdir=tmp_path / "e")
    sb._platform = "darwin"
    sb._proc = types.SimpleNamespace(pid=1)
    monkeypatch.setattr(sandbox_module.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=code, stdout=stdout))
    assert sb._rss_bytes() is None


def test_a_darwin_sandbox_kills_the_process_that_ps_says_is_over_the_limit(tmp_path, monkeypatch):
    """MOCK of ps on a real process: the watch of the parent uses the number ps gives, as it uses /proc on Linux (here it trips while the worker starts)."""
    monkeypatch.setattr(sandbox_module, "PS_EVERY", 0.0)
    monkeypatch.setattr(sandbox_module, "WATCH_EVERY", 0.01)
    sb = Sandbox(workdir=tmp_path / "e", call_timeout=5, memory_mb=100)
    sb._platform = "darwin"
    monkeypatch.setattr(sandbox_module.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=str(500 * 1024)))
    with pytest.raises(SandboxError) as caught:
        sb.start()
    assert caught.value.code == "memory_limit" and not sb.alive


# ---------------------------------------------------------------- D: hash seed

def test_the_interpreter_is_started_without_dash_I_so_that_the_hash_seed_is_honoured():
    argv = Sandbox(workdir=Path("x")).argv()
    assert "-I" not in argv and "-E" not in argv and "-s" in argv and "-P" in argv
    assert Sandbox.environment()["PYTHONHASHSEED"] == "0"
    assert not [k for k in Sandbox.environment() if k in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP")]


def test_two_processes_of_the_sandbox_give_the_same_hash_of_a_text():
    argv = Sandbox(workdir=Path("x")).argv()
    base = argv[:argv.index("-m")]
    outs = {subprocess.run(base + ["-c", "print(hash('abc'), hash(('a', 1)), flush=True)"], capture_output=True, text=True, env=Sandbox.environment()).stdout
            for _ in range(3)}
    assert len(outs) == 1 and outs.pop().strip()


def test_the_worker_still_finds_hokma_without_dash_I(tmp_path):
    with Sandbox(workdir=tmp_path / "e") as sb:
        assert sb.load(source("alpha"), "alpha")["kind"] == "alpha"


# ---------------------------------------------------------------- F: what protects this run

@LINUX
def test_the_layers_report_net_and_files_and_degraded(tmp_path):
    with Sandbox(workdir=tmp_path / "e") as sb:
        layers = sb.layers
    assert layers["net"] in ("namespace", "hook_only") and layers["files"] in ("landlock", "hook_only")
    assert layers["net"] == ("namespace" if layers["net_namespace"] == "active" else "hook_only")
    assert layers["degraded"] is ("hook_only" in (layers["net"], layers["files"]))


@LINUX
def test_a_run_without_landlock_is_marked_degraded_on_linux(tmp_path):
    sb = Sandbox(workdir=tmp_path / "e", worker_argv=UNSAFE + ["--no-landlock"])
    try:
        layers = sb.start()
    finally:
        sb.close()
    assert layers["files"] == "hook_only" and layers["degraded"] is True


def test_the_report_of_layers_for_each_platform(tmp_path):
    sb = Sandbox(workdir=tmp_path / "e")
    sb._platform = "linux"
    assert sb._report_layers({"net_namespace": "active", "landlock": "active"}) == {"net_namespace": "active", "landlock": "active", "net": "namespace",
                                                                                      "files": "landlock", "degraded": False}
    assert sb._report_layers({"net_namespace": "unavailable", "landlock": "active"})["degraded"] is True
    assert sb._report_layers({"net_namespace": "active", "landlock": "unavailable"})["files"] == "hook_only"
    sb._platform = "win32"
    assert sb._report_layers({"net_namespace": "not_supported"})["degraded"] is False and sb._report_layers({})["net"] == "hook_only"


# ---------------------------------------------------------------- G: the thread that creates the child

@LINUX
def test_a_sandbox_started_by_a_thread_that_ends_goes_on_living(tmp_path):
    """PR_SET_PDEATHSIG follows the thread that made the child. A pool thread that starts a Sandbox and ends must not take the process with it."""
    box: dict = {}

    def starter():
        sb = Sandbox(workdir=tmp_path / "e", call_timeout=5)
        sb.start()
        box["sb"] = sb

    thread = threading.Thread(target=starter)
    thread.start()
    thread.join()
    time.sleep(0.4)
    sb = box["sb"]
    try:
        assert sb.alive
        sb.load(source("alpha"), "alpha")
        assert sb.call("signal", ctx=alpha_ctx(INSIDE_UP))["value"] == 1.0
    finally:
        sb.close()


@LINUX
def test_the_child_is_created_by_the_reader_thread_which_lives_as_long_as_the_child(tmp_path):
    with Sandbox(workdir=tmp_path / "e") as sb:
        assert sb._reader is not None and sb._reader.is_alive()
    sb._reader.join(timeout=5)
    assert not sb._reader.is_alive()


# ---------------------------------------------------------------- H: CRLF

def test_a_rule_with_windows_line_ends_is_normalized_before_the_hash_and_the_check(tmp_path):
    lf = source("alpha")
    crlf = lf.replace("\n", "\r\n")
    assert not static_check.check(crlf, kind="alpha").ok                       # the check itself stays strict
    assert static_check.normalize_source(crlf) == lf
    assert static_check.code_sha256(crlf) == static_check.code_sha256(lf)
    assert static_check.check(static_check.normalize_source(crlf), kind="alpha").ok
    assert static_check.normalize_source("a\rb\r\nc") == "a\rb\nc"              # a lone carriage return is still a finding, never silently fixed
    with Sandbox(workdir=tmp_path / "e") as sb:
        assert sb.load(crlf, "alpha")["kind"] == "alpha"


def test_a_lone_carriage_return_still_fails_the_load(tmp_path):
    with Sandbox(workdir=tmp_path / "e") as sb:
        with pytest.raises(SandboxError) as caught:
            sb.load(source("alpha").replace("\n", "\r", 3), "alpha")
        assert caught.value.code == "static_check"


# ---------------------------------------------------------------- B: status and hash at load

def row(**kw):
    text = source("alpha")
    base = {"status": "generated", "kind": "alpha", "code_text": text, "code_sha256": static_check.code_sha256(text), "reviewed_sha256": None}
    return {**base, **kw}


def test_a_stored_version_loads_when_its_status_runs_and_its_hash_matches(tmp_path):
    with Sandbox(workdir=tmp_path / "e") as sb:
        assert sb.load_version(row())["kind"] == "alpha"
    with Sandbox(workdir=tmp_path / "e2") as sb:
        text = source("alpha").replace("\n", "\r\n")                                # stored with CRLF: the hash is of the normalized text
        assert sb.load_version(row(code_text=text))["kind"] == "alpha"


@pytest.mark.parametrize("status", ["rejected", "retired", "weird", None])
def test_a_version_in_a_status_that_does_not_run_is_refused_before_the_interpreter(tmp_path, status):
    sb = Sandbox(workdir=tmp_path / "e")
    with pytest.raises(SandboxError) as caught:
        sb.load_version(row(status=status))
    assert caught.value.code == "not_loadable" and sb.pid is None


@pytest.mark.parametrize("change", [{"code_sha256": "0" * 64}, {"code_text": source("alpha") + "\n# edited\n"}, {"code_sha256": None}, {"code_text": None}])
def test_a_version_whose_text_is_not_the_hashed_text_is_refused(tmp_path, change):
    with Sandbox(workdir=tmp_path / "e") as sb:
        with pytest.raises(SandboxError) as caught:
            sb.load_version(row(**change))
        assert caught.value.code == "hash_mismatch"


def test_an_owner_reviewed_version_must_carry_the_reviewed_hash_of_its_text(tmp_path):
    good = static_check.code_sha256(source("alpha"))
    with Sandbox(workdir=tmp_path / "e") as sb:
        with pytest.raises(SandboxError) as caught:
            sb.load_version(row(status="owner_reviewed", reviewed_sha256="f" * 64))
        assert caught.value.code == "hash_mismatch"
    with Sandbox(workdir=tmp_path / "e2") as sb:
        assert sb.load_version(row(status="owner_reviewed", reviewed_sha256=good))["kind"] == "alpha"


def test_only_owner_reviewed_may_operate():
    require_operable("owner_reviewed")
    for status in ("draft", "failed", "generated", "rejected", "retired", None):
        with pytest.raises(SandboxError) as caught:
            require_operable(status)
        assert caught.value.code == "not_reviewed"


# ---------------------------------------------------------------- I: the job of Windows (fakes: nothing here ran on Windows)

class FakeJob:
    instances: list = []

    def __init__(self, memory_bytes=768 * MB, cpu_seconds=90, fail=None):
        self.memory_bytes, self.cpu_seconds, self.fail = memory_bytes, cpu_seconds, fail
        self.assigned, self.closed, self.terminated = [], False, False
        FakeJob.instances.append(self)

    def assign(self, handle):
        if self.fail:
            raise self.fail
        self.assigned.append(handle)

    def terminate(self):
        self.terminated = True

    def close(self):
        self.closed = True


class HandlePopen(subprocess.Popen):
    _handle = 77                                                     # what Popen has on Windows


def windows_box(tmp_path, monkeypatch, **fail):
    FakeJob.instances.clear()
    monkeypatch.setattr("hokma.core.winjob.SandboxJob", lambda **kw: FakeJob(**kw, **fail))
    monkeypatch.setattr(sandbox_module.subprocess, "Popen", HandlePopen)
    sb = Sandbox(workdir=tmp_path / "e", memory_mb=300, cpu_seconds=17, start_timeout=10)
    sb._platform = "win32"
    return sb


def test_the_job_gets_the_memory_and_cpu_of_the_sandbox(tmp_path, monkeypatch):
    sb = windows_box(tmp_path, monkeypatch)
    try:
        layers = sb.start()
    finally:
        sb.close()
    job, = FakeJob.instances
    assert (job.memory_bytes, job.cpu_seconds) == (300 * MB, 17) and job.assigned == [77] and layers["job_object"] is True


@pytest.mark.parametrize("error", [OSError("denied"), RuntimeError("anything"), ValueError("x"), AttributeError("y")])
def test_any_failure_of_assign_kills_the_process_and_ends_in_unavailable(tmp_path, monkeypatch, error):
    sb = windows_box(tmp_path, monkeypatch, fail=error)
    with pytest.raises(SandboxError) as caught:
        sb.start()
    assert caught.value.code == "unavailable"
    job, = FakeJob.instances
    assert job.closed and not sb.alive and sb.exit_code is not None


def test_a_job_that_cannot_be_made_is_unavailable_not_a_crash(tmp_path, monkeypatch):
    def boom(**kw):
        raise RuntimeError("no job")

    monkeypatch.setattr("hokma.core.winjob.SandboxJob", boom)
    sb = Sandbox(workdir=tmp_path / "e")
    sb._platform = "win32"
    with pytest.raises(SandboxError) as caught:
        sb.start()
    assert caught.value.code == "unavailable" and sb.pid is None


def test_the_hashes_are_compared_without_regard_to_the_case_of_the_hex(tmp_path):
    text = source("alpha")
    digest = static_check.code_sha256(text)
    base = {"status": "owner_reviewed", "kind": "alpha", "code_text": text}
    sb = Sandbox(workdir=tmp_path / "empty", call_timeout=5)
    try:
        sb.start()
        assert sb.load_version({**base, "code_sha256": digest.upper(), "reviewed_sha256": digest.lower()})["kind"] == "alpha"
    finally:
        sb.close()
    for reviewed in (None, 7, "f" * 64):
        sb = Sandbox(workdir=tmp_path / "empty2", call_timeout=5)
        try:
            sb.start()
            with pytest.raises(SandboxError) as caught:
                sb.load_version({**base, "code_sha256": digest, "reviewed_sha256": reviewed})
            assert caught.value.code == "hash_mismatch"
        finally:
            sb.close()

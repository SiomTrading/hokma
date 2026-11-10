"""The detached updater (task 3146): the Update button no longer updates inside the server.

Order: launch the updater, shut the engine down, kill what is left in the slots, update, start Hokma again. The outcome
goes to `<data>/update-status.json` in plain words; a traceback only to the updater log. Processes are simulated."""
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dbsupport import db, env  # noqa: F401  (fixtures)
from hokma.core import config, session
from hokma.core.app import create_app
from hokma.core.proc import CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW, DETACHED_PROCESS
from hokma.i18n import t
from hokma.rpc.ws import SESSION_TOKEN
from hokma.update import api as update_api
from hokma.update import detached
from hokma.update.detached import Proc, Steps, StatusFile, Updater
from hokma.update.runner import UpdateResult
from hokma.update.source import UpdateError

TOKEN = "t" * 43
OWNER = "a" * 64


class FakePopen:
    """Records the launch and what was written to the child's standard input."""

    calls: list = []

    def __init__(self, argv, **kwargs):
        self.argv, self.kwargs = argv, kwargs
        self.pid = 4321
        self.stdin = io.BytesIO()
        self.stdin.close = lambda: None
        FakePopen.calls.append(self)

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def installed(env, monkeypatch):  # noqa: F811
    """An installed layout: `current` names slot a."""
    data = Path(os.environ["HOKMA_DATA_DIR"])
    (data / "runtime" / "a").mkdir(parents=True)
    (data / "current").write_text("a\n")
    FakePopen.calls = []
    return data


def recording_steps(log: list, *, update=None, start=True):
    def run_update(source, force, progress):
        log.append("update")
        progress("download")
        if update is not None:
            return update()
        return UpdateResult("updated", "0.0.14", "9.9.9", slot="b", restart_required=True)

    def run_start(port, handoff):
        log.append(("start", port, handoff, json.loads(detached.status_path().read_text())["state"]))
        return start

    return Steps(stop=lambda pids: log.append(("stop", tuple(pids))), kill=lambda: log.append("kill"),
                 clear_locks=lambda: log.append("clear_locks"), update=run_update, start=run_start)


# ---- the whole order --------------------------------------------------------------------------------------------

def test_order_is_launch_shutdown_kill_update_start(db, installed, monkeypatch):  # noqa: F811
    log: list = []
    monkeypatch.setattr(update_api.detached, "check_newer", lambda source=None, force=False: "9.9.9")

    def fake_launch(**kwargs):
        log.append("launch")
        fake_launch.kwargs = kwargs
        return 4321

    async def fake_shutdown(reason="shutting_down"):
        log.append(("shutdown", reason))

    monkeypatch.setattr(update_api.detached, "launch", fake_launch)
    app = create_app()
    monkeypatch.setattr(app.state.lifecycle, "shutdown", fake_shutdown)
    r = TestClient(app).post("/api/update", headers={"X-Hokma-Token": SESSION_TOKEN})
    assert r.json()["status"] == "launched"
    assert log == ["launch", ("shutdown", "updating")]
    assert os.getpid() in fake_launch.kwargs["wait_pids"]          # the updater waits for this engine to end

    # what the detached process does next
    handoff = fake_launch.kwargs["handoff"]
    code = Updater(port=8765, wait_pids=fake_launch.kwargs["wait_pids"], restart=True, handoff=handoff,
                   steps=recording_steps(log), log=io.StringIO()).run()
    assert code == 0
    assert [e if isinstance(e, str) else e[0] for e in log] == [
        "launch", "shutdown", "stop", "kill", "clear_locks", "update", "start"]
    start = log[-1]
    assert start[1] == 8765 and start[2] == handoff                # same port, same session: the tab reconnects
    assert start[3] == "done"                                      # the outcome is written BEFORE Hokma comes back
    body = json.loads(detached.status_path().read_text())
    assert body["state"] == "done" and body["to_version"] == "9.9.9" and body["started"] is True


# ---- failures are reported in plain words ------------------------------------------------------------------------

def test_an_update_error_is_written_to_the_status_file_and_the_old_version_starts_again(installed):
    log: list = []
    busy = t("update.slot_busy", path="C:/x/runtime/b")

    def fail():
        raise UpdateError(busy)

    out = io.StringIO()
    code = Updater(port=8765, wait_pids=[], restart=True, handoff=(TOKEN, OWNER, ""),
                   steps=recording_steps(log, update=fail), log=out).run()
    assert code == 1
    assert log[-1][0] == "start" and log[-1][3] == "failed"        # Hokma comes back, already knowing it failed
    body = json.loads(detached.status_path().read_text())
    assert body["state"] == "failed" and body["message"] == busy and body["started"] is True
    assert detached.failed_outcome() == busy                       # what the panel shows, with Retry


def test_an_unexpected_error_never_shows_a_traceback(installed):
    def boom():
        raise RuntimeError("internal detail")

    out = io.StringIO()
    code = Updater(port=8765, wait_pids=[], restart=True, handoff=None,
                   steps=recording_steps([], update=boom), log=out).run()
    assert code == 1
    message = json.loads(detached.status_path().read_text())["message"]
    assert message == t("update.detached.unexpected", path=detached.updater_log_path())
    assert "Traceback" not in message and "internal detail" not in message
    assert "Traceback" in out.getvalue() and "internal detail" in out.getvalue()   # the log keeps the detail


def test_a_failure_while_stopping_still_starts_hokma_again(installed):
    log: list = []
    steps = recording_steps(log)._replace(kill=lambda: (_ for _ in ()).throw(OSError("denied")))
    assert Updater(port=1, wait_pids=[], restart=True, handoff=None, steps=steps, log=io.StringIO()).run() == 1
    assert "update" not in log and log[-1][0] == "start"


def test_when_hokma_does_not_come_back_the_status_says_so(installed):
    steps = recording_steps([], start=False)
    assert Updater(port=1, wait_pids=[], restart=True, handoff=None, steps=steps, log=io.StringIO()).run() == 1
    body = json.loads(detached.status_path().read_text())
    assert body["state"] == "failed" and body["message"] == t("update.detached.not_started", version="9.9.9")


def test_without_restart_hokma_is_not_started(installed):
    log: list = []
    assert Updater(port=None, wait_pids=[], restart=False, handoff=None, steps=recording_steps(log),
                   log=io.StringIO()).run() == 0
    assert all(e[0] != "start" for e in log if isinstance(e, tuple))


# ---- launching -------------------------------------------------------------------------------------------------

def test_launch_copies_the_updater_outside_the_slots_and_hands_the_secrets_on_stdin(installed, tmp_path):
    pid = detached.launch(handoff=(TOKEN, OWNER, ""), port=8765, wait_pids=[11, 22], restart=True,
                          to_version="9.9.9", popen=FakePopen, temp_parent=tmp_path / "tmp")
    assert pid == 4321
    proc = FakePopen.calls[0]
    python, script = Path(proc.argv[0]), Path(proc.argv[3])
    runtime = config.runtime_root().resolve()
    for path in (python, script):
        assert runtime not in path.resolve().parents
    assert script.is_file() and (script.parent / "hokma" / "update" / "detached.py").is_file()
    assert not list(script.parent.rglob("__pycache__"))
    assert proc.argv[1:3] == ["-I", "-B"]
    assert proc.argv.count("--wait-pid") == 2 and "--restart" in proc.argv and "8765" in proc.argv
    assert proc.stdin.getvalue() == f"{TOKEN}\n{OWNER}\n\n".encode()
    assert TOKEN not in " ".join(proc.argv) and TOKEN not in json.dumps(proc.kwargs["env"])   # stdin only
    assert proc.kwargs["start_new_session"] is True                # POSIX: detached from the engine
    assert proc.kwargs["env"]["HOKMA_DATA_DIR"] == str(installed)
    assert json.loads(detached.status_path().read_text())["state"] == "waiting"


def test_on_windows_the_updater_is_detached_windowless_and_leaves_the_job(installed, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    detached.launch(handoff=None, port=None, wait_pids=[], restart=False, popen=FakePopen,
                    temp_parent=tmp_path / "tmp")
    flags = FakePopen.calls[0].kwargs["creationflags"]
    for flag in (DETACHED_PROCESS, CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW, detached.CREATE_BREAKAWAY_FROM_JOB):
        assert flags & flag


def test_a_job_that_forbids_leaving_falls_back_to_a_plain_detached_start(installed, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")

    class Picky(FakePopen):
        def __init__(self, argv, **kwargs):
            if kwargs["creationflags"] & detached.CREATE_BREAKAWAY_FROM_JOB:
                raise PermissionError(5, "Access is denied")
            super().__init__(argv, **kwargs)

    detached.launch(handoff=None, port=None, wait_pids=[], restart=False, popen=Picky, temp_parent=tmp_path / "t")
    assert not FakePopen.calls[0].kwargs["creationflags"] & detached.CREATE_BREAKAWAY_FROM_JOB


def test_a_launch_that_cannot_start_says_so_in_plain_words(installed, tmp_path):
    def broken(argv, **kwargs):
        raise OSError("nope")

    with pytest.raises(UpdateError) as caught:
        detached.launch(handoff=None, port=None, wait_pids=[], restart=False, popen=broken,
                        temp_parent=tmp_path / "tmp")
    assert str(caught.value) == t("update.detached.launch_failed")
    assert not list((tmp_path / "tmp").glob(detached.FOLDER_PREFIX + "*"))
    assert detached.read_status() is None


def test_the_staged_copy_runs_on_its_own(installed, tmp_path):
    """The copy imports with the standard library only: no venv, no PYTHONPATH, isolated mode."""
    folder = detached.stage_updater(tmp_path / "tmp")
    r = subprocess.run([sys.executable, "-I", "-B", str(folder / detached.ENTRY_NAME), "--help"],
                       capture_output=True, text=True, timeout=60, env={"PATH": os.environ.get("PATH", "")})
    assert r.returncode == 0 and "--wait-pid" in r.stdout, r.stderr


# ---- the steps themselves -------------------------------------------------------------------------------------

def test_leftovers_under_the_slots_or_the_python_are_killed_and_nothing_else(installed):
    data = str(installed)
    me = os.getpid()
    procs = [
        Proc(1, f"{data}/runtime/b/venv/bin/python", "python -m uvicorn"),
        Proc(2, "/usr/bin/node", f"node {data}/runtime/a/app/x.js"),
        Proc(3, f"{data}/python/cpython-3.12/bin/python3", "python3"),
        Proc(4, "/usr/bin/bash", "bash"),
        Proc(5, f"{data}/runtime-other/x", "x"),
        Proc(me, f"{data}/python/cpython-3.12/bin/python3", "the updater itself"),
    ]
    killed: list[int] = []
    rounds = iter([procs, []])
    detached.kill_leftovers(list_fn=lambda: next(rounds), kill_fn=killed.append, sleep=lambda s: None, windows=False)
    assert killed == [1, 2, 3]


def test_on_windows_paths_match_without_case():
    roots = [Path(r"C:\Users\crist\AppData\Local\Hokma\runtime\b")]
    proc = Proc(9, r"c:\users\CRIST\appdata\local\hokma\RUNTIME\B\venv\Scripts\hokma.exe", "")
    assert detached.runs_from(proc, [Path(str(r).replace("/", "\\")) for r in roots], windows=True)


def test_stop_asks_the_supervisor_only_when_it_does_not_end_by_itself(installed, monkeypatch):
    from hokma.core import control, enginelock

    monkeypatch.setattr(enginelock, "read", lambda: {"pid": 77, "port": 8765, "started_at": "x"})
    asked = []
    monkeypatch.setattr(control, "request", lambda cmd, timeout=None: asked.append(cmd) or {"ok": True})
    now = [0.0]
    alive = {11, 77}

    def sleep(seconds):
        now[0] += seconds
        if now[0] > 6:
            alive.clear()                                      # the supervisor ends once asked

    assert detached.stop_hokma([11], alive=lambda p: p in alive, clock=lambda: now[0], sleep=sleep) is True
    assert asked == ["stop"]


def test_start_hands_the_same_session_to_the_new_supervisor(installed):
    FakePopen.calls = []
    assert detached.start_hokma(8765, (TOKEN, OWNER, ""), popen=FakePopen, healthy=lambda port: True) is True
    proc = FakePopen.calls[0]
    assert proc.argv[0] == str(config.slot_exe("a"))
    assert proc.argv[1:] == ["start", "--port", "8765", "--no-browser", "--handoff-stdin"]
    assert proc.stdin.getvalue() == f"{TOKEN}\n{OWNER}\n\n".encode()


def test_start_without_a_session_is_a_plain_start(installed):
    FakePopen.calls = []
    assert detached.start_hokma(None, None, popen=FakePopen, healthy=lambda port: True,
                                clock=iter([0, 0, 999]).__next__, sleep=lambda s: None) is False
    assert FakePopen.calls[0].argv[1:] == ["start"]


def test_the_handoff_lines_are_checked(installed):
    assert detached.read_handoff(io.StringIO(f"{TOKEN}\n{OWNER}\n\n")) == (TOKEN, OWNER, "")
    assert detached.read_handoff(io.StringIO("\n\n\n")) is None
    assert detached.read_handoff(io.StringIO(f"{TOKEN}\nnot-a-hash\n\n")) is None


def test_the_session_secrets_are_the_ones_of_this_engine():
    token, owner, once = session.handoff_secrets()
    assert token == session.token()

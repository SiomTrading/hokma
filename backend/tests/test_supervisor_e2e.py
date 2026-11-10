"""Supervisor end to end on Linux/macOS: a fake installation with two runtime slots, a fake update source, the real
supervisor and the real engine. U2 to U6 of the release plan, plus the session token checks.

Windows and macOS variants of U4 and U6 are not run here (see docs/updates-and-restart.md)."""
import os
import pty
import signal
import subprocess
import json
import sys
import urllib.error
import urllib.request

import pytest

from hokma.i18n import t
from supervisor_support import (BASE_VERSION, Install, children_of, cmdline, find_engine, free_port, health,
                                holds_path, http_json, session_pids, token_in_files, token_in_process, wait_until)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process model")


def _bump(version: str, patch: int) -> str:
    major, minor, current = version.split(".")
    return f"{major}.{minor}.{int(current) + patch}"


V1, V2 = _bump(BASE_VERSION, 1), _bump(BASE_VERSION, 2)


class Running:
    """`hokma start` of slot a, in its own session (like a terminal window)."""

    def __init__(self, tmp_path) -> None:
        self.install = Install(tmp_path)
        self.port = free_port()
        self.proc = subprocess.Popen(
            [str(self.install.exe_a), "start", "--port", str(self.port), "--no-browser"], env=self.install.env(),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        line = self.proc.stdout.readline().strip()
        assert "#t=" in line and "&o=" in line, line
        self.token, self.once = line.split("#t=")[1].split("&o=")
        assert self.token and self.once
        assert wait_until(lambda: health(self.port), 40), "the engine never answered /health"

    @property
    def sid(self) -> int:
        return self.proc.pid

    def api(self, path: str, method: str = "GET"):
        return http_json(f"http://127.0.0.1:{self.port}{path}", self.token, method)

    def lock_pid(self) -> int | None:
        try:
            return json.loads((self.install.data / "engine.lock").read_text())["pid"]
        except (OSError, ValueError, KeyError):
            return None

    def stop(self) -> None:
        sessions = {self.sid}
        held = self.lock_pid()                     # after an update the updater started a supervisor of its own
        if held is not None:
            try:
                sessions.add(os.getsid(held))
            except ProcessLookupError:
                pass
        for sid in sessions:
            try:
                os.killpg(sid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        self.proc.wait()


@pytest.fixture
def running(tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_LANG", "en")
    box = Running(tmp_path)
    yield box
    box.stop()


def test_token_is_not_in_arguments_environment_or_files(running):
    pids = session_pids(running.sid)
    assert len(pids) >= 2                                  # the supervisor and the engine
    for pid in pids:
        assert token_in_process(pid, running.token) == [], cmdline(pid)
    ins = running.install
    assert token_in_files(running.token, ins.data, ins.work, ins.home) == []
    for pid in pids:                                       # the one-time secret is no more at rest than the token
        assert token_in_process(pid, running.once) == [], cmdline(pid)
    assert token_in_files(running.once, ins.data, ins.work, ins.home) == []
    # and the engine does use it: the right token works, a wrong one does not
    assert running.api("/api/update/state")["version"] == BASE_VERSION
    with pytest.raises(urllib.error.HTTPError) as err:
        http_json(f"http://127.0.0.1:{running.port}/api/update/state", "x" * 32)
    assert err.value.code == 401


def register(running, nonce, once=None):
    body = json.dumps({"nonce": nonce, **({"once": once} if once is not None else {})}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{running.port}/api/session/owner-nonce", data=body, method="POST",
                                 headers={"X-Hokma-Token": running.token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, None
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())["detail"]["code"]


def test_3045_only_the_start_link_registers_the_owner_and_it_survives_a_restart(running):
    nonce, intruder = "0123456789abcdef0123456789abcdef", "f" * 32
    assert register(running, intruder) == (403, "owner_secret_required")             # the token alone is not enough
    assert register(running, intruder, "W" * 22) == (403, "owner_secret_required")
    assert register(running, nonce, running.once) == (200, None)                      # the tab, with the link's secret
    assert register(running, intruder, running.once) == (409, "owner_already_registered")   # one use
    for _ in range(2):                                    # a crash, twice: the engine is relaunched with what it needs
        first = find_engine(running.sid)
        os.kill(first, signal.SIGKILL)
        assert wait_until(lambda: (find_engine(running.sid) not in (None, first)) and health(running.port), 40)
        assert register(running, nonce) == (200, None)                                 # same tab, no new secret
        assert register(running, intruder) == (409, "owner_already_registered")       # and nobody else takes its place
        assert register(running, intruder, running.once) == (409, "owner_already_registered")


def test_3045_a_crash_before_the_registration_keeps_the_secret_for_the_tab(running):
    first = find_engine(running.sid)
    os.kill(first, signal.SIGKILL)
    assert wait_until(lambda: (find_engine(running.sid) not in (None, first)) and health(running.port), 40)
    assert register(running, "f" * 32) == (403, "owner_secret_required")
    assert register(running, "0" * 32, running.once) == (200, None)                   # the link still works


def test_u5_one_crash_is_relaunched_with_the_same_port_and_token(running):
    first = find_engine(running.sid)
    os.kill(first, signal.SIGKILL)
    assert wait_until(lambda: (find_engine(running.sid) not in (None, first)) and health(running.port), 40)
    assert running.api("/api/update/state")["phase"] == "idle"          # same token, same port, serving again


def test_u5_four_crashes_in_a_minute_stop_and_say_why(running):
    seen = set()
    for _ in range(4):
        engine = wait_until(lambda: (find_engine(running.sid) not in (None, *seen)) and find_engine(running.sid), 40)
        assert engine, "the supervisor did not relaunch the engine"
        seen.add(engine)
        os.kill(engine, signal.SIGKILL)
    assert running.proc.wait(timeout=30) == 1
    assert health(running.port) is None
    log = (running.install.data / "logs" / "engine.log").read_text()
    assert "stopping: 4 crashes in 60 s" in log and log.count("engine crashed") == 4
    assert running.token not in log
    assert session_pids(running.sid) == []


def test_u6_closing_the_terminal_leaves_no_process(tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_LANG", "en")
    ins, port = Install(tmp_path), free_port()
    pid, master = pty.fork()                               # a real terminal: the child is its session leader
    if pid == 0:
        os.execve(str(ins.exe_a), [str(ins.exe_a), "start", "--port", str(port), "--no-browser"], ins.env())
    try:
        assert wait_until(lambda: health(port), 40)
        assert len(session_pids(pid)) >= 2
        os.close(master)                                   # closing the window: the kernel sends SIGHUP
        assert wait_until(lambda: not session_pids(pid), 5), f"left behind: {session_pids(pid)}"
        assert health(port) is None
    finally:
        for left in session_pids(pid):
            os.kill(left, signal.SIGKILL)
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass


def test_u6_engine_dies_when_the_supervisor_is_killed_without_a_chance_to_react(running):
    engine = find_engine(running.sid)
    os.kill(running.proc.pid, signal.SIGKILL)              # no handler can run on SIGKILL
    assert wait_until(lambda: engine not in session_pids(running.sid), 5), "the engine kept running"
    assert health(running.port) is None


def test_u2_u4_two_updates_in_a_row_restart_by_themselves(running):
    """Task 3146: the update runs in the detached updater while Hokma is down; it starts a new supervisor with the
    same session on the same port, and nothing of Hokma keeps running from the slot that is rebuilt."""
    ins = running.install
    connect = pytest.importorskip("websockets.sync.client").connect

    # first update: a -> b, with a tab connected
    ins.publish(V1)
    with connect(f"ws://127.0.0.1:{running.port}/rpc?token={running.token}") as ws:
        result = running.api("/api/update", "POST")
        assert result["status"] == "launched" and result["to_version"] == V1 and result["restarting"] is True
        closed = pytest.raises(Exception, ws.recv)         # the open tab learns why the socket closed
        assert closed.value.rcvd.code == 1012 and closed.value.rcvd.reason == "updating"
    assert wait_until(lambda: (health(running.port) or {}).get("version") == V1, 180), "no automatic restart"
    assert ins.current() == "b"
    assert running.api("/api/update/state")["version"] == V1            # same token, same port
    supervisor = running.lock_pid()
    assert supervisor is not None and str(ins.slot_dir("b")) in " ".join(cmdline(supervisor))
    for pid in session_pids(os.getsid(supervisor)):
        assert holds_path(pid, ins.slot_dir("a")) == [], (pid, cmdline(pid))

    # second update, with no restart by hand in between: b -> a (which is rebuilt from scratch)
    ins.publish(V2)
    result = running.api("/api/update", "POST")
    assert result["status"] == "launched" and result["to_version"] == V2 and result["restarting"] is True
    assert wait_until(lambda: (health(running.port) or {}).get("version") == V2, 180), "second restart did not happen"
    assert ins.current() == "a"
    assert running.api("/api/update/state")["version"] == V2
    supervisor = running.lock_pid()
    assert supervisor is not None and str(ins.slot_dir("a")) in " ".join(cmdline(supervisor))
    for pid in session_pids(os.getsid(supervisor)):
        assert holds_path(pid, ins.slot_dir("b")) == [], (pid, cmdline(pid))
        assert token_in_process(pid, running.token) == []
    # the updater writes `started` right after /health answers: wait for that write, not for /health
    assert wait_until(lambda: "started" in json.loads((ins.data / "update-status.json").read_text()), 30)
    status = json.loads((ins.data / "update-status.json").read_text())
    assert status["state"] == "done" and status["to_version"] == V2 and status["started"] is True


def test_u3_a_package_with_a_different_sha_is_refused_and_the_app_keeps_working(running):
    running.install.publish(V1, bad_sha=True)
    result = running.api("/api/update", "POST")
    assert result["status"] == "launched"
    status_file = running.install.data / "update-status.json"

    def finished():
        try:
            return json.loads(status_file.read_text()).get("started") is not None
        except (OSError, ValueError):
            return False

    assert wait_until(finished, 180), "the updater never finished"
    assert wait_until(lambda: health(running.port) is not None, 60)
    assert running.install.current() == "a" and health(running.port)["version"] == BASE_VERSION
    state = running.api("/api/update/state")                            # same session after the failed update
    assert state["phase"] == "error" and t("update.package_sha_mismatch") in state["error"]
    assert "Traceback" not in state["error"]


def test_the_updater_reports_its_real_stages_in_order(tmp_path, monkeypatch):
    from hokma.update.runner import run_update

    ins = Install(tmp_path)
    for key, value in ins.env().items():
        if key.startswith("HOKMA_") or key == "HOME":
            monkeypatch.setenv(key, value)
    ins.publish(V1)
    stages = []
    result = run_update(str(ins.manifest), progress=stages.append)
    assert result.status == "updated" and result.slot == "b"
    assert stages == ["download", "unpack", "python", "dependencies", "verify", "switch"]

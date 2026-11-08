"""The control channel, the engine lock and the clean stop of the supervisor, in one process with fakes.
Windows-only pieces are guarded by the platform and are NOT run on Windows by the automated tests."""
import io
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time

import pytest

from hokma.core import config, control, enginelock
from hokma.core.engine_main import watch_parent
from hokma.core.supervisor import STOP_LINE, EngineLog, Supervisor, run_supervisor

TOKEN = "T" * 32


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOKMA_LANG", "en")
    (tmp_path / "data").mkdir()
    return tmp_path / "data"


def served(data_dir_unused=None, extra=None):
    stops = []
    info = {"pid": os.getpid(), "port": 8765, "started_at": "2026-01-01T00:00:00Z", "engine": "up", **(extra or {})}
    server = control.ControlServer(lambda: dict(info), lambda: stops.append(True))
    server.start()
    return server, stops


# ---- the channel -------------------------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX socket")
def test_the_socket_is_private_to_the_user(data):
    server, _ = served()
    try:
        mode = stat.S_IMODE((data / "control.sock").stat().st_mode)
        assert mode == 0o600 and (data / "control.sock").stat().st_uid == os.getuid()
    finally:
        server.close()
    assert not (data / "control.sock").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX socket")
def test_status_and_stop_work_and_nothing_else_does(data):
    server, stops = served()
    try:
        status = control.request("status")
        assert status == {"ok": True, "pid": os.getpid(), "port": 8765, "started_at": "2026-01-01T00:00:00Z",
                          "engine": "up"} and stops == []
        assert control.request("stop") == {"ok": True, "stopping": True} and stops == [True]
        with pytest.raises(ValueError):
            control.request("token")
        for raw in (b"token\n", b"restart\n", b"status;stop\n", b"\n", b"\xff\xfe\n"):
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(3)
                client.connect(str(data / "control.sock"))
                client.sendall(raw)
                assert json.loads(client.recv(1000)) == {"ok": False, "error": "unknown_command"}
        assert stops == [True]                                  # none of those did anything
    finally:
        server.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX socket")
def test_a_reply_never_carries_more_than_the_fixed_keys(data):
    server, _ = served(extra={"token": "S" * 32, "once": "O" * 22, "owner_hash": "h" * 64})
    try:
        reply = json.dumps(control.request("status"))
        assert "S" * 32 not in reply and "O" * 22 not in reply and "h" * 64 not in reply
        assert set(json.loads(reply)) == {"ok", "pid", "port", "started_at", "engine"}
    finally:
        server.close()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="long socket paths are reached through /proc")
def test_a_very_long_data_folder_still_gets_a_working_channel(tmp_path, monkeypatch):
    deep = tmp_path / ("d" * 60) / ("e" * 60) / "data"
    deep.mkdir(parents=True)
    monkeypatch.setenv("HOKMA_DATA_DIR", str(deep))
    assert len(str(deep / "control.sock")) > 100
    server, _ = served()
    try:
        assert control.request("status")["ok"] is True
    finally:
        server.close()


def test_no_supervisor_means_no_answer(data):
    assert control.request("status") is None


def test_pipe_name_is_per_user_and_safe():
    name = control.pipe_name()
    assert name.startswith("\\\\.\\pipe\\hokma-control-") and "/" not in name[9:] and " " not in name


@pytest.mark.skipif(sys.platform != "win32", reason="named pipes exist only on Windows")
def test_windows_pipe_round_trip(data):
    server, stops = served()
    try:
        assert control.request("status")["ok"] is True
        assert control.request("stop")["ok"] is True and stops == [True]
    finally:
        server.close()


# ---- the engine lock ---------------------------------------------------------------------------------------

def test_lock_holds_pid_port_and_time_and_nothing_else(data):
    assert enginelock.claim(8766) is True
    lock = enginelock.read()
    assert set(lock) == {"pid", "port", "started_at"} and lock["pid"] == os.getpid() and lock["port"] == 8766
    assert stat.S_IMODE((data / "engine.lock").stat().st_mode) == 0o600 or sys.platform == "win32"


def test_a_live_supervisor_keeps_the_lock_and_a_dead_one_loses_it(data):
    assert enginelock.claim(8765, pid=os.getppid()) is True          # a live process that is not us
    assert enginelock.claim(8766) is False                            # fresh and alive: nobody else may become the supervisor
    assert enginelock.read()["port"] == 8765
    (data / "engine.lock").unlink()
    gone = subprocess.Popen(["true"])
    gone.wait()
    assert enginelock.claim(8765, pid=gone.pid) is True
    assert enginelock.pid_alive(gone.pid) is False
    assert enginelock.claim(8767) is True                             # stale: replaced
    assert enginelock.read()["port"] == 8767


def test_a_lock_that_is_not_a_lock_is_ignored(data):
    for text in ("", "not json", "[]", json.dumps({"pid": 1, "port": 8765}),
                 json.dumps({"pid": "1", "port": 8765, "started_at": "x"}),
                 json.dumps({"pid": 1, "port": 8765, "started_at": "x", "token": "T" * 32}),
                 json.dumps({"pid": 1, "port": 99999, "started_at": "x"})):
        (data / "engine.lock").write_text(text)
        assert enginelock.read() is None, text


def test_release_only_removes_our_own_lock(data):
    enginelock.take_over(8765, pid=os.getpid() + 100000)
    enginelock.release()
    assert enginelock.read() is not None                                   # not ours: a successor owns it
    enginelock.release(pid=os.getpid() + 100000)
    assert enginelock.read() is None


def test_an_old_pid_that_was_reused_is_not_a_supervisor(data):
    old = {"pid": os.getppid(), "port": 8765, "started_at": "2020-01-01T00:00:00Z"}
    assert enginelock.age_seconds(old) > enginelock.FRESH_SECONDS
    assert enginelock.holder_alive(old) is False                           # alive, but it does not answer on the channel


# ---- the clean stop of the supervisor ---------------------------------------------------------------------

class Lifeline(io.BytesIO):
    """The engine's standard input: closing it is what a real engine sees as the end of the stop line."""

    def __init__(self, on_close) -> None:
        super().__init__()
        self.on_close, self.sent = on_close, b""

    def close(self) -> None:
        self.sent = self.getvalue()
        self.on_close()


class StubbornProc:
    """An engine that ends when its standard input closes, or never (`stubborn`: only kill() ends it)."""

    def __init__(self, stubborn: bool = False) -> None:
        self.ended, self.stubborn, self.killed = threading.Event(), stubborn, False
        self.stdin = Lifeline(lambda: None if stubborn else self.ended.set())
        self.stderr, self.code = io.BytesIO(), None

    def wait(self, timeout=None):
        self.ended.wait(timeout if timeout is not None else 30)
        self.code = 0
        return 0

    def poll(self):
        return self.code if self.ended.is_set() else None

    def terminate(self):
        self.killed = True
        self.ended.set()

    def kill(self):
        self.killed = True
        self.ended.set()


def make(tmp_path, proc, **kwargs):
    return Supervisor(port=8765, token=TOKEN, argv=lambda: ["engine"], log=EngineLog(tmp_path / "engine.log", (TOKEN,)),
                      stderr=io.StringIO(), popen=lambda *a, **k: proc, needs_handover=lambda: False, **kwargs)


def test_a_clean_stop_writes_the_stop_line_closes_the_pipe_and_ends_with_zero(tmp_path):
    proc = StubbornProc()
    sup = make(tmp_path, proc)
    result = []
    thread = threading.Thread(target=lambda: result.append(sup.run()))
    thread.start()
    deadline = time.monotonic() + 5
    while sup._child is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sup.status()["engine"] == "up" and sup.status()["pid"] == os.getpid()
    sup.request_shutdown()
    sup.request_shutdown()                                           # twice is harmless
    thread.join(5)
    assert result == [0]
    assert proc.stdin.sent.endswith(STOP_LINE) and TOKEN.encode() in proc.stdin.sent
    assert not proc.killed                                           # it ended by itself
    assert sup._force_timer.finished.is_set()                        # run() cancelled the timer: no late force


def test_after_the_grace_time_the_engine_and_the_group_are_closed_by_force(tmp_path, monkeypatch):
    import hokma.core.supervisor as module

    monkeypatch.setattr(module, "STOP_GRACE_SECONDS", 0.3)
    proc, events = StubbornProc(stubborn=True), []
    sup = make(tmp_path, proc, kill_group=lambda: events.append("group"), before_force=lambda: events.append("release"))
    thread = threading.Thread(target=sup.run)
    thread.start()
    while sup._child is None:
        time.sleep(0.01)
    sup.request_shutdown()
    thread.join(5)
    assert not thread.is_alive() and proc.killed
    assert events == ["release", "group"]                            # lock and channel are released before the group dies
    assert "did not end" in (tmp_path / "engine.log").read_text()


def test_a_stop_that_arrives_before_any_engine_runs_ends_the_loop(tmp_path):
    proc = StubbornProc()
    sup = make(tmp_path, proc)
    sup.request_shutdown()
    assert sup.run() == 0


def test_the_engine_tells_a_stop_line_from_a_dead_supervisor():
    events = []
    watch_parent(io.StringIO("stop\n"), lambda: events.append("gone"), lambda: events.append("stop")).join(2)
    watch_parent(io.StringIO(""), lambda: events.append("gone"), lambda: events.append("stop")).join(2)
    assert events == ["stop", "gone"]
    legacy = []
    watch_parent(io.StringIO("junk"), lambda: legacy.append("gone")).join(2)    # no on_stop: data is ignored, EOF is gone
    assert legacy == ["gone"]


def test_checkpoint_empties_the_wal_and_a_missing_database_is_fine(data):
    import sqlite3

    from hokma.core.lifecycle import checkpoint_database
    from hokma.db.paths import db_path

    checkpoint_database()                                            # no file yet: nothing happens
    keeper = sqlite3.connect(db_path())                              # a connection that keeps the wal file alive
    keeper.execute("PRAGMA journal_mode=WAL")
    keeper.execute("CREATE TABLE t (x)")
    keeper.execute("INSERT INTO t VALUES (1)")
    keeper.commit()
    wal = db_path().with_name(db_path().name + "-wal")
    assert wal.exists() and wal.stat().st_size > 0
    checkpoint_database()
    assert wal.stat().st_size == 0
    keeper.close()


# ---- ports -------------------------------------------------------------------------------------------------

def _hold(ports):
    holders = []
    for number in ports:
        holder = socket.socket()
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            holder.bind(("127.0.0.1", number))
        except OSError:
            for h in holders:
                h.close()
            pytest.skip(f"port {number} is not free")
        holder.listen()
        holders.append(holder)
    return holders


def _range(monkeypatch, size):
    from supervisor_support import free_port

    first = free_port()
    monkeypatch.setattr(config, "DEFAULT_PORT", first)
    monkeypatch.setattr(config, "LAST_PORT", first + size - 1)
    return first


def test_the_next_free_port_is_used_and_the_lock_names_it(data, monkeypatch):
    first = _range(monkeypatch, 3)
    holders = _hold([first])
    seen = []
    monkeypatch.setattr(Supervisor, "run", lambda self: seen.append((self.port, enginelock.read())) or 0)
    try:
        assert run_supervisor(port=None, print_url=True) == 0
    finally:
        for h in holders:
            h.close()
    port, lock = seen[0]
    assert port == first + 1 and lock["port"] == first + 1 and lock["pid"] == os.getpid()
    assert enginelock.read() is None                                 # removed on exit


def test_no_free_port_in_the_range_exits_with_5(data, monkeypatch, capsys):
    first = _range(monkeypatch, 3)
    holders = _hold(range(first, first + 3))
    try:
        assert run_supervisor(port=None) == 5
    finally:
        for h in holders:
            h.close()
    assert str(first) in capsys.readouterr().err and enginelock.read() is None


def test_an_explicit_busy_port_is_not_moved(data, capsys):
    from supervisor_support import free_port

    port = free_port()
    holders = _hold([port])
    try:
        assert run_supervisor(port=port) == 1
    finally:
        for h in holders:
            h.close()
    assert enginelock.read() is None


# ---- the detached launch (static) ---------------------------------------------------------------------------

def test_the_detached_command_on_windows_uses_pythonw_and_posix_uses_the_slot_command(data, monkeypatch):
    from hokma.core import launcher

    scripts = data / "runtime" / "a" / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "pythonw.exe").write_text("")
    (data / "runtime" / "a" / "venv" / "bin").mkdir(parents=True)
    (data / "current").write_text("a\n")
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    argv = launcher.supervisor_argv(None)
    assert argv[0].endswith("pythonw.exe") and argv[1:] == ["-m", "hokma_cli.main", "start", "--foreground"]
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    argv = launcher.supervisor_argv(8800)
    assert argv[0].endswith("runtime/a/venv/bin/hokma") and argv[1:] == ["start", "--foreground", "--port", "8800"]


def test_spawn_flags_of_a_detached_supervisor(monkeypatch):
    from hokma.core import proc

    monkeypatch.setattr(proc.sys, "platform", "win32")
    flags = proc.spawn_kwargs(detached=True)["creationflags"]
    assert flags & proc.DETACHED_PROCESS and flags & proc.CREATE_NO_WINDOW and flags & proc.CREATE_NEW_PROCESS_GROUP
    monkeypatch.setattr(proc.sys, "platform", "linux")
    assert proc.spawn_kwargs(detached=True) == {"start_new_session": True}

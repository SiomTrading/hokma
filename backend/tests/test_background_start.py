"""R2 end to end on Linux/macOS: `hokma start` leaves the terminal, a second start does not make a second supervisor,
`hokma stop` leaves no process, `hokma restart` makes a new session, and a supervisor killed by force takes its
engine with it. The real supervisor and the real engine of a fake installation run; the browser is a script that
writes the address it was given to a file.

The Windows parts (named pipe, DETACHED_PROCESS, the job) are covered by static checks in test_control_channel.py
and are NOT run on Windows by these tests."""
import json
import os
import signal
import socket
import subprocess
import sys
import time
from http.client import HTTPConnection

import pytest

from supervisor_support import Install, find_engine, free_port, health, session_pids, wait_until

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process model")


class Background:
    """A fake installation whose `hokma` commands run with a script as the browser."""

    def __init__(self, tmp_path) -> None:
        self.install = Install(tmp_path)
        self.opened = tmp_path / "browser.log"
        script = tmp_path / "fake-browser.sh"
        script.write_text(f'#!/bin/sh\necho "$1" >> "{self.opened}"\n')
        script.chmod(0o755)
        self.browser = f"{script} %s"

    def hokma(self, *args: str, timeout: float = 90.0) -> subprocess.CompletedProcess:
        return subprocess.run([str(self.install.exe_a), *args], env=self.install.env(BROWSER=self.browser),
                              capture_output=True, text=True, timeout=timeout)

    def addresses(self) -> list[str]:
        return self.opened.read_text().split() if self.opened.exists() else []

    def lock(self) -> dict | None:
        try:
            return json.loads((self.install.data / "engine.lock").read_text())
        except (OSError, ValueError):
            return None

    def cleanup(self) -> None:
        lock = self.lock()
        if lock:
            for pid in session_pids(lock["pid"]):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


@pytest.fixture
def bg(tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_LANG", "en")
    box = Background(tmp_path)
    yield box
    box.cleanup()


def test_start_returns_at_once_and_the_supervisor_has_no_terminal(bg):
    port = free_port()
    started = time.monotonic()
    done = bg.hokma("start", "--port", str(port))
    assert done.returncode == 0, done.stderr
    assert f"port {port}" in done.stdout and time.monotonic() - started < 60
    lock = bg.lock()
    assert lock and lock["port"] == port and set(lock) == {"pid", "port", "started_at"}
    pid = lock["pid"]
    assert os.path.exists(f"/proc/{pid}") and health(port)
    assert os.getsid(pid) == pid                                           # its own session: no controlling terminal
    assert os.readlink(f"/proc/{pid}/fd/0") == os.devnull                  # stdin from /dev/null
    assert os.readlink(f"/proc/{pid}/fd/1").endswith("logs/supervisor.out")
    assert os.readlink(f"/proc/{pid}/fd/2").endswith("logs/supervisor.out")
    # the supervisor opened the browser itself, with the one-time address; the parent had nothing to pass on
    assert wait_until(bg.addresses, 20)
    url = bg.addresses()[0]
    assert url.startswith(f"http://127.0.0.1:{port}/#t=") and "&o=" in url        # secrets only to the numeric loopback
    token = url.split("#t=")[1].split("&o=")[0]
    assert token not in done.stdout + done.stderr
    for process in session_pids(pid):                                      # and it is in no argument or variable
        assert token.encode() not in (open(f"/proc/{process}/cmdline", "rb").read()
                                      + open(f"/proc/{process}/environ", "rb").read())
    assert token not in (bg.install.data / "engine.lock").read_text()
    assert health(port) is not None                                        # the parent is gone and Hokma stays


def test_a_second_start_opens_the_page_without_secrets_and_creates_nothing(bg):
    port = free_port()
    assert bg.hokma("start", "--port", str(port)).returncode == 0
    pid = bg.lock()["pid"]
    assert wait_until(bg.addresses, 20)
    before = sorted(session_pids(pid))
    again = bg.hokma("start")
    assert again.returncode == 0 and "already running" in again.stdout
    assert wait_until(lambda: len(bg.addresses()) == 2, 20)
    assert bg.addresses()[1] == f"http://hokma.localhost:{port}/"                # no #t=, no &o=
    assert bg.lock()["pid"] == pid and sorted(session_pids(pid)) == before
    forced = bg.hokma("start", "--foreground", "--no-browser", "--port", str(free_port()))
    assert forced.returncode == 0 and "already running" in forced.stderr   # even a foreground start makes no second one
    assert sorted(session_pids(pid)) == before


def test_a_second_start_refuses_to_open_the_browser_when_another_program_holds_the_address(bg):
    import http.server
    import threading

    port = free_port()
    assert bg.hokma("start", "--port", str(port)).returncode == 0
    pid = bg.lock()["pid"]
    assert wait_until(bg.addresses, 20)

    class Impostor(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"proof": "' + b"0" * 64 + b'"}')

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Impostor)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        lock = bg.lock()
        (bg.install.data / "engine.lock").write_text(json.dumps({**lock, "port": server.server_address[1]}))
        opened = len(bg.addresses())
        again = bg.hokma("start")
        assert again.returncode == 1 and "Another program is using the Hokma address" in again.stderr
        assert len(bg.addresses()) == opened                                       # the browser was not opened
    finally:
        server.shutdown()
        (bg.install.data / "engine.lock").write_text(json.dumps(lock))
    assert bg.lock()["pid"] == pid


def test_status_and_stop_on_the_control_channel_and_no_secret_in_any_message(bg, monkeypatch):
    from hokma.core import control

    port = free_port()
    assert bg.hokma("start", "--port", str(port)).returncode == 0
    assert wait_until(bg.addresses, 20)
    url = bg.addresses()[0]
    token, once = url.split("#t=")[1].split("&o=")
    monkeypatch.setenv("HOKMA_DATA_DIR", str(bg.install.data))
    sock_path = bg.install.data / "control.sock"
    assert oct(sock_path.stat().st_mode & 0o777) == "0o600" and sock_path.stat().st_uid == os.getuid()
    status = control.request("status")
    assert status["ok"] is True and status["port"] == port and status["pid"] == bg.lock()["pid"]
    assert status["engine"] == "up" and set(status) <= {"ok", "pid", "port", "started_at", "engine"}
    seen = [json.dumps(status)]
    for word in (b"token\n", b"dump\n", b"status extra\n", b"\x00\xff\n", b"x" * 500):
        with socket.socket(socket.AF_UNIX) as raw:
            raw.settimeout(5)
            raw.connect(str(sock_path))
            raw.sendall(word)
            seen.append(raw.recv(4096).decode())
    for reply in seen:
        assert token not in reply and once not in reply
    assert all(json.loads(r.split("\n")[0]).get("ok") in (True, False) for r in seen)
    assert health(port)                                                     # nothing above stopped it


def test_stop_closes_everything_and_leaves_the_database_whole(bg):
    port = free_port()
    assert bg.hokma("start", "--port", str(port)).returncode == 0
    pid = bg.lock()["pid"]
    assert len(session_pids(pid)) >= 2                                      # the supervisor and the engine
    began = time.monotonic()
    done = bg.hokma("stop")
    assert done.returncode == 0 and "closed" in done.stdout, done.stderr
    assert time.monotonic() - began < 9                                     # the engine ended by itself, not by force
    log = bg.install.data / "logs" / "engine.log"
    assert "did not end" not in (log.read_text() if log.exists() else "")
    assert wait_until(lambda: not session_pids(pid), 12), f"left behind: {session_pids(pid)}"
    assert health(port) is None
    with socket.socket() as probe:                                          # nothing that outlived "Stop" holds the port
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)         # as the server binds: closed connections may linger
        probe.bind(("127.0.0.1", port))
    assert bg.lock() is None and not (bg.install.data / "control.sock").exists()
    import sqlite3

    from hokma.db.paths import DB_FILE

    db = bg.install.data / DB_FILE
    assert db.exists()
    wal = db.with_name(db.name + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0                      # wal_checkpoint(TRUNCATE) ran
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    again = bg.hokma("stop")
    assert again.returncode == 0 and "not running" in again.stdout


def test_restart_makes_a_new_session_with_a_new_one_time_code(bg):
    port = free_port()
    assert bg.hokma("start", "--port", str(port)).returncode == 0
    first_pid = bg.lock()["pid"]
    assert wait_until(bg.addresses, 20)
    done = bg.hokma("restart", "--port", str(port))
    assert done.returncode == 0, done.stderr
    assert wait_until(lambda: len(bg.addresses()) == 2, 20)
    second_pid = bg.lock()["pid"]
    assert second_pid != first_pid and not session_pids(first_pid)
    tokens = [a.split("#t=")[1].split("&o=")[0] for a in bg.addresses()]
    codes = [a.split("&o=")[1] for a in bg.addresses()]
    assert tokens[0] != tokens[1] and codes[0] != codes[1]
    assert health(port) is not None


def test_a_supervisor_killed_by_force_takes_its_engine_with_it_within_5_seconds(bg):
    port = free_port()
    assert bg.hokma("start", "--port", str(port)).returncode == 0
    pid = bg.lock()["pid"]
    engine = find_engine(pid)
    assert engine and engine != pid
    os.kill(pid, signal.SIGKILL)                                            # no handler can run on SIGKILL
    began = time.monotonic()
    assert wait_until(lambda: engine not in session_pids(pid), 5), "the engine kept running"
    assert time.monotonic() - began <= 5.0
    assert health(port) is None
    start = bg.hokma("start", "--port", str(port))                          # the lock is stale: a new start replaces it
    assert start.returncode == 0 and bg.lock()["pid"] != pid


def test_a_lock_with_a_dead_pid_is_stale(bg):
    dead = subprocess.Popen(["true"])
    dead.wait()
    (bg.install.data / "engine.lock").write_text(json.dumps({"pid": dead.pid, "port": 8765, "started_at": "2020-01-01T00:00:00Z"}))
    port = free_port()
    assert bg.hokma("stop").stdout.strip() == "Hokma is not running."
    (bg.install.data / "engine.lock").write_text(json.dumps({"pid": dead.pid, "port": 8765, "started_at": "2020-01-01T00:00:00Z"}))
    assert bg.hokma("start", "--port", str(port)).returncode == 0
    assert bg.lock()["pid"] != dead.pid and bg.lock()["port"] == port


def test_the_first_port_in_use_moves_the_start_to_the_next_one_and_host_checks_the_real_port(bg):
    holders = []
    try:
        for number in (8765,):
            holder = socket.socket()
            holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                holder.bind(("127.0.0.1", number))
            except OSError:
                pytest.skip(f"port {number} is not free on this machine")
            holder.listen()
            holders.append(holder)
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", 8766))
        except OSError:
            pytest.skip("port 8766 is not free on this machine")
        finally:
            probe.close()
        assert bg.hokma("start").returncode == 0
        assert bg.lock()["port"] == 8766 and health(8766)
        ok = HTTPConnection("127.0.0.1", 8766, timeout=5)
        ok.request("GET", "/health", headers={"Host": "127.0.0.1:8766"})
        assert ok.getresponse().status == 200
        wrong = HTTPConnection("127.0.0.1", 8766, timeout=5)
        wrong.request("GET", "/health", headers={"Host": "127.0.0.1:8765"})
        assert wrong.getresponse().status == 403
    finally:
        for holder in holders:
            holder.close()


def test_foreground_start_keeps_working_with_the_options_tests_use(bg):
    port = free_port()
    proc = subprocess.Popen([str(bg.install.exe_a), "start", "--foreground", "--no-browser", "--port", str(port)],
                            env=bg.install.env(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                            start_new_session=True)
    try:
        line = proc.stdout.readline()
        assert "#t=" in line and wait_until(lambda: health(port), 40)
        assert bg.lock()["pid"] == proc.pid                                  # the foreground supervisor writes the lock too
        assert bg.hokma("stop").returncode == 0                              # and stops through the same channel
        assert proc.wait(timeout=15) == 0 and bg.lock() is None
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_a_busy_explicit_port_fails_with_code_1_and_leaves_no_lock(bg):
    port = free_port()
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", port))
        holder.listen()
        done = bg.hokma("start", "--port", str(port))
    assert done.returncode == 1 and bg.lock() is None

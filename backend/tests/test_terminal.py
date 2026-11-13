"""D8: the integrated terminal. Real shells on real pseudo-terminals, no mocks."""
import json
import os
import re
import signal
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fastapi import FastAPI

from hokma.terminal.api import router as terminal_router
from hokma.rpc.ws import SESSION_TOKEN
from hokma.terminal import session as term_session

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX pty tests; Windows uses pywinpty")

URL = f"/ws/terminal?token={SESSION_TOKEN}"


@pytest.fixture(autouse=True)
def watchdog():
    """A stuck read must fail the test, not hang the suite."""
    def boom(*_):
        raise TimeoutError("terminal test took longer than 40 s")
    previous = signal.signal(signal.SIGALRM, boom)
    signal.alarm(40)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, previous)


@pytest.fixture
def home(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("HOKMA_HOME", str(work))
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setenv("PS1", "")
    return work


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(terminal_router)       # only this delivery: other routers are tested by their owners
    with TestClient(app) as c:
        yield c


def read_until(ws, needle: str) -> str:
    """Reads until `needle` (a regex) shows up in the output. The typed line is echoed too, so use needles
    that only the result can match."""
    text = ""
    while not re.search(needle, text):
        frame = json.loads(ws.receive_text())
        if frame["type"] == "output":
            text += frame["data"]
        elif frame["type"] == "exit":
            raise AssertionError(f"shell exited before {needle!r}: {text!r}")
    return text


def wait_registry_empty(timeout: float = 5.0) -> bool:
    end = time.time() + timeout
    while term_session.registry.sessions and time.time() < end:
        time.sleep(0.05)
    return not term_session.registry.sessions


def type_line(ws, line: str) -> None:
    ws.send_text(json.dumps({"type": "input", "data": line + "\n"}))


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:                                   # a zombie is still "alive" for kill(0): it was not reaped
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except OSError:
        return True


def wait_dead(pid: int, timeout: float = 5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return not alive(pid)


def open_ready(client, url: str = URL):
    ctx = client.websocket_connect(url)
    ws = ctx.__enter__()
    ready = json.loads(ws.receive_text())
    assert ready["type"] == "ready"
    return ctx, ws, ready


def test_rejects_missing_and_wrong_token(client, home):
    for url in ("/ws/terminal", "/ws/terminal?token=wrong"):
        with pytest.raises(WebSocketDisconnect) as info:
            with client.websocket_connect(url):
                pass
        assert info.value.code == 4401
    assert wait_registry_empty()


def test_open_write_read_close_leaves_no_process(client, home):
    with client.websocket_connect(URL) as ws:
        ready = json.loads(ws.receive_text())
        assert ready["type"] == "ready" and ready["cwd"] == str(home)
        pid = ready["pid"]
        assert alive(pid)
        type_line(ws, "echo hello-$((40+2))")
        assert "hello-42" in read_until(ws, "hello-42")
    assert wait_dead(pid)
    assert wait_registry_empty()


def test_runs_in_working_folder_and_has_isolated_claude_config(client, home, tmp_path):
    with client.websocket_connect(URL) as ws:
        json.loads(ws.receive_text())
        type_line(ws, "pwd; echo cfg=$CLAUDE_CONFIG_DIR; echo term=$TERM; env | grep -c SESSION_TOKEN")
        out = read_until(ws, "term=xterm-256color\r?\n")
    assert str(home) in out
    assert f"cfg={tmp_path / 'data' / 'claude-config'}" in out


def test_token_is_not_in_the_shell_environment(client, home):
    with client.websocket_connect(URL) as ws:
        json.loads(ws.receive_text())
        type_line(ws, f"env | grep -c -- {SESSION_TOKEN}; echo done-$((1+1))")
        out = read_until(ws, "done-2")
    assert re.search(r"^0\s*$", out, re.M)


def test_resize_reaches_the_terminal(client, home):
    with client.websocket_connect(URL + "&cols=100&rows=30") as ws:
        json.loads(ws.receive_text())
        type_line(ws, "stty size; echo a")
        assert "30 100" in read_until(ws, "30 100")
        ws.send_text(json.dumps({"type": "resize", "cols": 120, "rows": 40}))
        type_line(ws, "stty size; echo b")
        assert "40 120" in read_until(ws, "40 120")


def test_utf8_survives_the_round_trip(client, home):
    with client.websocket_connect(URL) as ws:
        json.loads(ws.receive_text())
        type_line(ws, "printf 'ma\\303\\261ana-\\342\\202\\254\\n'")
        expected = "ma\u00f1ana-\u20ac"
        assert expected in read_until(ws, expected)


def test_project_folder_is_used_when_it_exists(client, home, tmp_path, monkeypatch):
    project = tmp_path / "project-a"
    project.mkdir()

    class Rec:
        path = str(project)

    class Fake:
        def get(self, _):
            return Rec()

    monkeypatch.setattr("hokma.engine.dbstore.DbProjects", Fake)
    with client.websocket_connect(URL + "&project_id=7") as ws:
        assert json.loads(ws.receive_text())["cwd"] == str(project)
    Rec.path = str(tmp_path / "gone")
    with client.websocket_connect(URL + "&project_id=7") as ws:
        assert json.loads(ws.receive_text())["cwd"] == str(home)


def test_two_terminals_are_independent_and_both_die(client, home):
    ctx1, ws1, r1 = open_ready(client)
    ctx2, ws2, r2 = open_ready(client)
    try:
        assert r1["pid"] != r2["pid"] and len(term_session.registry.sessions) == 2
        type_line(ws1, "MARK=one; echo m1=$MARK")
        type_line(ws2, "echo m2=${MARK:-empty}")
        read_until(ws1, "m1=one")
        assert "m2=empty" in read_until(ws2, "m2=empty")
    finally:
        ctx1.__exit__(None, None, None)
    assert wait_dead(r1["pid"])
    assert alive(r2["pid"])                 # closing one tab leaves the other shell alone
    ctx2.__exit__(None, None, None)
    assert wait_dead(r2["pid"])


def test_closing_kills_background_children_too(client, home):
    with client.websocket_connect(URL) as ws:
        json.loads(ws.receive_text())
        type_line(ws, "sleep 300 & echo child=$!")
        child = int(re.search(r"child=(\d+)", read_until(ws, r"child=\d+")).group(1))
        assert alive(child)
    assert wait_dead(child)


def test_exit_reports_code_and_closes(client, home):
    with client.websocket_connect(URL) as ws:
        pid = json.loads(ws.receive_text())["pid"]
        type_line(ws, "exit 3")
        frame = {}
        while frame.get("type") != "exit":
            frame = json.loads(ws.receive_text())
        assert frame["code"] == 3
    assert wait_dead(pid)


def test_ctrl_c_interrupts_a_running_command(client, home):
    with client.websocket_connect(URL) as ws:
        json.loads(ws.receive_text())
        type_line(ws, "sleep 300")
        time.sleep(0.3)
        ws.send_text(json.dumps({"type": "input", "data": "\x03"}))
        type_line(ws, "echo back-$((2+3))")
        assert "back-5" in read_until(ws, "back-5")


def test_junk_frames_do_not_break_the_session(client, home):
    with client.websocket_connect(URL) as ws:
        json.loads(ws.receive_text())
        for junk in ("not json", "[]", json.dumps({"type": "resize", "cols": "x", "rows": None}),
                     json.dumps({"type": "input", "data": 5}), json.dumps({"type": "nope"})):
            ws.send_text(junk)
        ws.send_text(json.dumps({"type": "ping"}))
        type_line(ws, "echo alive-$((1+1))")
        assert "alive-2" in read_until(ws, "alive-2")


def test_session_limit(client, home, monkeypatch):
    monkeypatch.setattr(term_session, "MAX_SESSIONS", 1)
    ctx, ws, ready = open_ready(client)
    try:
        with client.websocket_connect(URL) as second:
            assert json.loads(second.receive_text()) == {"type": "error", "code": "too_many_terminals"}
            with pytest.raises(WebSocketDisconnect) as info:
                second.receive_text()
            assert info.value.code == 4429
    finally:
        ctx.__exit__(None, None, None)
    assert wait_dead(ready["pid"])


def test_kill_all_now_stops_every_shell(client, home):
    ctx, ws, ready = open_ready(client)
    try:
        term_session.registry.kill_all_now()
        assert wait_dead(ready["pid"])
    finally:
        ctx.__exit__(None, None, None)


def test_e2e_hokma_doctor_output_in_the_terminal(client, home, monkeypatch):
    """The spec's E2E: type `hokma doctor` in the terminal and see its output."""
    bin_dir = Path(sys.executable).parent
    if not (bin_dir / "hokma").exists():
        pytest.skip("hokma command not installed in this environment")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    with client.websocket_connect(URL) as ws:
        json.loads(ws.receive_text())
        type_line(ws, "hokma doctor; echo finished-$((6*7))")
        out = read_until(ws, "finished-42")
    assert "CLAUDE_CONFIG_DIR" in out and "command not found" not in out


def test_route_is_part_of_the_app():
    source = (Path(__file__).parents[1] / "hokma" / "core" / "app.py").read_text()
    assert "terminal_router" in source and "include_router(terminal_router)" in source


def test_first_terminal_opens_in_the_active_project_saved_in_the_database(client, home, tmp_path):
    """No project_id in the request: the folder comes from profile.active_project_id at the moment of opening."""
    from hokma.db import db_path, get_database, migrate
    first, second = tmp_path / "proj-a", tmp_path / "proj-b"
    first.mkdir()
    second.mkdir()
    migrate(db_path())
    db = get_database()
    with db.write() as u:
        u.profile.save(owner_name="Ana")
        a = u.projects.create("Fund A", str(first))
        b = u.projects.create("Fund B", str(second))
        u.profile.set_active_project(b.id)
    with client.websocket_connect(URL) as ws:
        assert json.loads(ws.receive_text())["cwd"] == str(second)
    with db.write() as u:
        u.profile.set_active_project(a.id)
    with client.websocket_connect(URL) as ws:
        assert json.loads(ws.receive_text())["cwd"] == str(first)


def test_without_any_project_the_terminal_opens_in_the_working_folder(client, home):
    with client.websocket_connect(URL) as ws:
        assert json.loads(ws.receive_text())["cwd"] == str(home)

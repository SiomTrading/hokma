"""A-11 (task 3082): the real supervisor and the real engine, a terminal and a chat turn open, and the shutdown call.
Within 15 seconds no process of the session is left, the lock and the control socket are gone, the port can be taken, the
database is intact and the partial text of the turn was saved."""
import asyncio
import json
import os
import queue
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from supervisor_support import Install, free_port, health, session_pids, wait_until

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process model")
FAKE_CLAUDE = Path(__file__).resolve().parent / "engine_fixtures" / "fake_claude.py"


def _descendants(root: int) -> dict[int, str]:
    """Every live process below `root` (pid -> command line), found through the parent links. The terminal shell and the
    fake claude run in sessions of their own, so the session of `hokma start` does not list them."""
    parents: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                parents[int(entry.name)] = int((entry / "stat").read_text().rsplit(") ", 1)[1].split()[1])
            except (OSError, ValueError, IndexError):
                continue
    found: dict[int, str] = {}
    frontier = [root]
    while frontier:
        parent = frontier.pop()
        for pid, ppid in parents.items():
            if ppid == parent and pid not in found:
                try:
                    found[pid] = (Path(f"/proc/{pid}") / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
                except OSError:
                    found[pid] = ""
                frontier.append(pid)
    return found


def _post(port: int, token: str | None, force: bool = False) -> int:
    headers = {"Sec-Fetch-Site": "same-origin", "Origin": f"http://127.0.0.1:{port}"}
    if token:
        headers["X-Hokma-Token"] = token
    else:                                                            # the entry screen asks for a nonce first
        ask = urllib.request.Request(f"http://127.0.0.1:{port}/api/system/shutdown-nonce", headers={"Sec-Fetch-Site": "same-origin"})
        with urllib.request.urlopen(ask, timeout=20) as r:
            headers["X-Hokma-Shutdown-Nonce"] = json.loads(r.read())["nonce"]
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/system/shutdown{'?force=true' if force else ''}", method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status
    except urllib.error.HTTPError as error:
        return error.code


TERMINAL_OUTPUT: list[str] = []


async def _open_work(port: int, token: str, keep: list) -> str:
    """A terminal with a running command and a chat turn that is still streaming; returns the text received so far.
    The connections go to `keep`: they must outlive this call, or the server shuts the terminal down on its own."""
    import websockets

    terminal = await websockets.connect(f"ws://127.0.0.1:{port}/ws/terminal?token={token}")
    keep.append(terminal)
    await asyncio.sleep(1.0)                                         # the shell must be reading before it gets the command
    await terminal.send(json.dumps({"type": "input", "data": "exec sleep 600\n"}))
    try:                                                             # what the terminal said, for the failure message
        while len("".join(TERMINAL_OUTPUT)) < 400:
            TERMINAL_OUTPUT.append(str(await asyncio.wait_for(terminal.recv(), 1.5)))
    except (asyncio.TimeoutError, Exception):
        pass
    chat = await websockets.connect(f"ws://127.0.0.1:{port}/ws/chat?token={token}")
    keep.append(chat)
    await chat.send(json.dumps({"type": "send", "text": "[slow] keep talking"}))
    seen = ""
    while len(seen) < 20:
        event = json.loads(await asyncio.wait_for(chat.recv(), 30))
        assert event["type"] != "error", event
        if event["type"] == "delta":
            seen += event["text"]
    return seen


def _hold_work(port: int, token: str, release: threading.Event, out: queue.Queue) -> None:
    """Runs in its own thread with its own loop, which lives until `release`: closing the loop earlier drops both sockets
    and the server stops the terminal shell, so the test sometimes missed processes it expects (bug 1191)."""
    async def main() -> None:
        keep: list = []
        try:
            out.put(await _open_work(port, token, keep))
        except BaseException as exc:                                 # noqa: BLE001  handed to the test thread
            out.put(exc)
            return
        while not release.is_set():
            await asyncio.sleep(0.1)
        for connection in keep:
            try:
                await connection.close()
            except Exception:                                        # noqa: BLE001  the shutdown may have closed it already
                pass

    asyncio.run(main())


def test_a11_the_shutdown_leaves_nothing_behind_and_saves_the_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_LANG", "en")
    ins, port = Install(tmp_path), free_port()
    shim = tmp_path / "fakebin"
    shim.mkdir()
    (shim / "claude").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CLAUDE}" "$@"\n')
    (shim / "claude").chmod(0o755)
    release, out = threading.Event(), queue.Queue()
    worker = None
    proc = subprocess.Popen([str(ins.exe_a), "start", "--port", str(port), "--no-browser"],
                            env=ins.env(PATH=f"{shim}:{os.environ['PATH']}"), stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    try:
        line = proc.stdout.readline().strip()
        token = line.split("#t=")[1].split("&o=")[0]
        assert wait_until(lambda: health(port), 40), "the engine never answered /health"
        worker = threading.Thread(target=_hold_work, args=(port, token, release, out), daemon=True)
        worker.start()
        seen = out.get(timeout=90)
        if isinstance(seen, BaseException):
            raise seen
        assert seen
        below = wait_until(lambda: (lambda d: d if any("fake_claude" in c for c in d.values()) and len(d) >= 3 else None)(
            _descendants(proc.pid)), 20)
        assert below, f"the fake claude and the terminal shell should be running: {_descendants(proc.pid)!r} {TERMINAL_OUTPUT!r}"
        tracked = list(below)
        assert _post(port, None) == 409                              # a turn and sockets are open: no entry-screen form
        assert _post(port, token) == 409                             # the panel session is refused too while an agent works...
        assert _post(port, token, force=True) == 202                 # ...until the owner accepts to cut it (0.0.36)
        assert wait_until(lambda: not session_pids(proc.pid), 15), f"left behind: {session_pids(proc.pid)}"
        assert health(port) is None
        assert wait_until(lambda: not [p for p in tracked if Path(f"/proc/{p}").exists()
                                       and Path(f"/proc/{p}/stat").read_text().rsplit(") ", 1)[1][0] != "Z"], 5), \
            "a process of the engine, the turn or the terminal survived"
        assert not (ins.data / "engine.lock").exists() and not (ins.data / "control.sock").exists()
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # as the server does: closed sockets in TIME_WAIT are fine
            s.bind(("127.0.0.1", port))                              # the port can be taken again
        databases = list(ins.data.glob("*.db")) + list(ins.data.glob("*.sqlite*"))
        assert databases, list(ins.data.iterdir())
        conn = sqlite3.connect(str(databases[0]))
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            saved = [r[0] for r in conn.execute("SELECT content FROM messages WHERE role = 'assistant'")]
        finally:
            conn.close()
        assert saved and saved[0].startswith(seen[:10]), (saved, seen)
    finally:
        release.set()
        if worker is not None:
            worker.join(15)
        for pid in session_pids(proc.pid):
            os.kill(pid, signal.SIGKILL)
        proc.wait()

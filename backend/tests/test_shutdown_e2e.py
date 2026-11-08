"""A-11 (task 3082): the real supervisor and the real engine, a terminal and a chat turn open, and the shutdown call.
Within 15 seconds no process of the session is left, the lock and the control socket are gone, the port can be taken, the
database is intact and the partial text of the turn was saved."""
import asyncio
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
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


def _post(port: int, token: str | None) -> int:
    headers = {"Sec-Fetch-Site": "same-origin", "Origin": f"http://127.0.0.1:{port}"}
    if token:
        headers["X-Hokma-Token"] = token
    else:                                                            # the entry screen asks for a nonce first
        ask = urllib.request.Request(f"http://127.0.0.1:{port}/api/system/shutdown-nonce", headers={"Sec-Fetch-Site": "same-origin"})
        with urllib.request.urlopen(ask, timeout=20) as r:
            headers["X-Hokma-Shutdown-Nonce"] = json.loads(r.read())["nonce"]
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/system/shutdown", method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status
    except urllib.error.HTTPError as error:
        return error.code


TERMINAL_OUTPUT: list[str] = []


async def _open_work(port: int, token: str) -> str:
    """A terminal with a running command and a chat turn that is still streaming; returns the text received so far."""
    import websockets

    terminal = await websockets.connect(f"ws://127.0.0.1:{port}/ws/terminal?token={token}")
    await asyncio.sleep(1.0)                                         # the shell must be reading before it gets the command
    await terminal.send(json.dumps({"type": "input", "data": "exec sleep 600\n"}))
    try:                                                             # what the terminal said, for the failure message
        while len("".join(TERMINAL_OUTPUT)) < 400:
            TERMINAL_OUTPUT.append(str(await asyncio.wait_for(terminal.recv(), 1.5)))
    except (asyncio.TimeoutError, Exception):
        pass
    chat = await websockets.connect(f"ws://127.0.0.1:{port}/ws/chat?token={token}")
    await chat.send(json.dumps({"type": "send", "text": "[slow] keep talking"}))
    seen = ""
    while len(seen) < 20:
        event = json.loads(await asyncio.wait_for(chat.recv(), 30))
        assert event["type"] != "error", event
        if event["type"] == "delta":
            seen += event["text"]
    return seen


def test_a11_the_shutdown_leaves_nothing_behind_and_saves_the_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_LANG", "en")
    ins, port = Install(tmp_path), free_port()
    shim = tmp_path / "fakebin"
    shim.mkdir()
    (shim / "claude").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CLAUDE}" "$@"\n')
    (shim / "claude").chmod(0o755)
    proc = subprocess.Popen([str(ins.exe_a), "start", "--port", str(port), "--no-browser"],
                            env=ins.env(PATH=f"{shim}:{os.environ['PATH']}"), stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    try:
        line = proc.stdout.readline().strip()
        token = line.split("#t=")[1].split("&o=")[0]
        assert wait_until(lambda: health(port), 40), "the engine never answered /health"
        seen = asyncio.run(_open_work(port, token))
        assert seen
        below = wait_until(lambda: (lambda d: d if any("fake_claude" in c for c in d.values()) and len(d) >= 3 else None)(
            _descendants(proc.pid)), 20)
        assert below, f"the fake claude and the terminal shell should be running: {_descendants(proc.pid)!r} {TERMINAL_OUTPUT!r}"
        tracked = list(below)
        assert _post(port, None) == 409                              # a turn and sockets are open: no entry-screen form
        assert _post(port, token) == 202
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
        for pid in session_pids(proc.pid):
            os.kill(pid, signal.SIGKILL)
        proc.wait()

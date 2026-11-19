"""Bug 1205: on macOS `ps -axo sess=` is 0 for every process; closing a terminal must only touch its own session."""
import os
import signal
import subprocess
from types import SimpleNamespace

from hokma.core import proctree
from hokma.terminal import pty_backend
from hokma.terminal.pty_backend import PosixPty


def _fake_pty(pid: int) -> PosixPty:
    pty = PosixPty.__new__(PosixPty)
    pty.pid = pid
    return pty


def test_ps_sess_zero_everywhere_signals_nobody_foreign(monkeypatch):
    shell = 4242
    ps_out = "\n".join(f"{p}" for p in (1, os.getpid(), os.getppid(), shell, 5001, 5002, 5003))
    monkeypatch.setattr(pty_backend.os.path, "isdir", lambda p: False)
    monkeypatch.setattr(pty_backend.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(stdout=ps_out))
    sessions = {5002: shell}                    # only this one is really in the shell's session
    monkeypatch.setattr(pty_backend.os, "getsid", lambda p: sessions.get(p, 999))
    sent = []
    monkeypatch.setattr(pty_backend.os, "kill", lambda p, s: sent.append((p, s)))
    monkeypatch.setattr(pty_backend.os, "killpg", lambda p, s: sent.append(("pg", p)))
    pty = _fake_pty(shell)
    assert pty._session_members() == [5002]
    pty._signal_group(signal.SIGKILL)
    assert (5001, signal.SIGKILL) not in sent and (5003, signal.SIGKILL) not in sent
    assert sent == [("pg", shell), (5002, signal.SIGKILL)]


def test_signal_group_revalidates_before_kill(monkeypatch):
    pty = _fake_pty(4242)
    monkeypatch.setattr(pty, "_session_members", lambda: [5001])
    monkeypatch.setattr(pty_backend.os, "getsid", lambda p: 7)     # pid was reused by another session
    sent = []
    monkeypatch.setattr(pty_backend.os, "kill", lambda p, s: sent.append(p))
    monkeypatch.setattr(pty_backend.os, "killpg", lambda p, s: None)
    pty._signal_group(signal.SIGHUP)
    assert sent == []


def test_protected_pids(monkeypatch):
    assert proctree.is_protected_pid(0) and proctree.is_protected_pid(1)
    assert proctree.is_protected_pid(os.getpid()) and proctree.is_protected_pid(os.getppid())
    assert not proctree.is_protected_pid(os.getpid() + 100000)


def test_proctree_never_signals_protected(monkeypatch):
    monkeypatch.setattr(proctree, "_children_map", lambda: {5000: [1, os.getpid(), os.getppid(), 5001]})
    assert proctree.descendants(5000) == [5001]
    assert proctree.descendants(1) == []
    tree = proctree.ProcessTree(5000)
    tree._known = {1: "x", os.getpid(): "x", 5001: "x"}
    monkeypatch.setattr(proctree, "start_time", lambda p: "x")
    sent = []
    monkeypatch.setattr(proctree.os, "kill", lambda p, s: sent.append(p))
    monkeypatch.setattr(proctree.os, "killpg", lambda p, s: None)
    tree._signal_all(signal.SIGTERM)
    assert sent == [5001]

"""Supervisor rules with a fake engine process: restart on 75, backoff 1/2/4 s, stop on the fourth crash in 60 s,
the same token on every launch, hand over to the current slot. No real process is started."""
import io
import signal
import struct
import subprocess
import sys

import pytest

from dbsupport import env  # noqa: F401  (fixture)
from hokma.core import session
from hokma.core.engine_main import read_channel_key, read_owner_hash, read_owner_secret_hash, read_token, watch_parent
from hokma.core.supervisor import (BACKOFF_SECONDS, EXIT_RESTART, MAX_CRASHES, EngineLog, Supervisor, engine_argv,
                                   hand_over)
from hokma.core.winjob import (BasicLimitInformation, ExtendedLimitInformation, JOB_OBJECT_LIMIT_BREAKAWAY_OK,
                               JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, kill_on_close_info)
from hokma.i18n import t

TOKEN = "T" * 32
KEY = "c" * 64


class FakeStdin(io.BytesIO):
    def close(self) -> None:                 # keep the bytes readable after the supervisor closes it
        self.flush()


class FakeProc:
    def __init__(self, code: int, stderr: bytes = b"") -> None:
        self.code, self.stdin, self.stderr = code, FakeStdin(), io.BytesIO(stderr)
        self.terminated = False

    def wait(self, timeout=None) -> int:
        return self.code

    def poll(self):
        return self.code

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        pass


class Harness:
    def __init__(self, tmp_path, codes, *, now=None, needs_handover=lambda: False, handover=lambda p, t, o, s: False,
                 stderr=b"", owner_secret_hash=""):
        """`stderr` is what every engine writes, or a list with what the first, the second... engine writes."""
        self.codes, self.launched, self.sleeps, self.engine_err = list(codes), [], [], stderr
        self.clock = now or (lambda: 0.0)
        self.stderr = io.StringIO()
        self.log = EngineLog(tmp_path / "logs" / "engine.log", (TOKEN,))
        self.sup = Supervisor(port=1234, token=TOKEN, argv=lambda: ["engine"], log=self.log, stderr=self.stderr,
                              popen=self.popen, clock=self.clock, sleep=self.sleeps.append,
                              needs_handover=needs_handover, handover=handover, owner_secret_hash=owner_secret_hash)

    def popen(self, argv, **kwargs):
        err = self.engine_err[len(self.launched)] if isinstance(self.engine_err, list) else self.engine_err
        proc = FakeProc(self.codes.pop(0), b"Traceback boom " + TOKEN.encode() + b"\n" + err)
        self.launched.append(proc)
        return proc


def test_restart_code_relaunches_at_once_with_the_same_token(tmp_path, monkeypatch):
    monkeypatch.setattr("hokma.core.supervisor.new_channel_key", lambda: KEY)
    h = Harness(tmp_path, [EXIT_RESTART, EXIT_RESTART, 0])
    assert h.sup.run() == 0
    assert len(h.launched) == 3 and h.sleeps == []                       # no waiting, no crash counted
    assert {p.stdin.getvalue() for p in h.launched} == {payload(TOKEN, key=KEY)}          # token, no owner hash yet
    assert t("supervisor.restarting") in h.stderr.getvalue()


def test_crashes_wait_1_2_4_seconds_and_the_fourth_stops(tmp_path):
    h = Harness(tmp_path, [1, 1, 1, 1, 0])
    assert h.sup.run() == 1
    assert len(h.launched) == MAX_CRASHES and h.sleeps == list(BACKOFF_SECONDS)
    log = h.log.path.read_text()
    assert log.count("engine crashed") == 4 and f"stopping: {MAX_CRASHES} crashes in 60 s" in log
    assert "Traceback boom" in log and TOKEN not in log                  # the reason, never the token
    assert t("supervisor.gave_up", path=h.log.path) in h.stderr.getvalue()


def test_crashes_more_than_a_minute_apart_do_not_add_up(tmp_path):
    clock = iter([0.0, 30.0, 61.0, 100.0, 130.0, 170.0])
    h = Harness(tmp_path, [1, 1, 1, 1, 1, 1, 0], now=lambda: next(clock))
    assert h.sup.run() == 0                                               # never 4 inside one window
    assert len(h.launched) == 7


def test_integrity_and_migration_failures_are_not_retried(tmp_path):
    for code in (3, 4):
        h = Harness(tmp_path, [code, 0])
        assert h.sup.run() == code and len(h.launched) == 1 and h.sleeps == []


def test_a_signal_from_the_terminal_is_a_stop_not_a_crash(tmp_path):
    h = Harness(tmp_path, [-signal.SIGINT, 0])
    assert h.sup.run() == 0 and len(h.launched) == 1


def test_it_hands_over_before_launching_when_the_current_slot_changed(tmp_path):
    calls = []
    h = Harness(tmp_path, [0], needs_handover=lambda: True,
                handover=lambda port, token, owner, once: calls.append((port, token, owner, once)) or True)
    assert h.sup.run() == 0 and h.sup.handed_over
    assert calls == [(1234, TOKEN, "", "")] and h.launched == []                  # the new supervisor launches the engine


def test_when_the_hand_over_is_not_possible_it_keeps_running(tmp_path):
    h = Harness(tmp_path, [0], needs_handover=lambda: True, handover=lambda port, token, owner, once: False)
    assert h.sup.run() == 0 and len(h.launched) == 1 and not h.sup.handed_over


def test_engine_command_points_at_the_current_slot(env):  # noqa: F811
    data = env / "data"
    (data / "runtime" / "b" / "venv" / "bin").mkdir(parents=True)
    (data / "current").write_text("b\n")
    argv = engine_argv(8765)
    assert argv[0].endswith("runtime/b/venv/bin/hokma") and argv[1:] == ["engine", "--port", "8765"]
    (data / "current").unlink()                                           # a dev checkout: no slots
    assert engine_argv(8765)[:3] == [sys.executable, "-m", "hokma_cli.main"]


def test_hand_over_without_slots_does_nothing(env):  # noqa: F811
    assert hand_over(8765, TOKEN) is False


# ---- the owner confirmation hash (bug 1035) ---------------------------------------------------------------

DIGEST = "ab" * 32
OTHER = "cd" * 32
SECRET_HASH = "ef" * 32


def payload(token, owner="", once="", key=""):
    return "\n".join((token, owner, once, key)).encode() + b"\n"


def note(digest, key=KEY):
    return session.owner_note(digest, key).encode() + b"\n"


@pytest.fixture
def fixed_key(monkeypatch):
    monkeypatch.setattr("hokma.core.supervisor.new_channel_key", lambda: KEY)


def test_the_supervisor_keeps_the_owner_hash_and_gives_it_to_the_next_engine(tmp_path, fixed_key):
    h = Harness(tmp_path, [EXIT_RESTART, 0], stderr=[note(DIGEST), b""], owner_secret_hash=SECRET_HASH)
    assert h.sup.run() == 0
    assert h.launched[1].stdin.getvalue() == payload(TOKEN, DIGEST, "", KEY)            # registered: hash kept, secret gone
    assert DIGEST not in h.stderr.getvalue() and "HOKMA_OWNER_HASH" not in h.stderr.getvalue()   # not on the terminal


def test_3045_the_secret_hash_waits_for_its_registration_across_restarts(tmp_path, fixed_key):
    h = Harness(tmp_path, [EXIT_RESTART, 0], owner_secret_hash=SECRET_HASH)            # nobody registered
    assert h.sup.run() == 0
    assert {p.stdin.getvalue() for p in h.launched} == {payload(TOKEN, "", SECRET_HASH, KEY)}


def test_3045_every_engine_gets_its_own_channel_key(tmp_path):
    h = Harness(tmp_path, [EXIT_RESTART, 0])
    assert h.sup.run() == 0
    keys = [p.stdin.getvalue().split(b"\n")[3] for p in h.launched]
    assert len(set(keys)) == 2 and all(session.CHANNEL_KEY_PATTERN.fullmatch(k.decode()) for k in keys)


@pytest.mark.parametrize("forged", [
    f"HOKMA_OWNER_HASH {DIGEST}\n",                                       # the old unsigned note
    f"HOKMA_OWNER_HASH {DIGEST} {'0' * 64}\n",                            # a signature that is not the engine's
    f"HOKMA_OWNER_HASH {DIGEST} not-a-signature\n", f"HOKMA_OWNER_HASH {DIGEST}\n".replace(DIGEST, "zz"),
])
def test_3045_a_note_that_is_not_signed_by_the_engine_is_dropped(tmp_path, fixed_key, forged):
    h = Harness(tmp_path, [EXIT_RESTART, 0], stderr=forged.encode())
    assert h.sup.run() == 0 and h.sup.owner_hash == ""
    assert h.launched[1].stdin.getvalue() == payload(TOKEN, "", "", KEY)
    assert "HOKMA_OWNER_HASH" not in h.stderr.getvalue()                    # never shown, never taken


def test_3045_a_note_signed_with_another_key_is_dropped(tmp_path, fixed_key):
    h = Harness(tmp_path, [0], stderr=note(DIGEST, key="9" * 64))
    assert h.sup.run() == 0 and h.sup.owner_hash == ""


def test_3045_only_the_first_signed_note_of_an_engine_counts(tmp_path, fixed_key):
    h = Harness(tmp_path, [0], stderr=note(DIGEST) + note(OTHER))
    assert h.sup.run() == 0 and h.sup.owner_hash == DIGEST


def test_3045_a_later_engine_can_never_overwrite_the_hash(tmp_path, fixed_key):
    h = Harness(tmp_path, [EXIT_RESTART, EXIT_RESTART, 0], stderr=[note(DIGEST), note(OTHER), b""])
    assert h.sup.run() == 0 and h.sup.owner_hash == DIGEST
    assert h.launched[2].stdin.getvalue() == payload(TOKEN, DIGEST, "", KEY)


def test_3045_a_hash_given_at_start_is_not_replaced_by_a_note(tmp_path, fixed_key):
    h = Harness(tmp_path, [0], stderr=note(OTHER))
    h.sup.owner_hash = DIGEST
    assert h.sup.run() == 0 and h.sup.owner_hash == DIGEST


def test_3045_a_partial_line_before_the_note_does_not_hide_it_nor_forge_it(tmp_path, fixed_key):
    h = Harness(tmp_path, [0], stderr=b"half a line " + note(DIGEST))
    assert h.sup.run() == 0 and h.sup.owner_hash == DIGEST and "half a line" not in h.stderr.getvalue()


def test_a_malformed_owner_note_is_ignored(tmp_path):
    h = Harness(tmp_path, [EXIT_RESTART, 0], stderr=b"HOKMA_OWNER_HASH not-a-hash\n")
    assert h.sup.run() == 0 and h.launched[1].stdin.getvalue().startswith(TOKEN.encode() + b"\n\n\n")


def test_the_hand_over_carries_the_owner_hash_and_the_waiting_secret(tmp_path):
    calls = []
    sup = Supervisor(port=1, token=TOKEN, owner_hash=DIGEST, owner_secret_hash=SECRET_HASH, needs_handover=lambda: True,
                     handover=lambda port, token, owner, once: calls.append((owner, once)) or True,
                     log=EngineLog(tmp_path / "e.log", (TOKEN,)), stderr=io.StringIO())
    assert sup.run() == 0 and calls == [(DIGEST, SECRET_HASH)]


def test_the_engine_reads_the_owner_hash_line_and_refuses_a_wrong_one():
    assert read_owner_hash(io.StringIO(DIGEST + "\n")) == DIGEST
    assert read_owner_hash(io.StringIO("\n")) == "" and read_owner_hash(io.StringIO("")) == ""
    for bad in ("short\n", "Z" * 64 + "\n", DIGEST + "00\n"):
        with pytest.raises(ValueError):
            read_owner_hash(io.StringIO(bad))


def test_the_engine_reads_the_secret_hash_and_the_channel_key_lines():
    assert read_owner_secret_hash(io.StringIO(SECRET_HASH + "\n")) == SECRET_HASH and read_owner_secret_hash(io.StringIO("")) == ""
    assert read_channel_key(io.StringIO(KEY + "\n")) == KEY and read_channel_key(io.StringIO("\n")) == ""
    for bad in ("short\n", "Z" * 64 + "\n", SECRET_HASH + "00\n"):
        with pytest.raises(ValueError):
            read_owner_secret_hash(io.StringIO(bad))
        with pytest.raises(ValueError):
            read_channel_key(io.StringIO(bad))


def test_an_engine_started_with_the_hash_accepts_the_same_tab_without_registering_again():
    code = ("import hashlib; from hokma.core import session\n"
            "n = '0123456789abcdef0123456789abcdef'\n"
            "session.adopt('A'*32, hashlib.sha256(n.encode()).hexdigest())\n"
            "assert session.owner_matches(n) and not session.owner_matches('f'*32)\n"
            "assert session.register_owner(n) == 'registered' and session.register_owner('f'*32) == 'taken'\n"
            "print('ok')\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.stdout.strip() == "ok", out.stderr


def test_registering_tells_the_supervisor_on_standard_error_only_when_supervised_and_signed():
    code = ("from hokma.core import session\n"
            "once = 'S' * 22\n"
            "session.adopt('A'*32, '', session.secret_hash(once), 'c' * 64)\n"
            "assert session.register_owner('0123456789abcdef0123456789abcdef', once) == 'registered'\n"
            "assert session.register_owner('0123456789abcdef0123456789abcdef', once) == 'registered'\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    lines = out.stderr.strip().splitlines()
    assert out.returncode == 0 and len(lines) == 1 and out.stdout == ""            # once: the reload says nothing more
    digest = session.parse_owner_note(lines[0], "c" * 64)
    assert digest and len(digest) == 64 and session.parse_owner_note(lines[0], "d" * 64) is None
    assert "0123456789abcdef" not in out.stderr and "SSSSSSSS" not in out.stderr   # the hash, never the values
    silent = ("from hokma.core import session\n"
              "session.adopt('A'*32, '', session.secret_hash('S' * 22))\n"
              "assert session.register_owner('0123456789abcdef0123456789abcdef', 'S' * 22) == 'registered'\n")
    out = subprocess.run([sys.executable, "-c", silent], capture_output=True, text=True)
    assert out.returncode == 0 and out.stderr == ""


def test_3045_the_one_time_secret_only_registers_once_in_the_engine():
    code = ("from hokma.core import session\n"
            "once = 'S' * 22\n"
            "session.adopt('A'*32, '', session.secret_hash(once), 'c' * 64)\n"
            "assert session.register_owner('f' * 32, 'W' * 22) == 'secret_needed'\n"
            "assert session.register_owner('f' * 32) == 'secret_needed'\n"
            "assert session.register_owner('0' * 32, once) == 'registered'\n"
            "assert session.register_owner('f' * 32, once) == 'taken'\n"
            "print('ok')\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.stdout.strip() == "ok", out.stderr


# ---- engine side --------------------------------------------------------------------------------------------

def test_engine_reads_a_valid_token_and_refuses_anything_else():
    assert read_token(io.StringIO(TOKEN + "\n")) == TOKEN
    for bad in ("", "short\n", "has space in it 1234567890\n", "x" * 600 + "\n"):
        with pytest.raises(ValueError):
            read_token(io.StringIO(bad))


def test_engine_calls_back_when_the_supervisor_end_closes():
    gone = []
    stream = io.StringIO("")                                               # EOF at once: the supervisor is gone
    watch_parent(stream, lambda: gone.append(True)).join(2)
    assert gone == [True]


def test_token_can_only_be_fixed_once_and_before_the_app_loads():
    code = ("import sys; from hokma.core import session\n"
            "session.adopt('A'*32)\n"
            "import hokma.rpc.ws as ws; assert ws.SESSION_TOKEN == 'A'*32 and session.supervised()\n"
            "try: session.adopt('B'*32)\n"
            "except RuntimeError: print('refused')\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.strip() == "refused", out.stderr
    late = ("from hokma.core import session; import hokma.rpc.ws\n"
            "try: session.adopt('A'*32)\n"
            "except RuntimeError: print('refused')\n")
    assert subprocess.run([sys.executable, "-c", late], capture_output=True, text=True).stdout.strip() == "refused"
    with pytest.raises(ValueError):
        session.adopt("no good")


# ---- Windows job object -------------------------------------------------------------------------------------

def test_job_object_structures_have_the_windows_layout():
    if struct.calcsize("P") != 8:
        pytest.skip("layout is checked for 64-bit")
    assert BasicLimitInformation.LimitFlags.offset == 16
    assert ExtendedLimitInformation.BasicLimitInformation.offset == 0
    assert ExtendedLimitInformation.IoInfo.offset == 64
    assert ExtendedLimitInformation.ProcessMemoryLimit.offset == 112
    import ctypes
    assert ctypes.sizeof(ExtendedLimitInformation) == 144                  # JOBOBJECT_EXTENDED_LIMIT_INFORMATION, x64
    assert JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE == 0x2000 and JOB_OBJECT_LIMIT_BREAKAWAY_OK == 0x800
    # the job kills everything on close, and lets only a child that asks for it (the detached updater) leave
    assert kill_on_close_info().BasicLimitInformation.LimitFlags == 0x2000 | 0x800


@pytest.mark.skipif(sys.platform != "win32", reason="job objects exist only on Windows")
def test_job_object_kills_the_child_when_its_owner_lets_go():
    from hokma.core.winjob import KillOnCloseJob

    job = KillOnCloseJob()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    job.assign(child._handle)
    assert child.poll() is None
    job.close()                                                           # what happens when the supervisor ends
    assert child.wait(timeout=5) is not None


@pytest.mark.skipif(sys.platform != "win32", reason="job objects exist only on Windows")
def test_windows_hand_over_starts_the_next_supervisor_with_the_token_on_stdin(env):  # noqa: F811
    data = env / "data"
    exe = data / "runtime" / "b" / "venv" / "Scripts" / "hokma.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    (data / "current").write_text("b\n")
    started = []

    class Spy:
        stdin = FakeStdin()

        def __init__(self, argv, **kwargs):
            started.append((argv, kwargs))

    import hokma.core.supervisor as sup
    real = sup.subprocess.Popen
    sup.subprocess.Popen = Spy
    try:
        assert hand_over(8765, TOKEN) is True
    finally:
        sup.subprocess.Popen = real
    argv, _ = started[0]
    assert TOKEN not in " ".join(argv) and "--handoff-stdin" in argv and Spy.stdin.getvalue() == TOKEN.encode() + b"\n\n"


def test_a_busy_port_stops_at_once_with_a_clear_message(env, capsys):  # noqa: F811
    import socket

    from hokma.core.supervisor import port_in_use, run_supervisor

    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = holder.getsockname()[1]
        assert port_in_use(port) is True
        assert run_supervisor(port=port) == 1
    assert t("supervisor.port_busy", port=port) in capsys.readouterr().err
    assert port_in_use(port) is False


def test_3045_the_hand_over_pipe_round_trips_through_the_cli_reader():
    import argparse
    import os

    from hokma_cli.main import _handoff_token
    from hokma.core.supervisor import handoff_payload

    read_end, write_end = os.pipe()
    os.write(write_end, handoff_payload(TOKEN, DIGEST, SECRET_HASH))
    os.close(write_end)
    args = argparse.Namespace(handoff_fd=read_end, handoff_stdin=False)
    assert _handoff_token(args) == (TOKEN, DIGEST, SECRET_HASH)
    read_end, write_end = os.pipe()
    os.write(write_end, handoff_payload(TOKEN, "", ""))
    os.close(write_end)
    assert _handoff_token(argparse.Namespace(handoff_fd=read_end, handoff_stdin=False)) == (TOKEN, "", "")
    read_end, write_end = os.pipe()
    os.write(write_end, handoff_payload(TOKEN, "bad", ""))
    os.close(write_end)
    assert _handoff_token(argparse.Namespace(handoff_fd=read_end, handoff_stdin=False)) == ("", "", "")

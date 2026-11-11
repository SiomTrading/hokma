"""Task 3175: every command Hokma runs for an engine has a time limit, live output, no way to wait for typing, and a
Stop that ends it with everything it started."""
import asyncio
import os
import sys
import threading
import time

import pytest

from hokma.core import runcmd
from hokma.core.config import command_timeout
from hokma.engines import envfilter
from hokma.engines.direct import tools

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="bash commands")


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(")")[-1].split()[0] != "Z"
    except FileNotFoundError:
        return False
    except OSError:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True


def _eventually_dead(pid: int, seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def test_the_environment_never_waits_for_typing_and_carries_the_limit(monkeypatch):
    monkeypatch.setenv("HOKMA_COMMAND_TIMEOUT", "90")
    env = envfilter.filtered_env({"PATH": os.environ.get("PATH", "")})
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GCM_INTERACTIVE"] == "never"
    assert env["PIP_NO_INPUT"] == "1" and env["GH_PROMPT_DISABLED"] == "1"
    assert env["BASH_DEFAULT_TIMEOUT_MS"] == env["BASH_MAX_TIMEOUT_MS"] == "90000"


def test_the_time_limit_is_configurable_and_kept_in_range(monkeypatch, tmp_path):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("HOKMA_COMMAND_TIMEOUT", raising=False)
    assert command_timeout() == 600
    (tmp_path / "command-timeout").write_text("120\n")
    assert command_timeout() == 120
    monkeypatch.setenv("HOKMA_COMMAND_TIMEOUT", "5")
    assert command_timeout() == 30
    monkeypatch.setenv("HOKMA_COMMAND_TIMEOUT", "nonsense")
    assert command_timeout() == 600


@posix_only
def test_output_arrives_while_the_command_runs(tmp_path):
    seen: list[tuple[float, str]] = []
    start = time.monotonic()
    done = runcmd.run(["bash", "-c", "echo first; sleep 1; echo second"], str(tmp_path), None, 10,
                      lambda text: seen.append((time.monotonic() - start, text)))
    assert done.exit_code == 0 and "first" in done.text and "second" in done.text
    first = next(at for at, text in seen if "first" in text)
    assert first < 0.9, "the first line came only at the end"


@posix_only
def test_stdin_is_closed_so_a_prompt_fails_at_once(tmp_path):
    start = time.monotonic()
    done = runcmd.run(["bash", "-c", "read -r answer; echo \"got [$answer] $?\""], str(tmp_path), None, 20)
    assert time.monotonic() - start < 5 and "got []" in done.text


@posix_only
def test_the_time_limit_ends_the_command_and_what_it_started(tmp_path):
    pid_file = tmp_path / "child.pid"
    script = f"setsid sleep 300 & echo $! > {pid_file}; echo started; sleep 300"
    start = time.monotonic()
    done = runcmd.run(["bash", "-c", script], str(tmp_path), None, 1.5)
    assert done.timed_out and "started" in done.text and time.monotonic() - start < 10
    assert _eventually_dead(int(pid_file.read_text()))


@posix_only
def test_cancel_ends_the_command_at_once(tmp_path):
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    start = time.monotonic()
    done = runcmd.run(["bash", "-c", "sleep 300"], str(tmp_path), None, 300, cancel=cancel)
    assert done.cancelled and time.monotonic() - start < 6


@posix_only
def test_a_background_child_holding_the_output_cannot_hold_the_call(tmp_path):
    pid_file = tmp_path / "child.pid"
    start = time.monotonic()
    done = runcmd.run(["bash", "-c", f"setsid sleep 300 & echo $! > {pid_file}; echo bye"], str(tmp_path), None, 60)
    assert done.exit_code == 0 and "bye" in done.text and time.monotonic() - start < 10
    assert _eventually_dead(int(pid_file.read_text()))


@posix_only
def test_run_command_tells_the_model_it_was_stopped_at_the_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_COMMAND_TIMEOUT", "30")
    text, is_error = tools.run("run_command", str(tmp_path), {"command": "echo partial; sleep 60", "timeout_s": 1})
    assert is_error and "partial" in text and "did not finish in 1 seconds" in text
    text, is_error = tools.run("run_command", str(tmp_path), {"command": "echo ok", "timeout_s": 99999})
    assert not is_error and "[exit code 0]" in text


@posix_only
def test_direct_engine_streams_output_and_a_stop_kills_the_command(tmp_path):
    from hokma.engines.base import EngineContext
    from hokma.engines.direct.loop import DirectSession

    ctx = EngineContext(conversation_id="c1", model="m", cwd=str(tmp_path))
    session = DirectSession.__new__(DirectSession)
    session.ctx, session._interrupted, session._cancels = ctx, False, set()
    session._env = lambda: None
    pid_file = tmp_path / "child.pid"

    async def go():
        events = []
        gen = session._run_command({"id": "t1"}, {"command": f"echo working; sleep 300 & echo $! > {pid_file}; wait"})
        start = time.monotonic()
        async for item in gen:
            events.append(item)
            if item["type"] == "tool_output" and "working" in item["text"]:
                await session.interrupt()
        return events, time.monotonic() - start

    session._stream = None
    session._answers = {}
    events, took = asyncio.run(go())
    assert events[0]["type"] == "tool_output" and events[-1]["type"] == "result"
    assert events[-1]["is_error"] and "stopped by the owner" in events[-1]["text"] and took < 8
    assert _eventually_dead(int(pid_file.read_text()))

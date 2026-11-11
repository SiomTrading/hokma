"""Task 3174: on real Windows (0.0.23) the Terminal panel said "The terminal could not be started" and the voice said
"The download failed", both with no reason. Simulated here: Windows shells and pywinpty failing with errors that are
not OSError, the PATH of the terminal, and a model download failing with a TLS error. Every failure must reach the
screen with its real text and engine.log."""
import json
import os
import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hokma.core import tools
from hokma.terminal import pty_backend, session as term_session
from hokma.terminal.api import router as terminal_router
from hokma.voice import model as voice_model

URL = "/ws/terminal?token=t"


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOKMA_HOME", str(tmp_path / "work"))
    (tmp_path / "work").mkdir()
    return tmp_path / "data"


def engine_log(data) -> str:
    path = data / "logs" / "engine.log"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


@pytest.fixture
def windows(monkeypatch):
    """The terminal module as it behaves on Windows: Windows shell search, no Windows-only processes started."""
    monkeypatch.setattr(term_session, "IS_WINDOWS", True)
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    monkeypatch.delenv("COMSPEC", raising=False)


# ----------------------------------------------------------------------------------------------- terminal

def test_windows_shell_candidates_end_in_cmd_and_never_repeat(windows, monkeypatch):
    monkeypatch.setattr(term_session.shutil, "which", lambda name: rf"C:\apps\{name}" if name == "pwsh.exe" else None)
    shells = term_session.shell_candidates()
    assert shells[0] == [r"C:\apps\pwsh.exe", "-NoLogo"]
    assert shells[-1][0].lower().endswith("cmd.exe")
    assert len({tuple(s) for s in shells}) == len(shells)


def test_windows_env_always_has_systemroot_and_comspec(windows, monkeypatch):
    monkeypatch.delenv("SYSTEMROOT")
    monkeypatch.delenv("WINDIR", raising=False)
    env = term_session.build_env()
    assert env["SYSTEMROOT"] == r"C:\Windows" and env["COMSPEC"].lower().endswith("cmd.exe")
    assert term_session.build_env(plain=True)["TERM"] == "xterm-256color"


def test_terminal_path_only_gets_tool_folders_that_exist(data, monkeypatch):
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/bin"]))
    monkeypatch.setenv("HOME", str(data.parent / "home"))
    (data / "tools" / "node" / "bin").mkdir(parents=True)
    parts = term_session.build_env()["PATH"].split(os.pathsep)
    assert str(data / "tools" / "node" / "bin") in parts
    assert str(data / "tools" / "bun") not in parts and str(data / "bin") not in parts
    assert parts[:2] == ["/usr/bin", "/bin"]


def test_with_tools_keeps_the_old_behaviour_when_asked(data):
    assert str(data / "bin") in tools.path_with_tools("/usr/bin").split(os.pathsep)


def test_the_next_shell_starts_when_the_first_fails_with_a_non_os_error(windows, data, monkeypatch):
    class WinptyError(RuntimeError):            # what pywinpty raises: not an OSError
        pass

    calls = []

    def fake_open(argv, cwd, env, cols, rows):
        calls.append((argv[0], "SYSTEMROOT" in env))
        if "pwsh" in argv[0]:
            raise WinptyError("Unable to start the process (error 5)")
        return types.SimpleNamespace(pid=1, start_reading=lambda loop, queue: None)

    monkeypatch.setattr(term_session, "open_pty", fake_open)
    monkeypatch.setattr(term_session.shutil, "which", lambda name: rf"C:\apps\{name}" if name == "pwsh.exe" else None)
    started = term_session.TerminalSession.open(None, 80, 10, None)
    assert started.pid == 1 and calls[0][0].endswith("pwsh.exe") and calls[1][0].lower().endswith("cmd.exe")
    log = engine_log(data)
    assert "terminal: the terminal started after failures" in log and "WinptyError: Unable to start the process" in log


def test_when_nothing_starts_the_error_lists_every_attempt(windows, data, monkeypatch):
    def fake_open(argv, cwd, env, cols, rows):
        raise OSError(2, "The system cannot find the file specified")

    monkeypatch.setattr(term_session, "open_pty", fake_open)
    monkeypatch.setattr(term_session.shutil, "which", lambda name: None)
    with pytest.raises(term_session.TerminalStartError) as info:
        term_session.TerminalSession.open(None, 80, 10, None)
    assert "full environment" in info.value.detail and "plain environment" in info.value.detail
    assert "cannot find the file" in info.value.detail
    assert "the terminal could not start" in engine_log(data)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(terminal_router)
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setattr("hokma.terminal.api._token_ok", lambda candidate: True)


def test_the_socket_sends_the_real_reason_of_a_spawn_failure(client, data, monkeypatch):
    def boom(*a):
        raise RuntimeError("winpty: the pseudo console could not be created (0x8007007b)")

    monkeypatch.setattr(term_session.TerminalSession, "open", classmethod(lambda cls, *a: boom()))
    with client.websocket_connect(URL) as ws:
        frame = json.loads(ws.receive_text())
    assert frame["code"] == "spawn_failed" and "0x8007007b" in frame["detail"] and frame["detail"].startswith("RuntimeError")
    assert "spawn_failed" in engine_log(data) and "0x8007007b" in engine_log(data)


def test_the_socket_sends_the_attempts_of_a_terminal_start_error(client, data, monkeypatch):
    def fail(cls, *a):
        raise term_session.TerminalStartError("pwsh.exe (full environment): WinptyError: access denied")

    monkeypatch.setattr(term_session.TerminalSession, "open", classmethod(fail))
    with client.websocket_connect(URL) as ws:
        frame = json.loads(ws.receive_text())
    assert frame == {"type": "error", "code": "spawn_failed", "detail": "pwsh.exe (full environment): WinptyError: access denied"}


def test_a_missing_pywinpty_says_so(client, data, monkeypatch):
    def gone(cls, *a):
        raise pty_backend.PtyUnavailable("pywinpty is not installed")

    monkeypatch.setattr(term_session.TerminalSession, "open", classmethod(gone))
    with client.websocket_connect(URL) as ws:
        frame = json.loads(ws.receive_text())
    assert frame["code"] == "pty_unavailable" and "pywinpty" in frame["detail"]
    assert "pty_unavailable" in engine_log(data)


# ----------------------------------------------------------------------------------------------- voice

class SSLError(Exception):
    pass


def tls_failure(repo, target, report):
    try:
        raise SSLError("certificate verify failed: unable to get local issuer certificate")
    except SSLError as inner:
        raise RuntimeError("An error happened while trying to locate the file on the Hub") from inner


def test_a_failed_download_keeps_the_real_reason_and_logs_it(data, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("SSL_CERT_FILE", "C:/nope/cert.pem")
    manager = voice_model.ModelManager("small", fetch=tls_failure)
    manager.start_download()
    manager.wait(10)
    snap = manager.snapshot()
    assert snap["state"] == "error" and snap["error_code"] == "download_failed"
    assert "SSLError: certificate verify failed" in snap["error_detail"] and "RuntimeError" in snap["error_detail"]
    log = engine_log(data)
    assert "voice: the voice model download failed (download_failed)" in log
    assert "HTTPS_PROXY, SSL_CERT_FILE" in log and "proxy.example" not in log        # names only, never values
    assert "SSLError" in log


def test_the_status_endpoint_carries_the_detail(data, monkeypatch):
    from hokma.voice.api import create_router
    from hokma.rpc.ws import SESSION_TOKEN
    from hokma.voice.transcriber import FasterWhisperTranscriber

    manager = voice_model.ModelManager("small", fetch=tls_failure)
    manager.start_download()
    manager.wait(10)
    transcriber = FasterWhisperTranscriber.__new__(FasterWhisperTranscriber)
    transcriber.manager = manager
    monkeypatch.setattr(FasterWhisperTranscriber, "available", lambda self: True)
    app = FastAPI()
    app.include_router(create_router(lambda: transcriber))
    with TestClient(app) as c:
        body = c.get("/api/transcribe/status", headers={"X-Hokma-Token": SESSION_TOKEN}).json()
    assert body["error"]["code"] == "download_failed" and "SSLError" in body["error"]["detail"]
    assert body["error"]["message"]


def test_the_hub_download_turns_xet_off(monkeypatch, tmp_path):
    for name in ("HF_HUB_DISABLE_XET", "HF_HUB_DISABLE_SYMLINKS_WARNING"):
        monkeypatch.delenv(name, raising=False)
    fake = types.ModuleType("huggingface_hub")
    fake.snapshot_download = lambda *a, **k: None
    fake_tqdm = types.ModuleType("tqdm.auto")
    fake_tqdm.tqdm = type("tqdm", (), {})
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake)
    monkeypatch.setitem(sys.modules, "tqdm", types.ModuleType("tqdm"))
    monkeypatch.setitem(sys.modules, "tqdm.auto", fake_tqdm)
    try:
        voice_model.ModelManager._fetch_from_hub("Systran/faster-whisper-small", tmp_path, lambda *a, **k: None)
        assert os.environ["HF_HUB_DISABLE_XET"] == "1"
    finally:
        os.environ.pop("HF_HUB_DISABLE_XET", None)
        os.environ.pop("HF_HUB_DISABLE_SYMLINKS_WARNING", None)

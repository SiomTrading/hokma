"""Shared fixtures for the engine tests: a fake `claude` on PATH and an app wired to a ChatEngine."""
import json
import os
import signal
import struct
import sys
import zlib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hokma.engine.api import create_router
from hokma.engine.engine import ChatEngine
from hokma.engine.memory import MemoryStore, WorkdirProjects
from hokma.kit.launch import LaunchSpec
from hokma.rpc.ws import SESSION_TOKEN

FIXTURES = Path(__file__).resolve().parent / "engine_fixtures"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the fake claude is a POSIX script")


@pytest.fixture(autouse=True)
def hard_timeout():
    """A broken engine must fail the test, not hang the suite."""
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError("test took over 60 s")))
        signal.alarm(60)
    yield
    if hasattr(signal, "alarm"):
        signal.alarm(0)


class Env:
    def __init__(self, tmp: Path, monkeypatch) -> None:
        self.home, self.data = tmp / "home", tmp / "data"
        self.home.mkdir()
        self.data.mkdir()
        self.log = tmp / "fake.log"
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        claude = bin_dir / "claude"
        claude.write_text(f"#!{sys.executable}\nimport runpy\n"
                          f"runpy.run_path({str(FIXTURES / 'fake_claude.py')!r}, run_name='__main__')\n")
        claude.chmod(0o755)
        monkeypatch.setenv("HOKMA_HOME", str(self.home))
        monkeypatch.setenv("HOKMA_DATA_DIR", str(self.data))
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("FAKE_CLAUDE_LOG", str(self.log))
        from hokma.engines import envfilter
        monkeypatch.setattr(envfilter, "ALLOWED", (*envfilter.ALLOWED, "FAKE_CLAUDE_*"))
        self.store = MemoryStore()
        self.launch_spec = LaunchSpec(args=[], env={"CLAUDE_CONFIG_DIR": str(self.data / "cfg")}, cwd=str(self.home))

    def engine(self, **kwargs) -> ChatEngine:
        kwargs.setdefault("stop_grace", 0.5)
        return ChatEngine(store=self.store, projects=WorkdirProjects(self.home), launch=lambda: self.launch_spec,
                          session_token=SESSION_TOKEN, **kwargs)

    def entries(self, event: str | None = None) -> list[dict]:
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text().splitlines() if line]
        return [r for r in rows if event is None or r["event"] == event]


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    return Env(tmp_path, monkeypatch)


def make_client(engine: ChatEngine) -> TestClient:
    app = FastAPI()
    app.include_router(create_router(lambda: engine))
    return TestClient(app)


def connect(client: TestClient, token: str = SESSION_TOKEN):
    return client.websocket_connect(f"/ws/chat?token={token}")


def until(ws, *types: str) -> list[dict]:
    """Read events up to and including the first one whose type is in `types`."""
    events = []
    while True:
        event = ws.receive_json()
        events.append(event)
        if event["type"] in types:
            return events


def turn(ws, text: str, **fields) -> list[dict]:
    ws.send_json({"type": "send", "text": text, **fields})
    return until(ws, "done", "error")


def text_of(events: list[dict]) -> str:
    return "".join(e["text"] for e in events if e["type"] == "delta")


def png_bytes() -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b""))

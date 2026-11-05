"""Fixtures for the D7 tests: a working folder with the real kit agents, a migrated database, a profile and the fake claude."""
import json
import os
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine_support import FIXTURES
from hokma.agents.api import create_router
from hokma.agents.service import AgentService
from hokma.db import Actor, get_database, migrate
from hokma.kit.launch import LaunchSpec
from hokma.kit.sync import sync_kit
from hokma.rpc.ws import SESSION_TOKEN

KIT_TEMPLATE = Path(__file__).resolve().parents[2] / "kit" / "template"
HEADERS = {"X-Hokma-Token": SESSION_TOKEN}
SYSTEM = Actor.system()


class Work:
    def __init__(self, tmp: Path, monkeypatch) -> None:
        self.tmp = tmp
        self.home, self.data = tmp / "work", tmp / "data"
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
        monkeypatch.setenv("HOKMA_LANG", "en")
        monkeypatch.setenv("HOME", str(tmp / "home"))
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("FAKE_CLAUDE_LOG", str(self.log))
        monkeypatch.setenv("HOKMA_SCHEDULER_TICK", "0.1")
        self.launch_spec = LaunchSpec(args=[], env={"CLAUDE_CONFIG_DIR": str(self.data / "cfg")}, cwd=str(self.home))

    def entries(self, event: str | None = None) -> list[dict]:
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text().splitlines() if line]
        return [r for r in rows if event is None or r["event"] == event]

    def loaded(self) -> set[str]:
        folder = self.home / ".claude" / "agents"
        return {p.stem for p in folder.glob("*.md")} if folder.is_dir() else set()

    def parked(self) -> set[str]:
        folder = self.home / ".claude" / "agents.off"
        return {p.stem for p in folder.glob("*.md")} if folder.is_dir() else set()


@pytest.fixture
def work(tmp_path, monkeypatch) -> Work:
    w = Work(tmp_path, monkeypatch)
    sync_kit(KIT_TEMPLATE, version="test", workdir=w.home, values={"owner_name": "Ana", "fund_name": "Fondo Ana"})
    migrate()
    db = get_database()
    with db.write() as u:
        u.profile.save(owner_name="Ana", fund_name="Fondo Ana", timezone="UTC", workdir=str(w.home))
    AgentService(db).reconcile()                       # what the app does on start
    return w


@pytest.fixture
def db(work):
    return get_database()


def make_client(**kwargs) -> TestClient:
    app = FastAPI()
    app.include_router(create_router())
    return TestClient(app, **kwargs)


@pytest.fixture
def client(work):
    with make_client() as c:
        c.headers.update(HEADERS)
        yield c

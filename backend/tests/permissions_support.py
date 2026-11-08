"""Fixtures for the permission mode, trash and restore point tests: a migrated database with an owner, a working
folder that is a real Git repository, and the real app."""
import hashlib
import subprocess
from pathlib import Path

import json

import keyring.backend
import pytest
from fastapi.testclient import TestClient
from keyring.errors import PasswordDeleteError

from hokma.core import resume, session
from hokma.core.app import create_app
from hokma.db import get_database, migrate
from hokma.rpc.ws import SESSION_TOKEN



class MemoryKeyring(keyring.backend.KeyringBackend):
    """A usable credential store that lives in memory: nothing of these tests reaches the real one."""

    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.items:
            raise PasswordDeleteError("not found")
        del self.items[(service, username)]

    def stored(self) -> dict | None:
        raw = self.items.get((resume.SERVICE, resume.ENTRY))
        return json.loads(raw) if raw else None


# The remembered-session store of every test that imports this module never touches the system credential store.
resume.use_store_for_tests(resume.ResumeStore(MemoryKeyring()))

OWNER_NONCE = "0123456789abcdef0123456789abcdef"
OWNER_ONCE = "O" * 22                                   # the one-time secret of the start link
session.arm_owner_secret_for_tests(OWNER_ONCE)
assert session.register_owner(OWNER_NONCE, OWNER_ONCE) == session.REGISTERED   # the "open tab" of every test here
HEADERS = {"X-Hokma-Token": SESSION_TOKEN, "X-Hokma-Owner": OWNER_NONCE}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=t@example.com", *args], cwd=cwd,
                          capture_output=True, text=True, check=True)
    return done.stdout.strip()


class Owner:
    def __init__(self, tmp: Path, monkeypatch) -> None:
        self.tmp = tmp
        self.work, self.data = tmp / "work", tmp / "data"
        self.work.mkdir()
        self.data.mkdir()
        monkeypatch.setenv("HOME", str(tmp / "home"))
        monkeypatch.setenv("HOKMA_HOME", str(self.work))
        monkeypatch.setenv("HOKMA_DATA_DIR", str(self.data))
        monkeypatch.setenv("HOKMA_LANG", "en")
        migrate()
        self.db = get_database()
        with self.db.write() as u:
            u.profile.save(owner_name="Ana", workdir=str(self.work))
        self.client = TestClient(create_app())

    def repo(self, files: dict[str, str] | None = None) -> Path:
        """Make the working folder a Git repository with a first commit."""
        git(self.work, "init", "-q")
        for name, text in (files or {"notes.md": "some notes\n", "informes/viejo.md": "old report\n",
                                    "data.csv": "a,b\n1,2\n"}).items():
            target = self.work / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", "first")
        return self.work

    def profile(self):
        with self.db.read() as u:
            return u.profile.get()


@pytest.fixture
def owner(tmp_path, monkeypatch) -> Owner:
    return Owner(tmp_path, monkeypatch)

"""SPEC-011 P12: cloud backup and restore. Two folders play the old and the new computer; a local bare repository is
the student's GitHub repo and a folder is its release. No network."""
import shutil
import subprocess
import time

import keyring
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hokma.backup import cloud_runner, github_release, restore, secret_scan
from hokma.backup.api import create_router
from hokma.db import get_database, migrate
from hokma.docs.ingest import libraries_dir
from hokma.github_connect import flow
from hokma.projects import service
from hokma.rpc.ws import SESSION_TOKEN
from live_support import MemoryKeyring

HEAD = {"X-Hokma-Token": SESSION_TOKEN}


@pytest.fixture
def world(tmp_path, monkeypatch):
    previous = keyring.get_keyring()
    keyring.set_keyring(MemoryKeyring())
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    cloud = tmp_path / "cloud"
    cloud.mkdir()
    uploads = []
    monkeypatch.setattr(flow, "token", lambda: "tok")
    monkeypatch.setattr(flow, "ensure_private_repo", lambda name: {"full_name": f"student/{name}", "clone_url": str(bare)})

    def upload(path, repo, **kw):
        uploads.append(path.name)
        shutil.copy(path, cloud / path.name)

    monkeypatch.setattr(github_release, "upload", upload)
    monkeypatch.setattr(github_release, "list_backups", lambda repo, **kw: [
        {"name": p.name, "size": p.stat().st_size, "created_at": "2026-11-12", "parts": 1}
        for p in sorted(cloud.iterdir(), reverse=True)])
    monkeypatch.setattr(github_release, "download", lambda repo, name, dest, **kw: shutil.copy(cloud / name, dest / name))
    monkeypatch.setattr(secret_scan, "MAX_FILE_BYTES", 1000)

    def pc(name):
        monkeypatch.setenv("HOME", str(tmp_path / name / "home"))
        monkeypatch.setenv("HOKMA_HOME", str(tmp_path / name / "work"))
        monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / name / "data"))
        monkeypatch.setenv("HOKMA_LANG", "en")
        migrate()
        database = get_database()
        with database.write() as u:
            u.profile.save(owner_name="Ana")
        return database

    pc.uploads, pc.cloud = uploads, cloud
    yield pc
    keyring.set_keyring(previous)


def client(db):
    app = FastAPI()
    app.include_router(create_router())
    app.state.db = db
    return TestClient(app)


def wait_idle(api):
    for _ in range(100):
        state = api.get("/api/backup", headers=HEAD).json()
        if state["status"] != "running":
            return state
        time.sleep(0.1)
    raise AssertionError("the backup never finished")


def test_backup_then_restore_on_a_new_computer(world, tmp_path):
    db = world("old")
    api = client(db)
    project = service.create_project("Mi proyecto", onboarding=False)
    root = service.projects_root(db) / project["slug"]
    (root / "notas.txt").write_text("hello\n")
    (root / "big.bin").write_bytes(b"x" * 2000)                        # over the (lowered) Git limit
    (root / ".env").write_text("API_KEY=abc123\n")                      # a secret: never leaves the computer
    disk = tmp_path / "old" / "disk"
    disk.mkdir(parents=True)
    (disk / "paper.txt").write_text("from the disk\n")
    with db.write() as u:
        u.library.add_project_folder(project["id"], str(disk))
    (libraries_dir() / "1").mkdir(parents=True)
    (libraries_dir() / "1" / "7-book.txt").write_text("library original\n")
    memory = cloud_runner.memory_dir() / "p"
    memory.mkdir(parents=True)
    (memory / "note.md").write_text("remember this\n")

    before = api.get("/api/backup", headers=HEAD).json()                # what will be left out, before the first backup
    assert before["connected"] and before["status"] == "idle" and not before["key_ready"]
    assert before["total_size_estimate"] > 0

    api.post("/api/backup/run", headers=HEAD)                           # no key yet: a plain, readable failure
    state = wait_idle(api)
    assert state["status"] == "error" and "recovery key" in state["message"]

    key = api.post("/api/backup/key", headers=HEAD).json()["key"]
    assert api.post("/api/backup/key/confirm", json={"last4": "ZZZZ"}, headers=HEAD).json() == {"ok": False}
    last4 = key.replace("-", "").replace(" ", "")[-4:]
    assert api.post("/api/backup/key/confirm", json={"last4": last4}, headers=HEAD).json() == {"ok": True}

    api.post("/api/backup/run", headers=HEAD)
    state = wait_idle(api)
    assert state["status"] == "ok" and state["repo"] == "student/hokma-backup" and state["last_size"]
    assert {"path": f"projects/{project['slug']}/.env", "reason": "secret"} in state["omitted"]
    assert state["key_ready"] and len(world.uploads) == 1

    cloud_runner.run(db)                                                # nothing changed: no new file in the cloud
    assert len(world.uploads) == 1
    assert [b["name"] for b in api.get("/api/backup/list", headers=HEAD).json()] == world.uploads

    new = world("new")                                                  # the new computer: empty
    api = client(new)
    bad = api.post("/api/backup/restore", json={"name": world.uploads[0], "key": "AAAA-AAAA-AAAA-AAAA-AAAA-AAAA"},
                   headers=HEAD)
    assert bad.status_code == 400 and not (tmp_path / "new" / "data" / restore.STAGE).exists()
    done = api.post("/api/backup/restore", json={"name": world.uploads[0], "key": key}, headers=HEAD).json()
    assert done["status"] == "restart_required" and isinstance(done["reconnect"], list)
    assert (tmp_path / "new" / "work" / "projects" / project["slug"] / "notas.txt").read_text() == "hello\n"

    shutil.rmtree(disk)                                                 # the old folder is gone on this computer
    restore.apply_pending()                                             # what the engine does when it restarts
    restored = get_database()
    with restored.read() as u:
        assert u.projects.get(project["id"]).name == "Mi proyecto"
        assert u.session.scalar("SELECT count(*) FROM project_folders") == 2          # its own folder and the added one
    assert (disk / "paper.txt").read_text() == "from the disk\n"
    assert (libraries_dir() / "1" / "7-book.txt").read_text() == "library original\n"
    assert (cloud_runner.memory_dir() / "p" / "note.md").read_text() == "remember this\n"
    assert (tmp_path / "new" / "work" / "projects" / project["slug"] / "big.bin").stat().st_size == 2000
    assert not (tmp_path / "new" / "work" / "projects" / project["slug"] / ".env").exists()
    assert not (tmp_path / "new" / "data" / restore.STAGE).exists()
    restore.apply_pending()                                             # nothing staged: nothing happens


def test_apply_without_staged_restore_does_nothing(world):
    world("solo")
    restore.apply_pending()


def test_key_can_be_generated_again_until_confirmed(world):
    api = client(world("solo"))
    first = api.post("/api/backup/key", headers=HEAD).json()["key"]
    assert not api.get("/api/backup", headers=HEAD).json()["key_ready"]
    second = api.post("/api/backup/key", headers=HEAD)                 # the screen was closed before saving the first
    assert second.status_code == 200 and second.json()["key"] != first
    assert not api.get("/api/backup", headers=HEAD).json()["key_ready"]
    last4 = second.json()["key"].replace("-", "").replace(" ", "")[-4:]
    assert api.post("/api/backup/key/confirm", json={"last4": last4}, headers=HEAD).json() == {"ok": True}
    assert api.get("/api/backup", headers=HEAD).json()["key_ready"]
    again = api.post("/api/backup/key", headers=HEAD)                  # confirmed: final
    assert again.status_code == 409 and again.json()["detail"]["code"] == "key_exists"


def test_restore_restarts_the_engine_by_itself(world, monkeypatch):
    restarted = []

    class FakeLifecycle:
        async def restart(self):
            restarted.append(True)

    monkeypatch.setattr(restore, "prepare", lambda db, name, key: {"status": "restart_required", "reconnect": []})
    app = FastAPI()
    app.include_router(create_router())
    app.state.db = world("solo")
    app.state.lifecycle = FakeLifecycle()
    api = TestClient(app)
    body = api.post("/api/backup/restore", json={"name": "x.hkb", "key": "k"}, headers=HEAD)
    assert body.status_code == 200 and body.json() == {"status": "restart_required", "reconnect": []}
    assert restarted == [True]

    def fail(db, name, key):
        raise cloud_runner.BackupError("bad key")

    monkeypatch.setattr(restore, "prepare", fail)
    restarted.clear()
    assert api.post("/api/backup/restore", json={"name": "x.hkb", "key": "k"}, headers=HEAD).status_code == 400
    assert restarted == []                                              # a failed restore never restarts the engine

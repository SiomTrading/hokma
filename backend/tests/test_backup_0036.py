"""Hokma 0.0.36 backup fixes (tasks 1143, 1144, 1150): warning state, key confirmation, stale errors, language of the
panel, failed restore notice, pruning. Same doubles as test_backup_cloud.py: a local bare repository and a folder."""
from hokma.backup import cloud_runner, restore
from hokma.backup.fund_git import FundGit, SyncResult
from hokma.projects.service import workdir

from test_backup_cloud import HEAD, _first_backup, client, wait_idle, world  # noqa: F401  (fixture)


def _confirmed(api):
    key = api.post("/api/backup/key", headers=HEAD).json()["key"]
    last4 = key.replace("-", "").replace(" ", "")[-4:]
    assert api.post("/api/backup/key/confirm", json={"last4": last4}, headers=HEAD).json() == {"ok": True}


# ---------------------------------------------------------------------------------------------- 1144

def test_confirming_with_the_wrong_last4_is_a_400_and_the_key_stays_unconfirmed(world):
    api = client(world("solo"))
    api.post("/api/backup/key", headers=HEAD)
    wrong = api.post("/api/backup/key/confirm", json={"last4": "ZZZZ"}, headers=HEAD)
    assert wrong.status_code == 400 and wrong.json()["detail"]["code"] == "key_mismatch"
    assert not api.get("/api/backup", headers=HEAD).json()["key_ready"]


def test_confirming_with_an_empty_or_short_last4_is_a_422(world):
    api = client(world("solo"))
    api.post("/api/backup/key", headers=HEAD)
    for value in ("", "AB"):
        assert api.post("/api/backup/key/confirm", json={"last4": value}, headers=HEAD).status_code == 422


# ---------------------------------------------------------------------------------------------- 1143

def test_a_conflict_branch_is_a_warning_not_up_to_date(world, tmp_path):
    db, api = _first_backup(world)
    (workdir(db) / "plan.md").write_text("line\n")
    cloud_runner.run(db)
    runner = FundGit(tmp_path / "runner", remote=str(tmp_path / "remote.git"), identity=("Hokma runner", "hokma@localhost"))
    runner.ensure_repo()
    runner.pull()
    (runner.folder / "plan.md").write_text("from the runner\n")
    runner.sync(message="runner", conflict_branch="runner/1")
    (workdir(db) / "plan.md").write_text("from the desktop\n")             # both sides changed the same line
    cloud_runner.run(db)
    state = api.get("/api/backup", headers=HEAD).json()
    assert state["status"] == "warning" and "desktop/" in state["message"] and state["last_backup_at"]
    assert "separate branch" in state["message"]


def test_a_project_git_did_not_save_is_a_warning_and_is_listed_as_omitted(world, monkeypatch):
    db, api = _first_backup(world)
    real = FundGit.sync

    def sync(self, **kw):
        result = real(self, **kw)
        result.projects["mi-proyecto"] = "unsafe"
        return result

    monkeypatch.setattr(FundGit, "sync", sync)
    cloud_runner.run(db)
    state = api.get("/api/backup", headers=HEAD).json()
    assert state["status"] == "warning" and "mi-proyecto" in state["message"]
    assert {"path": "projects/mi-proyecto", "reason": "git"} in state["omitted"]


def test_a_clean_sync_stays_ok(world):
    _, api = _first_backup(world)
    assert api.get("/api/backup", headers=HEAD).json()["status"] == "ok"


def test_a_sync_result_with_a_successful_project_is_not_a_warning(world, monkeypatch):
    db, api = _first_backup(world)
    real = FundGit.sync

    def sync(self, **kw):
        result = real(self, **kw)
        result.projects["mi-proyecto"] = SyncResult()
        return result

    monkeypatch.setattr(FundGit, "sync", sync)
    cloud_runner.run(db)
    assert api.get("/api/backup", headers=HEAD).json()["status"] == "ok"


# ---------------------------------------------------------------------------------------------- 1150

def test_the_missing_key_error_goes_away_once_the_key_is_confirmed(world):
    api = client(world("solo"))
    api.post("/api/backup/run", headers=HEAD)
    failed = wait_idle(api)
    assert failed["status"] == "error" and failed["code"] == "backup.key.missing"
    _confirmed(api)
    state = api.get("/api/backup", headers=HEAD).json()
    assert state["status"] == "idle" and state["message"] is None and state["code"] is None


def test_the_no_token_error_goes_away_once_github_is_connected(world, monkeypatch):
    from hokma.github_connect import flow
    api = client(world("solo"))
    _confirmed(api)
    monkeypatch.setattr(flow, "token", lambda: None)
    api.post("/api/backup/run", headers=HEAD)
    failed = wait_idle(api)
    assert failed["status"] == "error" and failed["code"] == "backup.cloud.no_token"
    monkeypatch.setattr(flow, "token", lambda: "tok")
    assert api.get("/api/backup", headers=HEAD).json()["status"] == "idle"


def test_a_failed_backup_that_does_not_depend_on_connection_stays_an_error(world, monkeypatch):
    from hokma.backup import github_release
    db, api = _first_backup(world)
    (cloud_runner.memory_dir() / "p").mkdir(parents=True)
    (cloud_runner.memory_dir() / "p" / "n.md").write_text("changed\n")        # something to upload

    def broken(*a, **kw):
        raise github_release.BackupError("no network")

    monkeypatch.setattr(github_release, "upload", broken)
    cloud_runner.run(db)
    state = api.get("/api/backup", headers=HEAD).json()
    assert state["status"] == "error" and state["code"] is None


def test_the_messages_follow_the_language_of_the_panel(world):
    api = client(world("solo"))                                          # the engine is configured in English
    api.post("/api/backup/run", headers={**HEAD, "Accept-Language": "es"})
    wait_idle(api)
    failed = api.get("/api/backup", headers={**HEAD, "Accept-Language": "es"}).json()
    assert "clave de recuperaci\u00f3n" in failed["message"]
    again = api.get("/api/backup", headers={**HEAD, "Accept-Language": "en"}).json()
    assert "recovery key" in again["message"]                           # a coded failure is translated when it is read
    api.post("/api/backup/key", headers=HEAD)
    done = api.post("/api/backup/key/confirm", json={"last4": "ZZZZ"}, headers={**HEAD, "Accept-Language": "es"})
    assert "Esos 4 caracteres" in done.json()["detail"]["message"]


# ---------------------------------------------------------------------------------------------- restore notice, prune

def _stage(world_pc, tmp_path):
    db = world_pc("solo")
    stage = tmp_path / "solo" / "data" / restore.STAGE
    stage.mkdir(parents=True)
    (stage / restore.DB_ENTRY).write_bytes(b"not a database")
    return db, stage


def test_a_restore_that_could_not_be_applied_is_shown_and_can_be_dismissed(world, tmp_path, monkeypatch):
    db, stage = _stage(world, tmp_path)
    monkeypatch.setattr(restore, "safe_restore_db", lambda src, dst: False)
    restore.apply_pending()
    api = client(db)
    assert api.get("/api/backup", headers=HEAD).json()["restore_failed"]["at"]
    assert (stage / restore.DB_ENTRY).exists()                           # kept: nothing was lost
    assert api.delete("/api/backup/restore-failed", headers=HEAD).json() == {"ok": True}
    assert api.get("/api/backup", headers=HEAD).json()["restore_failed"] is None
    assert not stage.exists()


def test_a_restore_that_works_clears_an_old_notice(world, tmp_path, monkeypatch):
    db, stage = _stage(world, tmp_path)
    monkeypatch.setattr(restore, "safe_restore_db", lambda src, dst: False)
    restore.apply_pending()
    monkeypatch.setattr(restore, "safe_restore_db", lambda src, dst: True)
    restore.apply_pending()
    assert client(db).get("/api/backup", headers=HEAD).json()["restore_failed"] is None


def test_only_the_three_newest_copies_before_a_restore_are_kept(world, tmp_path, monkeypatch):
    db, stage = _stage(world, tmp_path)
    from hokma.db.paths import backups_dir
    backups_dir().mkdir(parents=True, exist_ok=True)
    for day in range(1, 7):
        (backups_dir() / f"before-restore-2026010{day}T000000.db").write_bytes(b"old")
    (backups_dir() / "other.db").write_bytes(b"keep")
    monkeypatch.setattr(restore, "safe_restore_db", lambda src, dst: True)
    restore.apply_pending()
    kept = sorted(p.name for p in backups_dir().glob("before-restore-*.db"))
    assert len(kept) == 3 and "before-restore-20260106T000000.db" in kept and "before-restore-20260105T000000.db" in kept
    assert "before-restore-20260101T000000.db" not in kept and (backups_dir() / "other.db").exists()


def test_the_status_cache_is_never_read_half_written(world):
    """The panel polls the status while a backup ends. A half-written cache was read as broken, rebuilt and saved over
    `failed_code`, so a failure showed no code (flaky in CI)."""
    import json
    import threading

    client(world("solo"))
    path = cloud_runner._cache_path()
    cloud_runner._save_cache({"failed_code": "backup.key.missing", "omitted": ["x" * 50] * 400})
    stop, bad = threading.Event(), []

    def reader():
        while not stop.is_set():
            try:
                if json.loads(path.read_text(encoding="utf-8")).get("failed_code") != "backup.key.missing":
                    bad.append("code lost")
            except (OSError, ValueError) as exc:
                bad.append(type(exc).__name__)

    thread = threading.Thread(target=reader)
    thread.start()
    for _ in range(300):
        cloud_runner._save_cache({"failed_code": "backup.key.missing", "omitted": ["x" * 50] * 400})
    stop.set()
    thread.join()
    assert bad == []


def test_a_failed_read_of_an_existing_cache_does_not_overwrite_the_failure_code(world, monkeypatch):
    from pathlib import Path

    db = world("solo")
    client(db)
    cloud_runner._save_cache({"failed_code": "backup.key.missing"})
    real = Path.read_text

    def blocked(self, *a, **kw):
        if self.name == "cloud-backup.json":
            raise PermissionError("busy")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", blocked)
    cloud_runner._cache(db)
    monkeypatch.setattr(Path, "read_text", real)
    assert cloud_runner._cache(db)["failed_code"] == "backup.key.missing"
    assert not list(cloud_runner._cache_path().parent.glob("*.tmp"))


def test_a_corrupt_cache_is_rebuilt_on_disk_once(world):
    import json

    db = world("solo")
    client(db)
    cloud_runner._cache_path().write_text("{not json", encoding="utf-8")
    cloud_runner._cache(db)
    assert isinstance(json.loads(cloud_runner._cache_path().read_text(encoding="utf-8")), dict)


def test_a_started_backup_reads_as_running_before_its_thread_runs(world, monkeypatch):
    """`POST /api/backup/run` answers running: the very next status read must say so too, not the previous state while the
    thread is still starting (this made the tests that wait for the backup flaky on a loaded machine)."""
    import threading
    import time

    db = world("solo")
    client(db)
    gate = threading.Event()
    monkeypatch.setattr(cloud_runner, "_backup", lambda _db: gate.wait(10))
    cloud_runner.start(db)
    assert cloud_runner.status(db)["status"] == "running"
    cloud_runner.start(db)                                   # a second start while one is held does nothing
    gate.set()
    for _ in range(100):
        if cloud_runner.status(db)["status"] != "running":
            break
        time.sleep(0.05)
    assert cloud_runner.status(db)["status"] != "running"



def test_a_slow_rebuild_of_the_cache_never_lands_over_the_failure_code(world, monkeypatch):
    """The status poll rebuilds a missing cache (a slow walk of the files) while the backup thread saves its `failed_code`;
    the late rebuild used to overwrite it (the flaky `code None` of test_the_no_token_error_goes_away...)."""
    import json
    import threading

    db = world("solo")
    client(db)
    cloud_runner._cache_path().unlink(missing_ok=True)
    started, release = threading.Event(), threading.Event()
    real_plan = cloud_runner.plan

    def slow_plan(database):
        started.set()
        release.wait(10)
        return real_plan(database)

    monkeypatch.setattr(cloud_runner, "plan", slow_plan)
    poll = threading.Thread(target=cloud_runner._cache, args=(db,))
    poll.start()
    assert started.wait(10)
    monkeypatch.setattr(cloud_runner, "plan", real_plan)
    cloud_runner._failed(db, "x", "backup.cloud.no_token")          # the backup thread finishes meanwhile
    release.set()
    poll.join()
    assert json.loads(cloud_runner._cache_path().read_text(encoding="utf-8"))["failed_code"] == "backup.cloud.no_token"

"""A project with no history gets one by itself when it is listed or opened, once, without blocking and without ever
touching an existing `.git`. The warning only stays when Git is not installed or the project's Git is unsafe."""
import os
import subprocess
import time

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)

from hokma.projects import gitops, service


@pytest.fixture(autouse=True)
def fresh_attempts():
    gitops._AUTO_TRIED.clear()
    yield
    gitops._AUTO_TRIED.clear()


@pytest.fixture
def project(db):
    with db.write() as u:
        u.profile.save(owner_name="Ana", fund_name="Fondo Prueba")
    made = service.create_project("Sin historial", db=db)
    from pathlib import Path
    path = Path(made["path"])
    # simulate a project made before it had history: drop the repository Hokma created
    import shutil
    shutil.rmtree(path / ".git")
    return path


def entry(db, path):
    return next(p for p in service.list_projects(db)["projects"] if p["path"] == str(path))


def test_listing_creates_the_history_and_no_warning(db, project):
    assert gitops.repo_state(project).reason == "no_repo"
    git = entry(db, project)["git"]
    assert git == {"ok": True, "reason": ""}
    assert (project / ".git").is_dir() and gitops.repo_state(project).ok
    assert gitops.log(project)                                                # the first commit exists


def test_opening_the_tree_creates_it_too(db, project):
    row = next(p for p in service.list_projects(db)["projects"] if p["path"] == str(project))
    import shutil
    shutil.rmtree(project / ".git")
    gitops._AUTO_TRIED.clear()
    assert service.project_tree(row["id"], db=db)["git"]["ok"]


def test_it_is_attempted_once_per_project(db, project, monkeypatch):
    calls = []
    real = gitops.init_repo
    monkeypatch.setattr(gitops, "init_repo", lambda *a, **k: calls.append(1) or real(*a, **k))
    entry(db, project)
    entry(db, project)
    assert len(calls) == 1
    import shutil
    shutil.rmtree(project / ".git")                                           # gone again: not retried in this run
    assert entry(db, project)["git"]["reason"] == "no_repo" and len(calls) == 1


def test_without_git_installed_the_state_says_missing_and_nothing_runs(db, project, monkeypatch):
    called = []
    monkeypatch.setattr(gitops, "git_installed", lambda: False)
    monkeypatch.setattr(gitops, "init_repo", lambda *a, **k: called.append(1))
    git = entry(db, project)["git"]
    assert git == {"ok": False, "reason": "missing"} and called == []
    assert not (project / ".git").exists()


def test_an_unsafe_git_is_reported_and_left_alone(db, project):
    (project / ".git").mkdir()
    (project / ".git" / "config").write_text("[core]\n\tfsmonitor = evil\n")
    before = (project / ".git" / "config").read_text()
    git = entry(db, project)["git"]
    assert git == {"ok": False, "reason": "unsafe"}
    assert (project / ".git" / "config").read_text() == before and sorted(os.listdir(project / ".git")) == ["config"]


def test_an_existing_repository_is_never_touched(db, project):
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(["git", "-C", str(project), "-c", "user.name=A", "-c", "user.email=a@b.c", "commit", "-q",
                    "--allow-empty", "-m", "mine"], check=True)
    head = (project / ".git" / "HEAD").read_text()
    entry(db, project)
    assert gitops.log(project) == ["mine"] and (project / ".git" / "HEAD").read_text() == head


def test_the_listing_does_not_wait_for_a_slow_git(db, project, monkeypatch):
    monkeypatch.setattr(gitops, "AUTO_INIT_WAIT", 0.2)
    monkeypatch.setattr(gitops, "_auto_init", lambda p, m: time.sleep(1.5))
    start = time.perf_counter()
    state = gitops.ensure_repo(project, "m", wait=0.2)
    assert time.perf_counter() - start < 1.0 and state.reason == "no_repo"


def test_a_symlink_or_missing_folder_is_not_initialized(tmp_path):
    link = tmp_path / "link"
    target = tmp_path / "target"
    target.mkdir()
    link.symlink_to(target)
    assert gitops.ensure_repo(link, "m").reason == "no_repo" and not (target / ".git").exists()
    assert gitops.ensure_repo(tmp_path / "gone", "m").reason == "no_repo"

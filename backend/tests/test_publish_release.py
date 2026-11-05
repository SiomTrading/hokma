"""scripts/publish_release.py: dry run on a temporary repository, with a fake `gh` on PATH. No network."""
import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "publish_release.py"
FILES = {"pyproject.toml": "x", "uv.lock": "x", "backend/hokma/__init__.py": '__version__ = "0.3.0"\n',
         "cli/hokma_cli/main.py": "x", "dashboard/dist/index.html": "x",
         "installer/lock.json": "{}", "kit/template/CLAUDE.md": "x"}

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    for name, text in FILES.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    return repo, git


def fake_gh(tmp_path, exit_code):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(f"#!/bin/sh\nexit {exit_code}\n")
    gh.chmod(0o755)
    return bin_dir


def run_script(repo, bin_dir, *args):
    env = {**os.environ, "PATH": os.pathsep.join([str(bin_dir), os.environ["PATH"]])}
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=repo, capture_output=True, text=True, env=env)


def test_dry_run_builds_a_coherent_package_and_publishes_nothing(tmp_path):
    repo, git = make_repo(tmp_path)
    r = run_script(repo, fake_gh(tmp_path, 0), "--dry-run")
    assert r.returncode == 0, r.stderr
    package = repo / "dist" / "hokma-0.3.0.tar.gz"
    line = (repo / "dist" / "hokma-0.3.0.tar.gz.sha256").read_text()
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    assert line == f"{digest}  hokma-0.3.0.tar.gz\n" and digest in r.stdout
    with tarfile.open(package) as tar:
        assert "hokma-0.3.0/backend/hokma/__init__.py" in tar.getnames()
    assert subprocess.run([*git, "tag"], capture_output=True, text=True).stdout == ""      # nothing tagged
    again = run_script(repo, tmp_path / "bin", "--dry-run")
    assert again.returncode == 1 and "not clean" in again.stderr             # dist/ is untracked in this repo


def test_dry_run_fails_when_gh_is_not_authenticated(tmp_path):
    repo, _ = make_repo(tmp_path)
    r = run_script(repo, fake_gh(tmp_path, 1), "--dry-run")
    assert r.returncode == 1 and "not authenticated" in r.stderr
    assert not (repo / "dist").exists()


def test_dry_run_fails_when_head_is_not_origin_main(tmp_path):
    repo, git = make_repo(tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run([*git, "remote", "add", "origin", str(remote)], check=True)
    subprocess.run([*git, "push", "-q", "origin", "HEAD:refs/heads/main"], check=True)
    (repo / "extra.txt").write_text("x")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "ahead"], check=True)
    r = run_script(repo, fake_gh(tmp_path, 0), "--dry-run")
    assert r.returncode == 1 and "HEAD is not origin/main" in r.stderr


def load_script():
    sys.path.insert(0, str(SCRIPT.parent.parent / "backend"))
    spec = importlib.util.spec_from_file_location("publish_release_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_run_lets_gh_create_the_tag_on_the_remote_and_pushes_no_tag(tmp_path, monkeypatch):
    module = load_script()
    repo, git = make_repo(tmp_path)
    package = repo / "dist" / "hokma-0.3.0.tar.gz"
    package.parent.mkdir()
    package.write_bytes(b"x")
    calls, git_calls = [], []

    def fake_run(name, args, **kwargs):
        calls.append((name, args))
        return subprocess.CompletedProcess(args, 0, "https://example.test/release\n", "")

    real_git = module.git
    monkeypatch.setattr(module, "run_isolated", fake_run)
    monkeypatch.setattr(module, "git", lambda root, *a, **kw: (git_calls.append(a), real_git(root, *a, **kw))[1])
    url = module.publish(repo, "0.3.0", package, None)
    head = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert url == "https://example.test/release"
    (name, args), = calls
    assert name == "gh" and args[:2] == ["release", "create"] and args[2] == "v0.3.0"
    assert args[args.index("--target") + 1] == head and "--verify-tag" not in args
    assert args[args.index("--repo") + 1] == "SiomTrading/hokma"
    assert all(a[0] not in ("tag", "push") for a in git_calls)


def test_a_failed_release_leaves_no_orphan_tag(tmp_path, monkeypatch):
    module = load_script()
    repo, git = make_repo(tmp_path)
    package = repo / "p.tar.gz"
    package.write_bytes(b"x")
    removed = []
    real_git = module.git

    def fake_git(root, *a, **kw):
        if a[0] == "ls-remote":
            return subprocess.CompletedProcess(a, 0, "abc\trefs/tags/v0.3.0\n", "")
        if a[0] == "push":
            removed.append(a)
            return subprocess.CompletedProcess(a, 0, "", "")
        return real_git(root, *a, **kw)

    def fake_run(name, args, **kwargs):
        if args[:2] == ["release", "create"]:
            return subprocess.CompletedProcess(args, 1, "", "boom")
        return subprocess.CompletedProcess(args, 1, "", "release not found")        # `release view`: none exists

    monkeypatch.setattr(module, "git", fake_git)
    monkeypatch.setattr(module, "run_isolated", fake_run)
    with pytest.raises(module.PublishError, match="boom"):
        module.publish(repo, "0.3.0", package, None)
    assert removed == [("push", "origin", ":refs/tags/v0.3.0")]

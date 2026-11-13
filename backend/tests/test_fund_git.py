"""Git of the fund: commit, pull, push, rejection, conflict branches, secrets, size cap, projects. Local bare remote, no network."""
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from hokma.backup import fund_git, secret_scan
from hokma.backup.fund_git import FundGit, FundGitError, desktop_branch, parse_remote
from hokma.projects.gitops import repo_state

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths in assertions")
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOKMA_HOME", str(tmp_path / "work"))
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOKMA_LANG", "en")


def git(cwd, *args):
    done = subprocess.run([*GIT, *args], cwd=cwd, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


@pytest.fixture
def remote(tmp_path):
    path = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(path))
    return path


def fund(tmp_path, name, remote, **kw):
    return FundGit(tmp_path / name, remote=str(remote), **kw)


def write(folder, rel, text="x\n"):
    path = Path(folder) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def remote_files(remote, ref="main"):
    out = git(remote, "ls-tree", "-r", "--name-only", ref)
    return set(out.splitlines())


def remote_branches(remote):
    return set(git(remote, "for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines())


# ------------------------------------------------------------------------------------------------ setup


def test_ensure_repo_inits_excludes_projects_and_keeps_the_config_plain(tmp_path, remote):
    f = fund(tmp_path, "a", remote)
    f.ensure_repo()
    f.ensure_repo()                                              # idempotent
    assert repo_state(f.folder).ok
    assert git(f.folder, "symbolic-ref", "--short", "HEAD") == "main"
    exclude = f.folder / ".git" / "info" / "exclude"
    assert "projects/" not in (exclude.read_text() if exclude.exists() else "")      # nothing is written inside .git
    write(f.folder, "projects/p/a.txt")
    write(f.folder, "note.txt")
    rules = f"core.excludesFile={f._rules_file()}"
    assert git(f.folder, "-c", rules, "status", "--porcelain", "-uall").split() == ["??", "note.txt"]


def test_a_changed_repository_config_stops_git(tmp_path, remote):
    f = fund(tmp_path, "a", remote)
    f.ensure_repo()
    with open(f.folder / ".git" / "config", "a") as handle:
        handle.write("[core]\n\tsshCommand = touch /tmp/never\n")
    for call in (f.ensure_repo, lambda: f.commit_all("m"), f.pull):
        with pytest.raises(FundGitError) as caught:
            call()
        assert caught.value.code == "unsafe"
    assert "does not run" in str(caught.value)


def test_remote_addresses_are_checked():
    for bad in ("", "-oProxyCommand=x", "ext::sh -c id", "https://user:pw@github.com/a/b.git", "relative/path",
                "https://", "file with space"):
        with pytest.raises(FundGitError) as caught:
            parse_remote(bad)
        assert caught.value.code == "bad_remote"
    for good in ("git@github.com:o/n.git", "https://github.com/o/n.git", "ssh://git@host:2222/o/n.git", "/srv/repo.git"):
        assert parse_remote(good) == good


def test_a_fund_without_remote_does_not_sync(tmp_path):
    with pytest.raises(FundGitError):
        FundGit(tmp_path / "x").sync(message="m", conflict_branch="b")           # no remote


# ------------------------------------------------------------------------------------------------ commit, push, pull


def test_commit_all_commits_once_and_not_without_changes(tmp_path, remote):
    f = fund(tmp_path, "a", remote)
    f.ensure_repo()
    write(f.folder, "notes.md", "hello\n")
    first = f.commit_all("First")
    assert first.committed and first.head
    assert not f.commit_all("Again").committed
    assert git(f.folder, "log", "-1", "--format=%an <%ae>|%s") == "Hokma <hokma@localhost>|First"


def test_first_sync_pushes_and_projects_stay_out_of_the_fund_repository(tmp_path, remote):
    f = fund(tmp_path, "a", remote)
    write(f.folder, "notes.md")
    write(f.folder, "projects/alpha/file.txt", "inside the project\n")
    result = f.sync(message="Backup", conflict_branch="desktop/x")
    assert result.status == "synced" and result.committed
    assert remote_files(remote) == {"notes.md"}                      # nothing from projects/
    assert not result.projects                                       # alpha has no repository of its own yet


def test_each_project_syncs_as_its_own_repository(tmp_path, remote):
    f = fund(tmp_path, "a", remote)
    f.ensure_repo()
    project = f.folder / "projects" / "alpha"
    project.mkdir(parents=True)
    git(project, "init", "-q", "-b", "main")
    write(project, "model.py", "print(1)\n")
    write(f.folder, "notes.md")
    result = f.sync(message="Backup", conflict_branch="desktop/x")
    assert remote_branches(remote) == {"main", "hokma-projects/alpha"}
    assert remote_files(remote, "hokma-projects/alpha") == {"model.py"}
    assert remote_files(remote) == {"notes.md"}
    assert result.projects["alpha"].head


def test_a_runner_brings_in_the_projects_that_only_the_remote_has(tmp_path, remote):
    a = fund(tmp_path, "a", remote)
    a.ensure_repo()
    project = a.folder / "projects" / "alpha"
    project.mkdir(parents=True)
    git(project, "init", "-q", "-b", "main")
    write(project, "model.py", "print(1)\n")
    write(a.folder, "notes.md")
    a.sync(message="Backup", conflict_branch="desktop/x")
    b = fund(tmp_path, "b", remote, adopt_projects=True, identity=("Hokma runner", "hokma@localhost"))
    b.pull_all()
    assert (b.folder / "notes.md").exists()
    assert (b.folder / "projects" / "alpha" / "model.py").read_text() == "print(1)\n"
    write(b.folder, "projects/alpha/more.py", "print(2)\n")
    b.sync(message="Run x", conflict_branch="runner/1")
    assert remote_files(remote, "hokma-projects/alpha") == {"model.py", "more.py"}


def test_pull_creates_then_fast_forwards(tmp_path, remote):
    a, b = fund(tmp_path, "a", remote), fund(tmp_path, "b", remote)
    write(a.folder, "one.md")
    a.sync(message="m1", conflict_branch="desktop/x")
    b.ensure_repo()
    assert b.pull().status == "created" and (b.folder / "one.md").exists()
    assert b.pull().status == "up_to_date"
    write(a.folder, "two.md")
    a.sync(message="m2", conflict_branch="desktop/x")
    assert b.pull().status == "fast_forward" and (b.folder / "two.md").exists()


def test_pull_from_an_empty_remote_is_not_an_error(tmp_path, remote):
    f = fund(tmp_path, "a", remote)
    f.ensure_repo()
    assert f.pull().status == "empty_remote"


def test_a_rejected_push_fetches_rebases_and_pushes(tmp_path, remote):
    a, b = fund(tmp_path, "a", remote), fund(tmp_path, "b", remote)
    write(a.folder, "base.md")
    a.sync(message="base", conflict_branch="desktop/x")
    b.ensure_repo()
    b.pull()
    write(b.folder, "from_b.md")
    b.sync(message="b", conflict_branch="runner/1")
    write(a.folder, "from_a.md")
    a.commit_all("a")
    assert a._push_ref("refs/heads/main") == "rejected"                                    # the remote moved: not forced
    result = a.sync(message="a2", conflict_branch="desktop/x")
    assert result.status == "synced" and not result.branches
    assert remote_files(remote) == {"base.md", "from_a.md", "from_b.md"}
    assert git(remote, "rev-list", "--count", "main") == "3"


def test_a_conflict_goes_to_its_own_branch_and_nothing_is_lost(tmp_path, remote):
    a = fund(tmp_path, "a", remote)
    r = fund(tmp_path, "r", remote, identity=("Hokma runner", "hokma@localhost"))
    write(a.folder, "plan.md", "line\n")
    a.sync(message="base", conflict_branch="desktop/x")
    r.ensure_repo()
    r.pull()
    write(a.folder, "plan.md", "desktop edit\n")
    a.sync(message="desktop", conflict_branch="desktop/x")
    write(r.folder, "plan.md", "runner edit\n")
    result = r.sync(message="Run daily 2026-11-05 10:00", conflict_branch="desktop/42")
    assert result.status == "conflict_branch" and result.branch == "desktop/42" and result.branches == ["desktop/42"]
    assert remote_branches(remote) == {"main", "desktop/42"}
    assert git(remote, "show", "main:plan.md") == "desktop edit"          # the desktop's work is untouched
    assert git(remote, "show", "desktop/42:plan.md") == "runner edit"     # and so is the other machine's
    assert git(remote, "log", "-1", "--format=%an|%s", "desktop/42") == "Hokma runner|Run daily 2026-11-05 10:00"


def test_the_desktop_keeps_its_files_after_a_conflict(tmp_path, remote):
    a, b = fund(tmp_path, "a", remote), fund(tmp_path, "b", remote)
    write(a.folder, "plan.md", "line\n")
    a.sync(message="base", conflict_branch="desktop/x")
    b.ensure_repo()
    b.pull()
    write(b.folder, "plan.md", "other machine\n")
    b.sync(message="b", conflict_branch="runner/1")
    write(a.folder, "plan.md", "my edit\n")
    name = desktop_branch(date(2026, 11, 5))
    result = a.sync(message="Backup", conflict_branch=name)
    assert result.branch == "desktop/2026-11-05" and name in remote_branches(remote)
    assert (a.folder / "plan.md").read_text() == "my edit\n"              # nothing is reset under the owner
    assert git(remote, "show", f"{name}:plan.md") == "my edit"
    again = a.sync(message="Backup", conflict_branch=name)               # the same day, nothing new: still fine
    assert again.status == "conflict_branch"


def test_a_taken_conflict_branch_name_gets_a_suffix_not_a_force(tmp_path, remote):
    a, b, c = (fund(tmp_path, n, remote) for n in "abc")
    write(a.folder, "plan.md", "line\n")
    a.sync(message="base", conflict_branch="x/1")
    for other in (b, c):
        other.ensure_repo()
        other.pull()
    write(b.folder, "plan.md", "b\n")
    b.sync(message="b", conflict_branch="x/1")
    write(a.folder, "plan.md", "a\n")
    assert a.sync(message="a", conflict_branch="desktop/day").branch == "desktop/day"
    write(c.folder, "plan.md", "c\n")
    # the same branch name with a different history: the push is rejected and the name gets the commit as a suffix
    third = c.sync(message="c", conflict_branch="desktop/day")
    assert third.branch.startswith("desktop/day-")
    assert git(remote, "show", "desktop/day:plan.md") == "a"
    assert git(remote, "show", f"{third.branch}:plan.md") == "c"


def test_no_command_ever_forces_anything(tmp_path, remote, monkeypatch):
    seen = []
    real = subprocess.run

    def spy(argv, *a, **k):
        seen.append(list(argv))
        return real(argv, *a, **k)

    monkeypatch.setattr(fund_git.subprocess, "run", spy)
    a = fund(tmp_path, "a", remote)
    r = fund(tmp_path, "r", remote, adopt_projects=True)
    write(a.folder, "plan.md", "line\n")
    a.sync(message="base", conflict_branch="desktop/x")
    r.pull_all()
    write(a.folder, "plan.md", "a\n")
    a.sync(message="a", conflict_branch="desktop/x")
    write(r.folder, "plan.md", "r\n")
    assert r.sync(message="r", conflict_branch="runner/9").status == "conflict_branch"
    pushes = [argv for argv in seen if "push" in argv]
    assert pushes and any("fetch" in argv for argv in seen) and any("rebase" in argv for argv in seen)
    for argv in [a for a in seen if "--list" not in a]:               # the config check itself is a plain read
        for arg in argv:
            assert not arg.startswith("+") and not arg.startswith("--force") and arg not in ("-f", "--mirror", "--delete"), argv
    for argv in pushes:
        refspec = argv[-1]
        assert refspec.startswith("HEAD:refs/heads/") and "+" not in refspec


# ------------------------------------------------------------------------------------------------ secrets and size


def fake(prefix, tail):
    return prefix + tail            # built at run time: the test file holds no token-looking text


def test_secrets_stay_out_of_the_commit_and_are_reported_without_their_value(tmp_path, remote):
    f = fund(tmp_path, "a", remote)
    token = fake("ghp_", "a1B2" * 9)
    write(f.folder, "ok.md", "nothing here\n")
    write(f.folder, "notes/token.md", f"first line\nkey = {token}\n")
    write(f.folder, "keys/id.pem", fake("-----BEGIN OPENSSH ", "PRIVATE KEY-----\nabc\n"))
    write(f.folder, ".env", "API_KEY=abc123\n")
    write(f.folder, ".env.example", "API_KEY=\n")
    result = f.sync(message="Backup", conflict_branch="desktop/x")
    assert remote_files(remote) == {"ok.md", ".env.example"}
    found = {(s.path, s.reason, s.line) for s in result.skipped}
    assert found == {("notes/token.md", "secret", 2), ("keys/id.pem", "secret", 1), (".env", "secret", 1)}
    assert token not in repr(result) and token not in " ".join(s.message() for s in result.skipped)
    assert (f.folder / "notes" / "token.md").read_text().count(token) == 1       # the file itself is left alone
    assert "line 2" in [s for s in result.skipped if s.path == "notes/token.md"][0].message()


def test_a_file_over_the_cap_stays_out_and_the_sync_goes_on(tmp_path, remote):
    f = fund(tmp_path, "a", remote)
    write(f.folder, "small.md")
    with open(f.folder / "huge.bin", "wb") as handle:
        handle.truncate(secret_scan.MAX_FILE_BYTES + 1024 * 1024)          # sparse: no disk used
    assert secret_scan.MAX_FILE_BYTES == 95 * 1024 * 1024
    result = f.sync(message="Backup", conflict_branch="desktop/x")
    assert remote_files(remote) == {"small.md"}
    assert [(s.path, s.reason) for s in result.skipped] == [("huge.bin", "too_large")]
    again = f.sync(message="Backup", conflict_branch="desktop/x")              # still reported next time, still out
    assert [s.path for s in again.skipped] == ["huge.bin"] and not again.committed


def test_secret_scan_rules():
    assert secret_scan.scan_text("a\nb\n" + fake("AKIA", "ABCDEFGHIJKLMNOP") + "\n") == 3
    assert secret_scan.scan_text("plain text, no keys\n") is None
    assert secret_scan.scan_text("KEY=\n", ".env") is None and secret_scan.scan_text("KEY=1\n", ".env") == 1
    assert secret_scan.scan_text("KEY=1\n", "settings.txt") is None
    assert secret_scan.scan_text("KEY=1\n", ".env.template") is None


def test_the_binary_and_the_missing_file_are_not_scanned(tmp_path):
    blob = tmp_path / "b.bin"
    blob.write_bytes(b"\0\0" + fake("ghp_", "a" * 36).encode())
    assert secret_scan.scan_file(blob) is None and secret_scan.scan_file(tmp_path / "nope") is None


# ------------------------------------------------------------------------------------------------ names


def test_branch_names():
    assert desktop_branch(date(2026, 1, 2)) == "desktop/2026-01-02"


def test_every_git_call_runs_with_the_known_good_config(tmp_path, remote, monkeypatch):
    seen = []
    real = subprocess.run

    def spy(argv, *a, **kw):
        seen.append(list(argv))
        return real(argv, *a, **kw)

    monkeypatch.setattr(fund_git.subprocess, "run", spy)
    f = fund(tmp_path, "a", remote)
    write(f.folder, "a.txt")
    f.sync(message="m", conflict_branch="desktop/x")
    assert seen
    for argv in [a for a in seen if "--list" not in a]:               # the config check itself is a plain read
        assert "credential.helper=" in argv and "core.sshCommand=ssh" in argv
        hooks = [a for a in argv if a.startswith("core.hooksPath=")]
        assert hooks and hooks[-1] == "core.hooksPath=" + os.devnull               # the last one given wins
        assert any(a.startswith("core.excludesFile=") for a in argv)


def test_a_url_rewrite_in_the_repository_config_stops_every_network_call(tmp_path, remote):
    f = fund(tmp_path, "a", remote)
    write(f.folder, "a.txt")
    f.sync(message="m", conflict_branch="desktop/x")
    with open(f.folder / ".git" / "config", "a") as handle:
        handle.write('[url "ssh://evil.example/"]\n\tinsteadOf = https://github.com/\n')
    for call in (lambda: f._push_ref("refs/heads/main"), f.pull, lambda: f._adopt(f.folder / "projects")):
        with pytest.raises(FundGitError) as info:
            call()
        assert info.value.code == "unsafe"


def test_projects_that_are_links_or_point_outside_are_not_synced(tmp_path, remote):
    f = fund(tmp_path, "a", remote)
    f.ensure_repo()
    outside = tmp_path / "outside"
    git(tmp_path, "init", "-q", str(outside))
    (f.folder / "projects").mkdir()
    (f.folder / "projects" / "linked").symlink_to(outside)
    git(tmp_path, "init", "-q", str(f.folder / "projects" / "real"))
    assert [c.project_slug for c in f._children()] == ["real"]
    (f.folder / "projects" / "real").rename(tmp_path / "moved-real")
    (f.folder / "projects" / "linked").unlink()
    (f.folder / "projects").rmdir()
    (tmp_path / "moved").mkdir()
    (tmp_path / "moved-real").rename(tmp_path / "moved" / "real")
    (f.folder / "projects").symlink_to(tmp_path / "moved")                          # the whole folder is a link
    assert f._children() == []


def test_a_hook_file_never_runs_during_a_sync(tmp_path, remote, monkeypatch):
    marker = tmp_path / "ran"
    script = f"#!/bin/sh\ntouch {marker}\n"
    cwd = tmp_path / "helper-cwd"
    for folder in (cwd / "hooks", cwd / ".git" / "hooks"):
        folder.mkdir(parents=True)
    f = fund(tmp_path, "a", remote)
    f.ensure_repo()
    folders = [cwd / "hooks", cwd / ".git" / "hooks", f.folder / ".git" / "hooks"]
    for folder in folders:
        folder.mkdir(parents=True, exist_ok=True)
        for name in ("pre-push", "reference-transaction", "post-checkout", "post-merge", "pre-rebase", "pre-commit"):
            hook = folder / name
            hook.write_text(script)
            hook.chmod(0o755)

    class FixedDir:                       # the fresh working folder of each git call, here with hooks already in it
        def __enter__(self):
            return str(cwd)

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(fund_git.tempfile, "TemporaryDirectory", lambda **kw: FixedDir())
    write(f.folder, "a.txt")
    f.sync(message="m", conflict_branch="desktop/x")
    f.pull()
    f._push_ref("refs/heads/main")
    assert not marker.exists()

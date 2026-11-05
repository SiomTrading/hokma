"""Findings on the kit sync and the extensions: hostile repo, symlinks, marketplace sources."""
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from helpers import before
from hokma.core import config
from hokma.kit import extensions, guard, safefs
from hokma.kit.settings import build_settings, protected_paths
from hokma.kit.sync import sync_kit


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "work").mkdir()
    (tmp_path / "outside").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOKMA_HOME", str(tmp_path / "work"))
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return tmp_path


def git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", *a],
                          capture_output=True, text=True, check=True).stdout


def make_kit(tmp_path, files):
    tpl = tmp_path / "tpl"
    for rel, text in files.items():
        p = tpl / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tpl


def script(path: Path, marker: Path) -> None:
    path.write_text(f"#!/bin/sh\necho ran >> {marker}\nexit 0\n")
    path.chmod(0o755)


# ---- 1. Hokma never runs git on the student's repo ---------------------------------------------

def forbid_git(monkeypatch):
    """From now on any attempt to start a process named git fails the test and is recorded."""
    calls = []
    real_popen = subprocess.Popen

    class Spy(real_popen):
        def __init__(self, args, *a, **k):
            first = args if isinstance(args, str) else (args[0] if args else "")
            if os.path.basename(str(first)) in ("git", "git.exe"):
                calls.append(args)
                raise AssertionError(f"git was invoked: {args}")
            super().__init__(args, *a, **k)

    monkeypatch.setattr(subprocess, "Popen", Spy)
    return calls


def tree_state(root: Path):
    return sorted((str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime_ns)
                  for p in root.rglob("*") if p.is_file())


def test_hostile_repo_never_runs_git_during_kit_sync(env, monkeypatch):
    work, marker = env / "work", env / "outside" / "PWNED"
    git(work, "init", "-q")
    git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "base")
    for hook in ("pre-commit", "commit-msg", "post-commit", "reference-transaction", "post-index-change"):
        script(work / ".git" / "hooks" / hook, marker)
    script(env / "outside" / "x.sh", marker)
    with open(work / ".git" / "config", "a") as f:
        f.write(f'[core]\n\tfsmonitor = {env}/outside/x.sh\n\tsshCommand = {env}/outside/x.sh\n'
                f'\thooksPath = {work}/.git/hooks\n[hook "pre-commit"]\n\tcommand = {env}/outside/x.sh\n'
                f'[include]\n\tpath = {env}/outside/evil.cfg\n[includeIf "gitdir:{work}/"]\n\tpath = {env}/outside/evil.cfg\n'
                f'[filter "x"]\n\tclean = {env}/outside/x.sh\n\tsmudge = {env}/outside/x.sh\n'
                f'[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = {env}/outside/x.sh\n')
    (work / ".gitattributes").write_text("* filter=x\n")
    before = tree_state(work / ".git")
    head = (work / ".git" / "HEAD").read_text()

    calls = forbid_git(monkeypatch)
    tpl = make_kit(env, {"CLAUDE.md": "kit v1\n", "agents/base.md": "a1\n"})
    r1 = sync_kit(tpl, version="1.0.0", workdir=work)
    (tpl / "CLAUDE.md").write_text("kit v2\n")
    (tpl / "new.md").write_text("n\n")
    (work / "CLAUDE.md").write_text("kit v1\nmine\n")
    r2 = sync_kit(tpl, version="2.0.0", workdir=work)

    assert calls == [] and not marker.exists()
    assert sorted(r1.added) == ["CLAUDE.md", "agents/base.md"] and r2.added == ["new.md"] and r2.conflicts == ["CLAUDE.md"]
    assert (work / "CLAUDE.md").read_text() == "kit v1\nmine\n" and (work / "CLAUDE.md.hokma-new").read_text() == "kit v2\n"
    assert tree_state(work / ".git") == before and (work / ".git" / "HEAD").read_text() == head, ".git was touched"


@pytest.mark.parametrize("kind", ["symlink", "gitdir-file", "commondir", "alternates"])
def test_redirected_or_borrowed_git_dirs_are_never_touched(env, monkeypatch, kind):
    work, other = env / "work", env / "outside" / "evil.git"
    subprocess.run(["git", "init", "-q", "--bare", str(other)], check=True)
    if kind == "symlink":
        (work / ".git").symlink_to(other)
    elif kind == "gitdir-file":
        (work / ".git").write_text(f"gitdir: {other}\n")
    else:
        git(work, "init", "-q")
        if kind == "commondir":
            (work / ".git" / "commondir").write_text(f"{other}\n")
        else:
            (work / ".git" / "objects" / "info").mkdir(parents=True, exist_ok=True)
            (work / ".git" / "objects" / "info" / "alternates").write_text(f"{other / 'objects'}\n")
    before = tree_state(other)
    calls = forbid_git(monkeypatch)
    r = sync_kit(make_kit(env, {"CLAUDE.md": "kit\n"}), version="1.0.0", workdir=work)
    assert calls == [] and r.added == ["CLAUDE.md"]
    assert tree_state(other) == before


def test_kit_sync_has_no_git_code_left(env):
    import importlib.util

    import hokma.kit.sync as sync_module
    assert importlib.util.find_spec("hokma.kit.safegit") is None
    assert not hasattr(sync_module, "subprocess") and not hasattr(sync_module, "safegit")


def test_report_of_what_changed_is_kept_in_the_protected_data_dir(env):
    work = env / "work"
    tpl = make_kit(env, {"CLAUDE.md": "kit v1\n"})
    sync_kit(tpl, version="1.0.0", workdir=work)
    (tpl / "CLAUDE.md").write_text("kit v2\n")
    (work / "CLAUDE.md").write_text("mine\n")
    sync_kit(tpl, version="2.0.0", workdir=work)
    report = json.loads(config.kit_report_path().read_text())
    assert report["version"] == "2.0.0" and report["conflicts"] == ["CLAUDE.md"] and report["at"]
    assert config.hokma_data_dir() in config.kit_report_path().parents
    assert not (work / "kit-report.json").exists() and not (work / ".hokma" / "kit-report.json").exists()


# ---- 2. symlinks are never followed -------------------------------------------------------

def test_safefs_rejects_symlinked_target_parent_and_escape(env):
    work, outside = env / "work", env / "outside"
    (outside / "victim.txt").write_text("keep\n")
    (work / "link.txt").symlink_to(outside / "victim.txt")
    (work / "linkdir").symlink_to(outside)
    with pytest.raises(safefs.UnsafePath):
        safefs.write_atomic(work, "link.txt", b"pwn")
    with pytest.raises(safefs.UnsafePath):
        safefs.write_atomic(work, "linkdir/new.txt", b"pwn")
    with pytest.raises(safefs.UnsafePath):
        safefs.write_atomic(work, "../outside/new.txt", b"pwn")
    with pytest.raises(safefs.UnsafePath):
        safefs.read_regular(work, "link.txt")
    assert (outside / "victim.txt").read_text() == "keep\n" and not (outside / "new.txt").exists()
    safefs.write_atomic(work, "a/b/ok.txt", b"fine", 0o644)             # normal writes still work
    assert (work / "a" / "b" / "ok.txt").read_bytes() == b"fine"
    safefs.write_atomic(work, "a/b/ok.txt", b"again")                   # replace
    assert (work / "a" / "b" / "ok.txt").read_bytes() == b"again"
    assert not [p for p in (work / "a" / "b").iterdir() if "tmp" in p.name]


def test_kit_sync_does_not_write_through_symlinks(env):
    work, outside = env / "work", env / "outside"
    tpl = make_kit(env, {"CLAUDE.md": "kit v1\n", "agents/base.md": "a1\n", "notes/n.md": "n1\n"})
    sync_kit(tpl, version="1.0.0", workdir=work)

    # agent swaps files/folders for symlinks that point outside
    (outside / "victim.md").write_text("victim\n")
    (work / "CLAUDE.md").unlink()
    (work / "CLAUDE.md").symlink_to(outside / "victim.md")
    for p in (work / "agents" / "base.md",):
        p.unlink()
    (work / "agents" / "base.md.hokma-new").symlink_to(outside / "side.md")      # for a future conflict
    (work / "notes").rename(work / "notes-real")
    (work / "notes").symlink_to(outside)

    (tpl / "CLAUDE.md").write_text("kit v2\n")
    (tpl / "agents" / "base.md").write_text("a2\n")
    (tpl / "notes" / "n.md").write_text("n2\n")
    (tpl / "extra.md").write_text("x\n")
    r = sync_kit(tpl, version="2.0.0", workdir=work)

    assert (outside / "victim.md").read_text() == "victim\n"
    assert sorted(os.listdir(outside)) == ["victim.md"], os.listdir(outside)      # nothing created outside
    assert any("CLAUDE.md" in n for n in r.notes) and any("notes/n.md" in n for n in r.notes)
    assert (work / "extra.md").read_text() == "x\n"                                # safe files still sync


def test_extensions_mirror_refuses_a_symlinked_hokma_dir(env):
    work, outside = env / "work", env / "outside"
    (work / ".hokma").symlink_to(outside)
    warnings = extensions.set_plugin("p@m", True)
    assert warnings and before("ext.mirror_failed", "error") in warnings[0]
    assert sorted(os.listdir(outside)) == []                                       # nothing written outside
    assert extensions.load()[0]["enabledPlugins"] == {"p@m": True}                 # authoritative copy still saved


# ---- 3. marketplace sources and the authoritative copy ----------------------------------------

def test_directory_and_file_sources_inside_workdir_or_tmp_are_rejected(env, tmp_path):
    work = env / "work"
    for source in ({"source": "directory", "path": str(work / "plugins")},
                   {"source": "file", "path": str(work / "m.json")},
                   {"source": "directory", "path": "/tmp/anything"},
                   {"source": "directory", "path": "relative/dir"},
                   {"source": "directory", "path": "$HOME/x"}):
        with pytest.raises(ValueError):
            extensions.validate_source(source)
    link = env / "outside" / "looks-fine"
    link.symlink_to(work)                                                           # outside name, workdir inside
    with pytest.raises(ValueError):
        extensions.validate_source({"source": "directory", "path": str(link)})
    ok = extensions.validate_source({"source": "directory", "path": "/usr/share/hokma-market", "extra": "dropped"})
    assert ok == {"source": "directory", "path": "/usr/share/hokma-market"}


@pytest.mark.parametrize("source", [
    {"source": "github", "repo": "--upload-pack=evil/x"},
    {"source": "github", "repo": "a/b", "ref": "-oProxyCommand=x"},
    {"source": "git", "url": "ext::sh -c touch /tmp/x"},
    {"source": "git", "url": "http://insecure.test/r.git"},
    {"source": "url", "url": "http://insecure.test/m.json"},
    {"source": "url", "url": "https://ok.test/m.json x"},
    {"source": "shell", "cmd": "rm"},
    "not a dict",
])
def test_bad_sources_are_rejected(source):
    with pytest.raises(ValueError):
        extensions.validate_source(source)


def test_sources_are_revalidated_at_load_time(env):
    data = env / "data"
    data.mkdir()
    (data / "extensions.json").write_text(json.dumps({"extraKnownMarketplaces": {
        "inside": {"source": {"source": "directory", "path": str(env / "work" / "evil")}},
        "good": {"source": {"source": "github", "repo": "o/r", "junk": 1}}}}))
    clean, problems = extensions.load()
    assert list(clean["extraKnownMarketplaces"]) == ["good"] and clean["extraKnownMarketplaces"]["good"] == {
        "source": {"source": "github", "repo": "o/r"}}
    assert any("inside" in p for p in problems)
    assert list(build_settings()["extraKnownMarketplaces"]) == ["good"]


def test_workdir_copy_is_informational_only(env):
    extensions.set_plugin("real@m", True)
    mirror = config.extensions_mirror_path()
    assert json.loads(mirror.read_text())["enabledPlugins"] == {"real@m": True}
    mirror.write_text(json.dumps({"enabledPlugins": {"agent-added@m": True}}))      # tampered mirror
    assert build_settings()["enabledPlugins"] == {"real@m": True}                   # never read for settings
    assert extensions.mirror_differs() is True


def test_whole_hokma_folder_is_protected_from_the_agent(env):
    work = env / "work"
    assert config.workdir_hokma_dir() in protected_paths()
    protect = [str(p) for p in protected_paths()]
    for rel in (".hokma/extensions.json", ".hokma/anything-new.json", ".hokma/sub/x"):
        payload = {"tool_name": "Write", "tool_input": {"file_path": str(work / rel)}, "cwd": str(work)}
        assert guard.decide(payload, protect)[0] == 2, rel
    assert str(work / ".hokma") in build_settings()["sandbox"]["filesystem"]["denyWrite"]
    assert guard.decide({"tool_name": "Write", "tool_input": {"file_path": str(work / "notes.md")},
                         "cwd": str(work)}, protect)[0] == 0

"""E2E: install, work as a student, update, and the failure modes. Enable with HOKMA_E2E=1 (network)."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

from helpers import before, doctor_line, t
from ptyutil import run_tty

ROOT = Path(__file__).resolve().parents[2]


def _bump(version: str, patch: int) -> str:
    major, minor, current = version.split(".")
    return f"{major}.{minor}.{int(current) + patch}"


BASE = re.search(r'__version__ = "([^"]+)"', (ROOT / "backend" / "hokma" / "__init__.py").read_text()).group(1)
NEXT, NEXT2 = _bump(BASE, 1), _bump(BASE, 2)      # target versions derive from the checkout: no literals
pytestmark = pytest.mark.skipif(os.environ.get("HOKMA_E2E") != "1" or sys.platform == "win32",
                                reason="set HOKMA_E2E=1 (Linux/macOS, network) to run")


def _env(home: Path, **extra) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("HOKMA_", "UV_", "CLAUDE_CONFIG", "XDG_")) and k not in ("PYTHONPATH", "PYTHONHOME")}
    env.update(HOME=str(home), SHELL="/bin/bash", **extra)
    return env


def run(args, env, **kw):
    return subprocess.run([str(a) for a in args], env=env, capture_output=True, text=True, timeout=900, **kw)


class Box:
    def __init__(self, tmp_path):
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.env = _env(self.home)
        self.data = self.home / ".local" / "share" / "hokma"
        self.work = self.home / "Hokma"
        self.hokma = self.home / ".local" / "bin" / "hokma"
        self.tmp = tmp_path
        r = run(["bash", ROOT / "installer" / "install.sh"], self.env)
        assert r.returncode == 0, r.stdout + r.stderr

    @property
    def slot(self) -> str:
        return (self.data / "current").read_text().strip()

    def version(self) -> str:
        return run([self.hokma, "version"], self.env).stdout.strip()

    def release(self, version, *, mutate=None, break_lock=False, bad_sha=False) -> Path:
        """Build a release archive from this checkout, with a version bump and optional kit changes."""
        src = self.tmp / f"rel-{version}" / f"hokma-{version}"
        shutil.copytree(ROOT, src, ignore=shutil.ignore_patterns(".git", ".venv", "node_modules", "__pycache__",
                                                                  ".pytest_cache", "*.egg-info"))
        init = src / "backend" / "hokma" / "__init__.py"
        init.write_text(init.read_text().replace(f'"{BASE}"', f'"{version}"'))
        if mutate:
            mutate(src)
        if break_lock:
            (src / "uv.lock").write_text("this is not a lock file\n")
        pkg = self.tmp / f"hokma-{version}.tar.gz"
        with tarfile.open(pkg, "w:gz") as t:
            t.add(src, arcname=src.name)
        sha = "0" * 64 if bad_sha else hashlib.sha256(pkg.read_bytes()).hexdigest()
        manifest = self.tmp / f"manifest-{version}.json"
        manifest.write_text(json.dumps({"version": version, "url": pkg.name, "sha256": sha}))
        return manifest

    def update(self, manifest):
        return run([self.hokma, "update", "--from", manifest], self.env)


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


def _new_kit(src: Path) -> None:
    (src / "kit" / "template" / "NEW-KIT.md").write_text(f"new in {NEXT}\n")
    (src / "kit" / "template" / "CLAUDE.md").write_text("# Hokma base CLAUDE.md v2\n")


def test_update_keeps_student_agent_plugin_mcp_and_kit_edits(box):
    # 1. the student works: own agent, edits a kit file, enables a plugin, adds an MCP server
    (box.work / "agents").mkdir(exist_ok=True)
    (box.work / "agents" / "mi-agente.md").write_text("# mi agente\n")
    claude_md = box.work / "CLAUDE.md"
    claude_md.write_text(claude_md.read_text() + "\nmis reglas\n")
    assert run_tty([box.hokma, "ext", "enable", "tools@market"], box.env)[0] == 0
    assert run_tty([box.hokma, "ext", "add-marketplace", "market", "--github", "owner/repo"], box.env)[0] == 0
    cfg = box.data / "claude-config"
    have_claude = shutil.which("claude") is not None
    if have_claude:
        r = run(["claude", "mcp", "add", "-s", "user", "probe", "--", "echo", "hi"],
                {**box.env, "CLAUDE_CONFIG_DIR": str(cfg)}, cwd=box.work)
        assert r.returncode == 0, r.stdout + r.stderr
    run([box.hokma, "prepare-claude"], box.env)
    before = json.loads((cfg / "settings.json").read_text())
    assert before["enabledPlugins"] == {"tools@market": True} and "market" in before["extraKnownMarketplaces"]
    if have_claude:
        assert "probe" in json.loads((cfg / ".claude.json").read_text())["mcpServers"], "MCP is stored in .claude.json"
    old_slot, old_version = box.slot, box.version()

    # 2. update to the next version (new kit file + changed kit file)
    r = box.update(box.release(NEXT, mutate=_new_kit))
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"{BASE} a {NEXT}" in r.stdout and "CLAUDE.md.hokma-new" in r.stdout and "NEW-KIT.md" in r.stdout
    assert box.slot != old_slot and old_version == f"hokma {BASE}" and box.version() == f"hokma {NEXT}"

    # 3. everything of the student is still there
    assert (box.work / "agents" / "mi-agente.md").read_text() == "# mi agente\n"
    assert "mis reglas" in claude_md.read_text()                                   # edited kit file NOT overwritten
    assert (box.work / "CLAUDE.md.hokma-new").read_text() == "# Hokma base CLAUDE.md v2\n"
    assert (box.work / "NEW-KIT.md").read_text() == f"new in {NEXT}\n"
    report = json.loads((box.data / "kit-report.json").read_text())                  # protected data dir, not the workdir
    assert report["version"] == NEXT and report["added"] == ["NEW-KIT.md"] and report["conflicts"] == ["CLAUDE.md"]
    history = run(["git", "-C", box.work, "log", "--oneline"], box.env).stdout.strip().splitlines()
    assert len(history) == 1 and "initial kit" in history[0], history           # Hokma never commits on updates
    assert json.loads((box.work / ".hokma" / "extensions.json").read_text())["enabledPlugins"] == {"tools@market": True}

    # 4. settings were regenerated for the new runtime and kept plugin + locks; MCP survived
    after = json.loads((cfg / "settings.json").read_text())
    assert after["enabledPlugins"] == {"tools@market": True} and "market" in after["extraKnownMarketplaces"]
    assert after["sandbox"]["filesystem"]["denyWrite"] and after["hooks"]["PreToolUse"] and after["permissions"]["deny"]
    assert f"runtime/{box.slot}/app/backend/hokma/kit/guard.py" in after["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    run([box.hokma, "prepare-claude"], box.env)
    if have_claude:
        assert "probe" in json.loads((cfg / ".claude.json").read_text())["mcpServers"], "regeneration must not delete MCP"
    d = run([box.hokma, "doctor"], box.env)
    assert d.returncode == 0 and t("cli.tag.fail") not in d.stdout, d.stdout
    assert doctor_line("ok", "doctor.integrity.name") in d.stdout and doctor_line("ok", "doctor.locks.name") in d.stdout


def test_bad_hash_changes_nothing(box):
    (box.work / "marker.txt").write_text("mine\n")
    slot, version = box.slot, box.version()
    r = box.update(box.release(NEXT, bad_sha=True, mutate=_new_kit))
    assert r.returncode == 1 and t("cli.update.failed", error=t("update.package_sha_mismatch"), version=version.split()[1]) in r.stderr
    assert box.slot == slot and box.version() == version
    assert not (box.data / "runtime" / ("b" if slot == "a" else "a")).exists()
    assert not list((box.data / "tmp").glob("update-*")), "temp files must be cleaned"
    assert not (box.work / "NEW-KIT.md").exists() and (box.work / "marker.txt").read_text() == "mine\n"
    assert run([box.hokma, "doctor"], box.env).returncode == 0


def test_failed_update_keeps_previous_version_and_next_one_works(box):
    slot, version = box.slot, box.version()
    r = box.update(box.release(NEXT, break_lock=True))
    assert r.returncode == 1 and before("cli.update.failed", "error", "version") in r.stderr
    assert box.slot == slot and box.version() == version
    assert not (box.data / "runtime" / ("b" if slot == "a" else "a")).exists()
    assert run([box.hokma, "doctor"], box.env).returncode == 0
    ok = box.update(box.release(NEXT2))
    assert ok.returncode == 0 and box.version() == f"hokma {NEXT2}" and box.slot != slot


def test_interrupted_update_leftovers_do_not_break_the_active_version(box):
    slot, version = box.slot, box.version()
    other = "b" if slot == "a" else "a"
    # what a crash mid-update leaves behind: a half-built slot, a temp pointer, a stale lock
    half = box.data / "runtime" / other / "app" / "backend"
    half.mkdir(parents=True)
    (half / "garbage.py").write_text("half copied")
    (box.data / "current.tmp").write_text(other + "\n")
    lock = box.data / "update.lock"
    lock.write_text("99999")
    old = time.time() - 3600
    os.utime(lock, (old, old))

    assert box.version() == version and box.slot == slot                      # active version unaffected
    d = run([box.hokma, "doctor"], box.env)
    assert d.returncode == 0 and t("cli.tag.fail") not in d.stdout

    r = box.update(box.release(NEXT))                                      # recovers: wipes the leftovers
    assert r.returncode == 0, r.stdout + r.stderr
    assert box.slot == other and box.version() == f"hokma {NEXT}"
    assert not (box.data / "runtime" / other / "app" / "backend" / "garbage.py").exists()
    assert not lock.exists()


def test_concurrent_update_is_refused_and_same_version_is_a_noop(box):
    version = box.version()
    (box.data / "update.lock").write_text("1")                                # fresh lock = update in progress
    r = box.update(box.release(NEXT))
    assert r.returncode == 1 and t("update.in_progress") in r.stderr and box.version() == version
    (box.data / "update.lock").unlink()
    same = box.update(box.release(BASE))
    assert same.returncode == 0 and t("cli.update.up_to_date", version=version.split()[1]) in same.stdout
    assert box.version() == version

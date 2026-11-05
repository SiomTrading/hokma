"""End-to-end installer test on Linux/macOS with a temporary HOME.

Needs network (uv download, managed Python, PyPI). Enable with HOKMA_E2E=1.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import before, doctor_line, installer_before, installer_message
from hokma.i18n import t

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(os.environ.get("HOKMA_E2E") != "1" or sys.platform == "win32",
                                reason="set HOKMA_E2E=1 (Linux/macOS, network) to run")


def _env(home: Path, **extra) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("HOKMA_", "UV_", "CLAUDE_CONFIG", "XDG_")) and k not in ("PYTHONPATH", "PYTHONHOME")}
    env.update(HOME=str(home), SHELL="/bin/bash", **extra)
    return env


def _run(args, env, **kw):
    return subprocess.run(args, env=env, capture_output=True, text=True, timeout=900, **kw)


def _data_dir(home: Path) -> Path:
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Hokma"
    return home / ".local" / "share" / "hokma"


def _full_cycle(home: Path):
    home.mkdir(parents=True)
    work = home / "Hokma"
    data = _data_dir(home)
    env = _env(home)

    r = _run(["bash", str(ROOT / "installer" / "install.sh")], env)
    assert r.returncode == 0, r.stdout + r.stderr

    launcher = home / ".local" / "bin" / "hokma"
    assert launcher.is_file()
    assert (work / ".git").is_dir()
    assert (work / "CLAUDE.md").is_file()
    assert "# >>> hokma >>>" in (home / ".bashrc").read_text()
    assert not (home / ".claude").exists(), "installer must not touch ~/.claude"

    # Runtime and Claude config live in the data dir, never inside the working folder.
    for sub in ("runtime", "bin", "claude-config"):
        assert (data / sub).is_dir(), sub
    assert sorted(p.name for p in (work / ".hokma").iterdir()) == ["extensions.json"]   # student file only, no runtime
    assert not (work / ".claude-config").exists()
    assert not any(p.name in ("venv", "app", "claude-config") for p in work.rglob("*") if ".git" not in p.parts)
    assert work.resolve() not in data.resolve().parents and data.resolve() not in work.resolve().parents

    # settings.json denies Edit/Write/Bash on the runtime and the config.
    deny = json.loads((data / "claude-config" / "settings.json").read_text())["permissions"]["deny"]
    assert any(d.startswith("Edit(//") and "hokma" in d.lower() for d in deny)
    assert any(d.startswith("Write(//") for d in deny) and any(d.startswith("Bash(") for d in deny)

    # Doctor through the launcher: nothing may FAIL (claude missing/no session is a warning).
    d = _run([str(launcher), "doctor"], env)
    assert d.returncode == 0, d.stdout + d.stderr
    assert t("cli.tag.fail") not in d.stdout
    assert f"{t('cli.tag.ok')} uv" in d.stdout and doctor_line("ok", "doctor.dashboard.name") in d.stdout
    assert doctor_line("ok", "doctor.outside.name") in d.stdout
    assert doctor_line("ok", "doctor.isolation.name") in d.stdout
    assert doctor_line("ok", "doctor.locks.name") in d.stdout

    # Doctor must FAIL if the config is forced inside the workdir.
    bad = _run([str(data / "runtime" / (data / "current").read_text().strip() / "venv" / "bin" / "hokma"), "doctor"], _env(home, HOKMA_DATA_DIR=str(work / "inside"),
                                                CLAUDE_CONFIG_DIR=str(work / "inside" / "claude-config")))
    assert bad.returncode == 1 and doctor_line("fail", "doctor.outside.name") in bad.stdout, bad.stdout

    v = _run([str(launcher), "version"], env)
    assert v.stdout.startswith("hokma ")

    # Uninstall without --purge keeps the working folder and the Claude session.
    u = _run(["bash", str(ROOT / "installer" / "uninstall.sh")], env, stdin=subprocess.DEVNULL)
    assert u.returncode == 0, u.stdout + u.stderr
    assert not launcher.exists()
    assert not (data / "runtime").exists() and not (data / "bin").exists()
    assert (data / "claude-config").is_dir()
    assert (work / "CLAUDE.md").is_file(), "working folder must survive uninstall"
    assert "hokma" not in (home / ".bashrc").read_text()
    assert not (home / ".claude").exists()

    # --purge without confirmation (non-interactive, no env) must still keep everything.
    p = _run(["bash", str(ROOT / "installer" / "uninstall.sh"), "--purge"], env, stdin=subprocess.DEVNULL)
    assert p.returncode == 0 and work.exists() and data.exists()
    # --purge with explicit confirmation deletes both.
    p = _run(["bash", str(ROOT / "installer" / "uninstall.sh"), "--purge"],
             _env(home, HOKMA_CONFIRM_PURGE=installer_message("un.purge_word")), stdin=subprocess.DEVNULL)
    assert p.returncode == 0 and not work.exists() and not data.exists()


def test_install_doctor_uninstall(tmp_path):
    _full_cycle(tmp_path / "home")


def test_install_with_accents_and_spaces_in_home(tmp_path):
    """Latin-American usernames: an accent, an enye and a space in the home folder name."""
    _full_cycle(tmp_path / "Jos\u00e9 \u00d1and\u00fa")


def test_installer_refuses_runtime_inside_workdir(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    r = _run(["bash", str(ROOT / "installer" / "install.sh"), "--dir", str(home / "work")],
             _env(home, HOKMA_DATA_DIR=str(home / "work" / "runtime")))
    assert r.returncode != 0 and installer_message("inst.data_inside_work", home / "work" / "runtime", home / "work") in r.stderr
    assert not (home / "work").exists()


def test_bad_checksum_aborts_before_executing(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    lock = json.loads((ROOT / "installer" / "lock.json").read_text())
    for entry in lock["platforms"].values():
        entry["sha256"] = "0" * 64
    bad = tmp_path / "lock.json"
    bad.write_text(json.dumps(lock, indent=2))

    r = _run(["bash", str(ROOT / "installer" / "install.sh")], _env(home, HOKMA_LOCK_FILE=str(bad)))
    assert r.returncode != 0
    assert installer_before("inst.sha_mismatch", 2) in r.stderr
    assert not (_data_dir(home) / "bin" / "uv").exists()
    assert not (home / ".local" / "bin" / "hokma").exists()

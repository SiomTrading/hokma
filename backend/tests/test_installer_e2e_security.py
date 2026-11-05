"""E2E security checks on a real install (Linux/macOS, temp HOME). Enable with HOKMA_E2E=1."""
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


def _env(home: Path) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("HOKMA_", "UV_", "CLAUDE_CONFIG", "XDG_")) and k not in ("PYTHONPATH", "PYTHONHOME")}
    env.update(HOME=str(home), SHELL="/bin/bash")
    return env


def _run(args, env, **kw):
    return subprocess.run(args, env=env, capture_output=True, text=True, timeout=900, **kw)


@pytest.fixture(scope="module")
def install(tmp_path_factory):
    home = tmp_path_factory.mktemp("sec") / "home"
    home.mkdir()
    env = _env(home)
    r = _run(["bash", str(ROOT / "installer" / "install.sh")], env)
    assert r.returncode == 0, r.stdout + r.stderr
    data = home / ".local" / "share" / "hokma"
    return {"home": home, "env": env, "data": data, "work": home / "Hokma",
            "hokma": home / ".local" / "bin" / "hokma",
            "slot": (data / "current").read_text().strip()}


def test_manifest_installed_and_doctor_verifies_it(install):
    assert (install["data"] / "runtime" / install["slot"] / "manifest.json").is_file()
    d = _run([str(install["hokma"]), "doctor"], install["env"])
    assert doctor_line("ok", "doctor.integrity.name") in d.stdout, d.stdout
    assert doctor_line("ok", "doctor.locks.name") in d.stdout, d.stdout


def test_edited_settings_are_restored_on_next_launch(install):
    settings = install["data"] / "claude-config" / "settings.json"
    original = settings.read_text()
    settings.write_text(json.dumps({"permissions": {"allow": ["Bash(*)"]}}))
    # doctor notices the tampering...
    d = _run([str(install["hokma"]), "doctor"], install["env"])
    assert doctor_line("warn", "doctor.locks.name") in d.stdout, d.stdout
    # ...and the next Claude launch regenerates the file from the package template.
    p = _run([str(install["hokma"]), "prepare-claude"], install["env"])
    assert p.returncode == 0, p.stdout + p.stderr
    spec = json.loads(p.stdout)
    assert spec["args"][:2] == ["--settings", str(settings)] and "user" in spec["args"]
    assert settings.read_text() == original


def test_installed_hook_blocks_protected_and_allows_workdir(install):
    settings = json.loads((install["data"] / "claude-config" / "settings.json").read_text())
    cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]

    def fire(path):
        payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(path)}, "cwd": str(install["work"])})
        return _run(["sh", "-c", cmd], install["env"], input=payload).returncode

    assert fire(install["data"] / "claude-config" / "settings.json") == 2
    assert fire(install["data"] / "runtime" / install["slot"] / "app" / "backend" / "hokma" / "__init__.py") == 2
    assert fire(install["work"] / ".." / ".local" / "share" / "hokma" / "claude-config" / "x") == 2   # ..
    assert fire(install["work"] / "notes.md") == 0


def test_tampered_app_file_is_detected_and_refuses_to_start(install):
    target = install["data"] / "runtime" / install["slot"] / "app" / "backend" / "hokma" / "__init__.py"
    original = target.read_text()
    try:
        target.write_text(original + "\n# injected by the agent\n")
        d = _run([str(install["hokma"]), "doctor"], install["env"])
        assert d.returncode == 1 and doctor_line("fail", "doctor.integrity.name") in d.stdout, d.stdout
        assert t("integrity.modified", file="backend/hokma/__init__.py") in d.stdout
        for cmd in (["start", "--no-browser"], ["prepare-claude"], ["init-config"]):
            r = _run([str(install["hokma"]), *cmd], install["env"])
            assert r.returncode == 3 and before("cli.integrity.modified", "detail") in r.stderr, (cmd, r.stdout, r.stderr)
    finally:
        target.write_text(original)
    assert _run([str(install["hokma"]), "doctor"], install["env"]).returncode == 0

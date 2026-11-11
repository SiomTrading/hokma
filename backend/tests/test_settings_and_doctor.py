import json
import os
import subprocess
import sys

import pytest

from hokma.doctor import checks
from hokma.i18n import t
from hokma.kit import guard, integrity
from hokma.kit.launch import IntegrityError, check_integrity, prepare_claude_launch
from hokma.kit.settings import _abs_rule_path, build_settings, guard_command, write_settings


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "work").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOKMA_HOME", str(tmp_path / "work"))
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return tmp_path


def test_abs_rule_path_posix_and_windows():
    from pathlib import PurePosixPath, PureWindowsPath

    assert _abs_rule_path(PurePosixPath("/home/x/.local/share/hokma")) == "//home/x/.local/share/hokma"
    assert _abs_rule_path(PureWindowsPath("C:\\Users\\Jos\u00e9\\AppData\\Local\\Hokma")) == "//C:/Users/Jos\u00e9/AppData/Local/Hokma"


def test_settings_have_sandbox_hook_and_rules(env):
    s = build_settings()
    deny = s["permissions"]["deny"]
    data_rule = str(env / "data").lstrip("/")
    assert f"Edit(//{data_rule}/**)" in deny and f"Write(//{data_rule}/**)" in deny
    assert any(d.startswith("Bash(*") for d in deny)
    sb = s["sandbox"]
    assert sb["enabled"] is True and sb["failIfUnavailable"] is True and sb["allowUnsandboxedCommands"] is False
    assert sb["filesystem"]["allowWrite"] == [str(env / "work"), str(env / "data-requests")]
    assert str(env / "data") in sb["filesystem"]["denyWrite"]
    hook = s["hooks"]["PreToolUse"][0]
    assert hook["matcher"] == "Edit|MultiEdit|Write|NotebookEdit"
    assert hook["hooks"][0]["command"] == guard_command() and guard_command().endswith("|| exit 2")


def test_launch_regenerates_settings_every_time(env):
    spec = prepare_claude_launch()
    path = env / "data" / "claude-config" / "settings.json"
    assert spec.args == ["--settings", str(path), "--setting-sources", "user", "--mcp-config",
                         str(env / "data" / "claude-config" / "hokma-mcp.json")]
    assert spec.env == {"CLAUDE_CONFIG_DIR": str(env / "data" / "claude-config"),
                        "ENABLE_CLAUDEAI_MCP_SERVERS": "false"}     # claude.ai connectors stay out of Hokma
    assert spec.cwd == str(env / "work")
    original = path.read_text()
    # the agent "edits" its settings: drops the hook, the sandbox and the rules
    path.write_text(json.dumps({"permissions": {"allow": ["Bash(*)"]}}))
    prepare_claude_launch()
    assert path.read_text() == original


def test_doctor_checks_every_protected_path(env):
    write_settings()
    assert checks.check_settings_lock().status == checks.OK
    path = env / "data" / "claude-config" / "settings.json"
    s = json.loads(path.read_text())
    s["sandbox"]["filesystem"]["denyWrite"].pop()
    path.write_text(json.dumps(s))
    c = checks.check_settings_lock()
    assert c.status == checks.WARN and "denyWrite" in c.detail


def test_doctor_fails_when_runtime_inside_workdir(env, monkeypatch):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(env / "work" / "runtime"))
    assert checks.check_runtime_outside_workdir().status == checks.FAIL
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(env / "work" / "cfg"))
    assert checks.check_config_isolation().status == checks.FAIL


def test_doctor_ok_when_outside(env):
    assert checks.check_runtime_outside_workdir().status == checks.OK


# ---- guard hook ----------------------------------------------------------------------------

def _decide(tool, path, protect, cwd):
    return guard.decide({"tool_name": tool, "tool_input": {"file_path": str(path)}, "cwd": str(cwd)},
                        [str(p) for p in protect])[0]


def test_guard_blocks_protected_even_through_tricks(tmp_path, monkeypatch):
    data, work = tmp_path / "data", tmp_path / "work"
    (data / "claude-config").mkdir(parents=True)
    work.mkdir()
    (work / "link").symlink_to(data)
    monkeypatch.setenv("HOME", str(tmp_path))
    prot = [data]
    assert _decide("Edit", data / "claude-config" / "settings.json", prot, work) == 2
    assert _decide("Write", work / "link" / "claude-config" / "settings.json", prot, work) == 2   # symlink
    assert _decide("Write", "../data/claude-config/settings.json", prot, work) == 2               # relative
    assert _decide("Write", work / ".." / "data" / "x", prot, work) == 2                          # ..
    assert _decide("Edit", "~/data/x", prot, work) == 2                                           # ~ (HOME=tmp)
    assert _decide("NotebookEdit", data / "n.ipynb", prot, work) == 2
    assert _decide("Edit", work / "notes.md", prot, work) == 0
    assert _decide("Read", data / "x", prot, work) == 0                                           # not a file-editing tool


def test_guard_fails_closed(tmp_path):
    prot = [str(tmp_path / "data")]
    assert guard.decide({"tool_name": "Write", "tool_input": {}}, prot)[0] == 2        # no path
    r = subprocess.run([sys.executable, "-I", str(guard.__file__), "--protect", prot[0]],
                       input="not json", capture_output=True, text=True)
    assert r.returncode == 2
    r = subprocess.run([sys.executable, "-I", str(guard.__file__)], input="{}", capture_output=True, text=True)
    assert r.returncode == 2   # no --protect at all


def test_hook_command_blocks_when_interpreter_is_missing(env):
    cmd = guard_command().replace(sys.executable, "/nonexistent/python")
    r = subprocess.run(["sh", "-c", cmd], input="{}", capture_output=True, text=True)
    assert r.returncode == 2   # a missing interpreter is 127 without `|| exit 2`, which would NOT block


# ---- integrity ----------------------------------------------------------------------------

def test_manifest_detects_tampering(tmp_path, monkeypatch):
    app = tmp_path / "data" / "runtime" / "a" / "app"
    (app / "backend").mkdir(parents=True)
    (app / "backend" / "a.py").write_text("print(1)")
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(integrity, "app_root", lambda: app)
    integrity.write_manifest()
    assert integrity.verify() == []
    (app / "backend" / "a.py").write_text("print(2)")
    assert integrity.verify() == [t("integrity.modified", file="backend/a.py")]
    (app / "backend" / "b.py").write_text("x")
    assert t("integrity.new", file="backend/b.py") in integrity.verify()
    with pytest.raises(IntegrityError):
        check_integrity()
    assert checks.check_integrity().status == checks.FAIL

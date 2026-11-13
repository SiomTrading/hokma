import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hokma.doctor import checks
from hokma.i18n import t
from hokma.kit import guard, integrity
from hokma.kit.launch import IntegrityError, check_integrity, prepare_claude_launch
from hokma.kit.settings import build_settings, guard_command, protected_paths, write_settings


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "work").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOKMA_HOME", str(tmp_path / "work"))
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return tmp_path


def test_settings_have_the_two_hooks_and_nothing_that_stops_installers(env):
    s = build_settings()
    assert "sandbox" not in s and "deny" not in s["permissions"]           # task 3169: no sandbox, no deny list
    assert guard.registration_intact(json.dumps(s), [str(p) for p in protected_paths()])
    hook = s["hooks"]["PreToolUse"][0]
    assert hook["matcher"] == "Edit|MultiEdit|Write|NotebookEdit"
    assert hook["hooks"][0]["command"] == guard_command() and guard_command().endswith("|| exit 2")
    assert s["hooks"]["PreToolUse"][1]["matcher"] == ".*"
    assert s["disableAllHooks"] is False


def test_only_the_hooks_and_their_registration_are_protected(env):
    paths = [str(p) for p in protected_paths()]
    data = env / "data"
    assert str(data / "claude-config" / "settings.json") in paths
    assert str(data / "claude-config" / "settings.engine.json") in paths
    assert str(data / "guard") in paths
    assert not any(guard.under_any(str(data / "claude-config" / "plugins" / "x"), [p]) for p in paths)
    assert not any(guard.under_any(str(data / "extensions.json"), [p]) for p in paths)
    assert not any(guard.under_any(str(env / "work" / ".hokma" / "x"), [p]) for p in paths)
    assert any(guard.under_any(str(guard.__file__), [p]) for p in paths)         # the code of the hooks


def test_launch_regenerates_settings_every_time(env):
    spec = prepare_claude_launch()
    path = env / "data" / "claude-config" / "settings.json"
    assert spec.args == ["--settings", str(path), "--setting-sources", "user", "--mcp-config",
                         str(env / "data" / "claude-config" / "hokma-mcp.json")]
    assert spec.env == {"CLAUDE_CONFIG_DIR": str(env / "data" / "claude-config"),
                        "ENABLE_CLAUDEAI_MCP_SERVERS": "false"}     # claude.ai connectors stay out of Hokma
    assert spec.cwd == str(env / "work")
    original = path.read_text()
    # the agent "edits" its settings and drops the hooks
    path.write_text(json.dumps({"permissions": {"allow": ["Bash(*)"]}}))
    prepare_claude_launch()
    assert path.read_text() == original


def test_doctor_checks_that_both_hooks_are_registered(env):
    write_settings()
    assert checks.check_settings_lock().status == checks.OK
    path = env / "data" / "claude-config" / "settings.json"
    s = json.loads(path.read_text())
    s["hooks"]["PreToolUse"].pop()
    path.write_text(json.dumps(s))
    c = checks.check_settings_lock()
    assert c.status == checks.WARN and "hooks" in c.detail


def test_doctor_reports_missing_tools(env):
    c = checks.check_tools()
    assert c.status == checks.WARN and "node" in c.detail and "bun" in c.detail


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


def test_guard_blocks_the_hooks_even_through_tricks(tmp_path, monkeypatch):
    hooks, work = tmp_path / "hooks", tmp_path / "work"
    hooks.mkdir()
    work.mkdir()
    (hooks / "hardblock.py").write_text("# hook\n")
    (work / "link").symlink_to(hooks)
    monkeypatch.setenv("HOME", str(tmp_path))
    prot = [hooks]
    assert _decide("Edit", hooks / "hardblock.py", prot, work) == 2
    assert _decide("Write", work / "link" / "hardblock.py", prot, work) == 2                      # symlink
    assert _decide("Write", "../hooks/hardblock.py", prot, work) == 2                             # relative
    assert _decide("Write", work / ".." / "hooks" / "x", prot, work) == 2                         # ..
    assert _decide("NotebookEdit", hooks / "n.ipynb", prot, work) == 2
    assert _decide("Edit", work / "notes.md", prot, work) == 0
    assert _decide("Edit", "~/hooks/x", prot, work) == 0              # the tool does not expand `~`: it is a name in `work`
    assert _decide("Read", hooks / "x", prot, work) == 0                                           # not a file-editing tool


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


def test_a_file_the_system_will_not_let_us_read_is_a_named_problem(tmp_path, monkeypatch):
    """Bug 1113: an antivirus blocks a file; compute/verify name it instead of raising OSError. Only code blocks the chat."""
    app = tmp_path / "data" / "runtime" / "a" / "app"
    (app / "backend").mkdir(parents=True)
    (app / "installer").mkdir()
    (app / "backend" / "a.py").write_text("print(1)")
    (app / "installer" / "x.cmd").write_text("rem")
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(integrity, "app_root", lambda: app)
    integrity.write_manifest()
    real = Path.read_bytes

    def blocked(self):
        if self.name in blocked.names:
            raise OSError(22, "Invalid argument")
        return real(self)

    blocked.names = {"x.cmd"}
    monkeypatch.setattr(Path, "read_bytes", blocked)
    assert integrity.verify() == [t("integrity.unreadable", file="installer/x.cmd")]
    check_integrity()                                    # not code Claude runs: the chat still starts
    blocked.names = {"a.py"}
    assert integrity.verify() == [t("integrity.unreadable", file="backend/a.py")]
    with pytest.raises(IntegrityError):
        check_integrity()
    with pytest.raises(OSError):                         # a manifest is never written over a blocked file
        integrity.write_manifest()

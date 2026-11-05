"""E2E on a real install: the agent asks, the student approves in a terminal, the agent cannot approve."""
import json
import os
import sys

import pytest

from helpers import t
from ptyutil import run_tty
from test_installer_e2e_update import Box, run

pytestmark = pytest.mark.skipif(os.environ.get("HOKMA_E2E") != "1" or sys.platform == "win32",
                                reason="set HOKMA_E2E=1 (Linux/macOS, network) to run")

MCP = json.dumps({"command": "/usr/bin/env", "args": ["docs-server"]})


def launch(box):
    p = run([box.hokma, "prepare-claude"], box.env)
    assert p.returncode == 0, p.stdout + p.stderr
    spec = json.loads(p.stdout)
    settings = json.loads((box.data / "claude-config" / "settings.json").read_text())
    mcp = json.loads(open(spec["args"][spec["args"].index("--mcp-config") + 1]).read())["mcpServers"]
    return settings, mcp


def test_request_then_student_approval(tmp_path):
    box = Box(tmp_path)
    inbox = box.home / ".local" / "share" / "hokma-requests"

    # the agent's shell: no terminal. It can ask...
    a = run([box.hokma, "ext", "request", "plugin", "tools@claude-plugins-official"], box.env)
    b = run([box.hokma, "ext", "request", "mcp", "docs", MCP], box.env)
    assert a.returncode == 0 and b.returncode == 0, a.stderr + b.stderr
    ids = {f.stem for f in inbox.glob("*.json")}
    assert len(ids) == 2 and all(i in a.stdout + b.stdout for i in ids)
    settings, mcp = launch(box)
    assert "enabledPlugins" not in settings and mcp == {}, "a request alone must not activate anything"

    # ...but it cannot approve, reject, or change extensions itself
    plugin_id = next(i for i in ids if i in a.stdout)
    for argv in (["ext", "approve", plugin_id], ["ext", "reject", plugin_id], ["ext", "enable", "tools@claude-plugins-official"]):
        r = run([box.hokma, *argv], box.env)
        assert r.returncode == 1 and t("cli.ext.need_terminal") in r.stderr, (argv, r.stdout, r.stderr)
    settings, mcp = launch(box)
    assert "enabledPlugins" not in settings and mcp == {}
    assert plugin_id in {f.stem for f in inbox.glob("*.json")}

    # sandbox: the inbox is writable by the agent, the applied state is not
    assert str(inbox) in settings["sandbox"]["filesystem"]["allowWrite"]
    assert str(box.data) in settings["sandbox"]["filesystem"]["denyWrite"]

    # the student, in a real terminal: a wrong confirmation changes nothing...
    code, out = run_tty([box.hokma, "ext", "approve", plugin_id], box.env, text="si\n")
    assert code == 1 and "tools@claude-plugins-official" in out
    assert "enabledPlugins" not in launch(box)[0]
    # ...the exact id applies it
    code, out = run_tty([box.hokma, "ext", "approve", plugin_id], box.env, text=f"{plugin_id}\n")
    assert code == 0, out
    settings, mcp = launch(box)
    assert settings["enabledPlugins"] == {"tools@claude-plugins-official": True}
    assert settings["sandbox"]["enabled"] and settings["hooks"]["PreToolUse"] and settings["permissions"]["deny"]

    # MCP: shown with its exact command, then applied through --mcp-config
    mcp_id = next(i for i in ids if i != plugin_id)
    code, out = run_tty([box.hokma, "ext", "approve", mcp_id], box.env, text=f"{mcp_id}\n")
    assert code == 0 and "/usr/bin/env" in out and "docs-server" in out and t("req.risk_warning") not in out, out
    entry = launch(box)[1]["docs"]
    assert entry["command"].startswith(str(box.data / "runtime")) and entry["args"][-2:] == ["--name", "docs"] and "-I" in entry["args"]
    assert json.loads((box.data / "extensions.json").read_text())["mcpServers"]["docs"]["command"] == "/usr/bin/env"
    assert not list(inbox.glob("*.json"))
    assert "approved" in (box.data / "extension-decisions.jsonl").read_text()
    assert run([box.hokma, "doctor"], box.env).returncode == 0



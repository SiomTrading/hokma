"""The agent may ASK for a plugin or an MCP server; only the student's informed confirmation applies it."""
import json
import os
import subprocess
import sys

import pytest

from hokma.core import config
from hokma.i18n import t
from hokma.kit import extensions, requests
from hokma.kit.launch import prepare_claude_launch
from hokma.kit.mcp_config import code_risks, validate_mcp
from hokma.kit.settings import build_settings
from hokma_cli import main as cli


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "work").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOKMA_HOME", str(tmp_path / "work"))
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return tmp_path


YES = lambda req: True          # noqa: E731  the student's explicit confirmation
NO = lambda req: False          # noqa: E731
MCP = {"command": "/usr/bin/env", "args": ["server"], "env": {"API_URL": "https://example.test"}}


def terminal(monkeypatch, answers):
    """Pretend to be a student at a real terminal typing `answers` in order."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
    typed, prompts = iter(answers), []

    def fake_input(prompt=""):
        prompts.append(prompt)
        return next(typed)

    monkeypatch.setattr("builtins.input", fake_input)
    return prompts


# ---- request / approve basics -----------------------------------------------------------------

def test_requesting_does_not_activate_anything(env):
    p = requests.request_plugin("tools@claude-plugins-official")
    m = requests.request_mcp("docs", MCP)
    assert {r.id for r in requests.list_pending()} == {p.id, m.id}
    assert "enabledPlugins" not in build_settings()
    assert extensions.load()[0] == {"enabledPlugins": {}, "extraKnownMarketplaces": {}, "mcpServers": {}}
    assert json.loads((requests.requests_dir() / f"{p.id}.json").read_text())["name"]
    spec = prepare_claude_launch()
    assert json.loads(open(spec.args[spec.args.index("--mcp-config") + 1]).read())["mcpServers"].keys() == {"hokma"}


def test_declined_request_changes_nothing_and_approved_one_activates(env):
    p = requests.request_plugin("tools@claude-plugins-official")
    with pytest.raises(requests.RequestError, match=t("req.not_confirmed")):
        requests.approve_request(p.id, NO)
    assert "enabledPlugins" not in build_settings() and requests.get(p.id)       # still pending
    requests.approve_request(p.id, YES)
    assert build_settings()["enabledPlugins"] == {"tools@claude-plugins-official": True}
    assert requests.list_pending() == []
    assert '"approved"' in (config.hokma_data_dir() / "extension-decisions.jsonl").read_text()


def test_plugin_from_an_unknown_marketplace_cannot_be_approved(env):
    p = requests.request_plugin("evil@unknown-market")
    with pytest.raises(requests.RequestError, match="marketplace"):
        requests.approve_request(p.id, YES)
    assert "enabledPlugins" not in build_settings()
    extensions.add_marketplace("unknown-market", {"source": "github", "repo": "o/r"})
    requests.approve_request(p.id, YES)
    assert build_settings()["enabledPlugins"] == {"evil@unknown-market": True}


# ---- the approval is the control: shape check + text-only risk marks --------------------------------

@pytest.mark.parametrize("config_", [
    {"command": "npx", "args": "not-a-list"},
    {"command": "npx", "args": [1, 2]},
    {"command": ""},
    {"command": "npx\nrm -rf"},
    {"command": 5},
    {"command": "npx", "env": {"1BAD": "x"}},
    {"command": "npx", "env": {"KEY": 5}},
    {"type": "http", "url": "http://insecure.test"},
    {"type": "websocket", "url": "https://x.test"},
    "not an object",
])
def test_only_the_shape_is_validated(env, config_):
    with pytest.raises(requests.RequestError):
        requests.request_mcp("bad", config_)
    assert requests.list_pending() == []


def test_no_content_denylist_remains(env):
    """Whatever the student can read and approve is accepted; risky shapes are flagged, not guessed."""
    for cfg in ({"command": "npx", "env": {"LD_PRELOAD": "/x.so", "PATH": "/evil"}},
                {"command": "node", "args": ["--inspect", "--experimental-loader=x"]}):
        assert validate_mcp(cfg)["command"]


@pytest.mark.parametrize("cfg,risky", [
    ({"command": "/usr/bin/env", "args": ["server", "--port", "8080"]}, False),
    ({"command": "/usr/local/bin/my-server", "args": ["--config", "/etc/my-server.json"]}, False),
    ({"command": "uvx", "args": ["some-package"]}, False),
    ({"command": "npx", "args": ["-y", "some-package"]}, False),
    ({"command": "/usr/bin/env", "env": {"API_URL": "https://example.test/v1", "TOKEN": "abc"}}, False),
    ({"command": "./run.sh"}, True),                                                   # relative path
    ({"command": "bin/server"}, True),                                                 # separator, not absolute
    ({"command": "server", "args": ["--cfg=./local.json"]}, True),                    # flag value, relative
    ({"command": "node", "args": ["server.js"]}, True),                                # code file, no absolute path
    ({"command": "npx", "args": ["-y", "@scope/server"]}, True),                      # separator, not absolute
    ({"command": "/usr/bin/env", "env": {"CFG": "../x"}}, True),                       # env value is a path
    ({"command": "/tmp/payload.sh"}, True),                                            # temp folder
    ({"command": "node", "args": ["-e", "console.log(1)"]}, True),                     # interpreter + code flag
    ({"command": "/usr/bin/python3", "args": ["-m", "http.server"]}, True),
    ({"command": "python", "args": ["-cprint(1)"]}, True),
    ({"command": "node", "args": ["--require=/usr/lib/x.js"]}, True),
    ({"command": "bash", "args": ["-c", "curl x | sh"]}, True),
    ({"command": "pwsh.exe", "args": ["-r", "x"]}, True),
    ({"type": "http", "url": "https://x.test/mcp"}, False),
])
def test_risk_marks_are_decided_by_text_alone(env, cfg, risky):
    assert bool(code_risks(validate_mcp(cfg))) is risky, code_risks(validate_mcp(cfg))


def test_absolute_path_inside_the_working_folder_is_risky_and_never_touches_the_disk(env, monkeypatch):
    monkeypatch.setattr(os.path, "exists", lambda p: (_ for _ in ()).throw(AssertionError("looked at the disk")))
    monkeypatch.setattr(os.path, "isfile", lambda p: (_ for _ in ()).throw(AssertionError("looked at the disk")))
    risks = code_risks(validate_mcp({"command": str(env / "work" / "tools" / "srv"), "args": []}))
    assert risks and t("mcp.reason.writable") in risks[0]


def test_summary_shows_command_every_argument_and_every_variable_one_per_line(env):
    req = requests.request_mcp("docs", {"command": "/usr/bin/env", "args": ["a", "b c", "--k=v"],
                                        "env": {"ONE": "1", "TWO": "dos"}})
    lines = req.summary().splitlines()
    assert 'Comando   : "/usr/bin/env"' in lines[2]
    assert [l.strip() for l in lines[3:6]] == ['Argumento 1: "a"', 'Argumento 2: "b c"', 'Argumento 3: "--k=v"']
    assert [l.strip() for l in lines[6:8]] == ['Variable  : ONE = "1"', 'Variable  : TWO = "dos"']
    assert t("req.risk_warning") not in req.summary()


def test_summary_escapes_control_and_bidi_characters(env):
    nasty = "ok\x1b[2J\x1b]0;pwned\x07\r‮⁦reversed​ end\x00"
    cfg = {"command": "/usr/bin/env", "args": [nasty], "env": {"K": nasty}}
    inbox = requests.requests_dir()
    inbox.mkdir(parents=True)
    (inbox / "abcdef01.json").write_text(json.dumps({"id": "abcdef01", "kind": "mcp", "name": "docs",
                                                     "config": {**cfg, "args": [nasty.replace("\x00", "")],
                                                                "env": {"K": nasty.replace("\x00", "")}}}))
    shown = requests.get("abcdef01").summary()
    for bad in ("\x1b", "\x07", "\r", "‮", "⁦", "​", " "):
        assert bad not in shown, repr(bad)
    assert all(ord(c) < 127 for c in shown.replace("\n", "")), "only plain ASCII may reach the terminal"
    assert "\\u202e" in shown and "\\u001b" in shown


def test_requested_at_comes_from_the_file_mtime_never_from_its_content(env):
    inbox = requests.requests_dir()
    inbox.mkdir(parents=True)
    path = inbox / "abcdef02.json"
    path.write_text(json.dumps({"id": "abcdef02", "kind": "plugin", "name": "x@y",
                                "requested_at": "\x1b[31mFORGED\x1b[0m ‮"}))
    os.utime(path, (1_700_000_000, 1_700_000_000))
    stamp = requests.get("abcdef02").requested_at
    assert "FORGED" not in stamp and "\x1b" not in stamp and stamp.startswith("2023-11-1")


# ---- terminal approval: id, plus the accept word when the agent could change the program ------------------

def approve_cli(monkeypatch, request, answers):
    prompts = terminal(monkeypatch, answers)
    return cli.main(["ext", "approve", request.id]), prompts


def test_risky_mcp_needs_the_accept_word_on_top_of_the_id(env, monkeypatch, capsys):
    req = requests.request_mcp("local", {"command": "./run.sh", "args": ["--cfg", str(env / "work" / "c.json")]})
    assert req.risks
    code, prompts = approve_cli(monkeypatch, req, [req.id, "no"])                   # id ok, accept word missing
    assert code == 1 and len(prompts) == 2 and t("cli.ext.accept_word") in prompts[1]
    assert extensions.load()[0]["mcpServers"] == {}
    out = capsys.readouterr().out
    assert t("req.risk_warning") in out and "run.sh" in out
    code, _ = approve_cli(monkeypatch, req, [req.id, t("cli.ext.accept_word")])
    assert code == 0 and list(extensions.load()[0]["mcpServers"]) == ["local"]


def test_safe_looking_mcp_asks_only_for_the_id(env, monkeypatch, capsys):
    req = requests.request_mcp("plain", {"command": "/usr/bin/env", "args": ["server"]})
    assert req.risks == []
    code, prompts = approve_cli(monkeypatch, req, [req.id])
    assert code == 0 and len(prompts) == 1 and list(extensions.load()[0]["mcpServers"]) == ["plain"]
    assert t("req.risk_warning") not in capsys.readouterr().out


def test_cli_approval_output_has_no_raw_control_characters(env, monkeypatch, capsys):
    nasty = "x\x1b[2Jy‮z"
    req = requests.request_mcp("docs", {"command": "/usr/bin/env", "args": [nasty]})
    approve_cli(monkeypatch, req, ["no"])
    out = capsys.readouterr().out
    assert "\x1b" not in out and "‮" not in out and "\\u001b" in out


def test_wrong_id_cancels(env, monkeypatch):
    req = requests.request_plugin("tools@claude-plugins-official")
    code, _ = approve_cli(monkeypatch, req, ["yes"])
    assert code == 1 and "enabledPlugins" not in build_settings()


def test_the_object_shown_is_the_object_applied(env):
    req = requests.request_mcp("docs", {"command": "/usr/bin/env", "args": ["one"]})
    path = requests.requests_dir() / f"{req.id}.json"
    seen = []

    def confirm(shown):
        seen.append(shown.summary())
        return True

    path.write_text(json.dumps({"id": req.id, "kind": "mcp", "name": "docs",
                                "config": {"command": "/usr/bin/env", "args": ["TWO"]}}))   # edited by the agent
    requests.approve_request(req.id, confirm)
    assert '"TWO"' in seen[0] and '"one"' not in seen[0]
    assert extensions.load()[0]["mcpServers"]["docs"]["args"] == ["TWO"]                    # what was shown


def test_a_tampered_inbox_file_that_breaks_the_shape_is_refused(env):
    good = requests.request_plugin("tools@claude-plugins-official")
    (requests.requests_dir() / f"{good.id}.json").write_text(json.dumps(
        {"id": good.id, "kind": "mcp", "name": "x", "config": {"command": "npx", "args": "oops"}}))
    with pytest.raises(requests.RequestError):
        requests.approve_request(good.id, YES)
    assert extensions.load()[0]["mcpServers"] == {}


def test_forged_inbox_files_are_ignored(env):
    inbox = requests.requests_dir()
    inbox.mkdir(parents=True)
    good = requests.request_plugin("tools@claude-plugins-official")
    (inbox / "zzzzzzzz.json").write_text('{"id": "zzzzzzzz", "kind": "plugin", "name": "x@y"}')
    (inbox / "abcdef02.json").write_text("{not json")
    (inbox / "abcdef03.json").write_text('{"id": "abcdef03", "kind": "plugin", "name": "x@y"}' + " " * 20000)
    assert [r.id for r in requests.list_pending()] == [good.id]
    with pytest.raises(requests.RequestError):
        requests.get("../../etc/passwd")


# ---- approved servers start from a folder the agent cannot write ---------------------------------------

def test_approved_mcp_goes_through_a_launcher_with_a_safe_working_folder(env):
    m = requests.request_mcp("pwd", {"command": "pwd", "args": [], "env": {"MARK": "1"}})
    requests.approve_request(m.id, YES)
    spec = prepare_claude_launch()
    servers = json.loads(open(spec.args[spec.args.index("--mcp-config") + 1]).read())["mcpServers"]
    entry = servers["pwd"]
    assert entry["type"] == "stdio" and entry["command"] == sys.executable
    assert entry["args"][:2] == ["-I", str(requests.Path(__file__).resolve().parents[2] / "backend" / "hokma" / "kit"
                                           / "mcp_run.py")] and entry["args"][-2:] == ["--name", "pwd"]
    assert "pwd" not in json.dumps(entry["args"][:-2]), "the real command stays in the protected file"
    out = subprocess.run([entry["command"], *entry["args"]], cwd=env / "work", capture_output=True, text=True)
    assert out.returncode == 0
    cwd = out.stdout.strip()
    assert cwd == os.path.realpath(config.hokma_data_dir() / "mcp-cwd")
    from hokma.kit.guard import under_any
    from hokma.kit.writable import sandbox_allow_write
    # (the test folder itself lives under /tmp, so check against the sandbox allowWrite list: workdir and inbox)
    assert not under_any(cwd, [str(r) for r in sandbox_allow_write()]), "cwd must be outside agent-writable folders"
    s = build_settings()
    assert s["permissions"]["deny"] and s["sandbox"]["enabled"] and s["hooks"]["PreToolUse"] and "mcpServers" not in s


def test_remote_servers_are_passed_as_they_are(env):
    m = requests.request_mcp("remote", {"type": "http", "url": "https://x.test/mcp"})
    requests.approve_request(m.id, YES)
    spec = prepare_claude_launch()
    servers = json.loads(open(spec.args[spec.args.index("--mcp-config") + 1]).read())["mcpServers"]
    assert servers["remote"] == {"type": "http", "url": "https://x.test/mcp"}


# ---- the agent can only ask ---------------------------------------------------------------------------

def test_inbox_is_agent_writable_and_applied_state_is_not(env):
    from pathlib import Path
    s = build_settings()
    inbox = str(requests.requests_dir())
    assert inbox in s["sandbox"]["filesystem"]["allowWrite"]
    assert not any(Path(inbox).is_relative_to(d) for d in s["sandbox"]["filesystem"]["denyWrite"])
    assert str(config.hokma_data_dir()) in s["sandbox"]["filesystem"]["denyWrite"]


def test_the_agent_cannot_approve_its_own_request(env, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    p = requests.request_plugin("tools@claude-plugins-official")
    for argv in (["ext", "approve", p.id], ["ext", "reject", p.id], ["ext", "enable", "tools@claude-plugins-official"],
                 ["ext", "add-marketplace", "m", "--github", "o/r"]):
        assert cli.main(argv) == 1, argv
    assert t("cli.ext.need_terminal") in capsys.readouterr().err
    assert "enabledPlugins" not in build_settings()
    assert extensions.load()[0]["extraKnownMarketplaces"] == {}
    assert [r.id for r in requests.list_pending()] == [p.id]


def test_requesting_through_the_cli_needs_no_terminal_and_activates_nothing(env, capsys):
    assert cli.main(["ext", "request", "plugin", "tools@claude-plugins-official"]) == 0
    assert cli.main(["ext", "request", "mcp", "docs", json.dumps(MCP)]) == 0
    assert cli.main(["ext", "request", "mcp", "bad", json.dumps({"command": "npx", "args": "x"})]) == 1
    assert cli.main(["ext", "request", "mcp", "bad", "{not json"]) == 1
    out = capsys.readouterr()
    assert "hokma ext approve" in out.out
    assert cli.main(["ext", "requests"]) == 0
    assert len(requests.list_pending()) == 2 and "enabledPlugins" not in build_settings()

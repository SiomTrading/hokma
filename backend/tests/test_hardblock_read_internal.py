"""The agent may READ Hokma's data folder and its claude-config (plugins, installed_plugins.json, settings, logs);
keys, tokens and the database stay hidden; writing, deleting and moving there stays blocked (task 3163)."""
import pytest

from hardblock_support import Box, box  # noqa: F401

CFG = "{DATA}/claude-config"


@pytest.fixture
def full(box):
    plugins = box.data / "claude-config" / "plugins"
    (plugins / "cache" / "claude-mem").mkdir(parents=True)
    (plugins / "installed_plugins.json").write_text('{"plugins": {}}\n')
    (plugins / "cache" / "claude-mem" / "plugin.json").write_text("{}\n")
    (box.data / "logs").mkdir()
    (box.data / "logs" / "engine.log").write_text("started\n")
    (box.data / "hokma.db").write_text("sqlite")
    (box.data / "codex-home").mkdir()
    (box.data / "codex-home" / "auth.json").write_text("{}")
    (box.data / "identity.key").write_text("k")
    (box.data / "acp-home").mkdir()
    (box.data / "anything-new.key").write_text("k")
    (box.data / "backups").mkdir()
    (box.data / "backups" / "hokma-1.db").write_text("x")
    return box


def run(box, command):
    return box.bash(command.replace("{DATA}", str(box.data)))


READ_OK = [
    "ls {DATA}", "ls -la " + CFG, "ls " + CFG + "/plugins", "ls " + CFG + "/plugins/cache/claude-mem",
    "cat " + CFG + "/plugins/installed_plugins.json", "cat " + CFG + "/settings.json", "cat {DATA}/logs/engine.log",
    "tail -n 50 {DATA}/logs/engine.log", "head {DATA}/logs/engine.log", "grep -r claude-mem " + CFG + "/plugins",
    "grep claude-mem " + CFG + "/plugins/installed_plugins.json", "rg mem " + CFG + "/plugins",
    "find " + CFG + "/plugins -name '*.json'", "ls -R " + CFG + "/plugins", "wc -l {DATA}/logs/engine.log",
    "cat " + CFG + "/plugins/installed_plugins.json | grep mem | head -3", "cd " + CFG + "/plugins && ls && cat installed_plugins.json",
    "ls " + CFG + "/plugins 2>/dev/null", "cat " + CFG + "/plugins/*/*.json", "ls {DATA}/*",
    "stat " + CFG + "/plugins/installed_plugins.json", "grep -o mem " + CFG + "/plugins/installed_plugins.json",
    "ls {DATA} 2>&1",
]


@pytest.mark.parametrize("command", READ_OK)
def test_reading_the_data_folder_is_allowed(full, command):
    assert run(full, command) is None, command


def test_the_read_tools_are_allowed_on_plugins_settings_and_logs(full):
    b = full
    assert b.hit("Read", file_path=str(b.data / "claude-config" / "plugins" / "installed_plugins.json")) is None
    assert b.hit("Read", file_path=str(b.data / "claude-config" / "settings.json")) is None
    assert b.hit("Read", file_path=str(b.data / "logs" / "engine.log")) is None
    assert b.hit("Grep", pattern="mem", path=str(b.data / "claude-config" / "plugins")) is None
    assert b.hit("Glob", pattern="*", path=str(b.data / "claude-config")) is None
    assert b.hit("Glob", pattern=str(b.data / "claude-config" / "plugins" / "**" / "*.json")) is None


HIDDEN = [
    ("cat " + CFG + "/.credentials.json", "secrets"), ("cat " + CFG + "/.claude.json", "secrets"),
    ("cat {DATA}/database.json", "secrets"), ("grep token " + CFG + "/.credentials.json", "secrets"),
    ("cat {DATA}/hokma.db", "hokma_internal"), ("cat {DATA}/codex-home/auth.json", "hokma_internal"),
    ("ls {DATA}/codex-home", "hokma_internal"), ("cat {DATA}/backups/hokma-1.db", "hokma_internal"),
    ("strings {DATA}/hokma.db", "hokma_internal"),
    # a search that would walk into the credentials
    ("grep -r token {DATA}", "hokma_internal"), ("grep -r token " + CFG, "hokma_internal"),
    ("rg token " + CFG, "hokma_internal"), ("cat " + CFG + "/.c*", "hokma_internal"),
    ("cat " + CFG + "/.*", "hokma_internal"), ("cat {DATA}/*", "hokma_internal"),
    ("cd " + CFG + " && cat .credentials.json", "hokma_internal"),
    ("cd " + CFG + " && grep -r x .", "hokma_internal"),
    ("cd {DATA} && grep -r x .", "hokma_internal"), ("cd " + CFG + " && grep -r token", "hokma_internal"),
    ("cd " + CFG + " && rg token", "hokma_internal"), ("jq . " + CFG + "/.credentials.json", "secrets"),
    # the control channel and the lock file
    ("cat {DATA}/engine.lock", "hokma_internal"), ("ls {DATA}/control.sock", "hokma_internal"),
    ("cat ~/.ssh/id_ed25519", "secrets"),
    ("cat " + CFG + "/{.credentials,x}.json", "hokma_internal"), ("cat " + CFG + "/backups/.claude.json.backup.1", "hokma_internal"),
    ("cat " + CFG + "/.claude.json.backup", "secrets"),
    ("cat " + CFG + "/hokma-mcp.json", "hokma_internal"), ("cat " + CFG + "/shell-snapshots/x", "hokma_internal"),
    ("cd " + CFG + " && cd - && cat x", "hokma_internal"), ("cd {DATA} && cd claude-config && cat .claude.json", "hokma_internal"),
    ("ls " + CFG + "/*/../.c*", "hokma_internal"), ("cat " + CFG + "/pl*/../.claude.json", "hokma_internal"),
    ("cat ~+/x " + CFG + "/plugins", "hokma_internal"), ("cat ~root/x " + CFG + "/plugins", "hokma_internal"),
    ("cat " + CFG + "/plugins/installed_plugins.json," + CFG + "/.claude.json", "secrets"),
    ("findstr x " + CFG + "/plugins/installed_plugins.json", "hokma_internal"),
    ("cat {DATA}/extensions.json", "hokma_internal"), ("cat {DATA}/identity.key", "hokma_internal"), ("ls {DATA}/acp-home", "hokma_internal"),
    ("cat {DATA}/anything-new.key", "hokma_internal"), ("cat $HOME/.local/share/hokma/identity.key", "hokma_internal"),
    ("cat %LOCALAPPDATA%\\Hokma\\identity.key", "hokma_internal"),
]


@pytest.mark.parametrize("command,rule", HIDDEN)
def test_secrets_and_the_database_stay_hidden(full, command, rule):
    (full.data / "control.sock").write_text("")
    (full.data / "engine.lock").write_text("{}")
    assert run(full, command) == rule, command


def test_a_script_named_like_a_reader_does_not_make_the_line_read_only(full):
    (full.work / "cat").write_text("#!/bin/sh\necho hi > {DATA}/x\n".replace("{DATA}", str(full.data)))
    assert run(full, "./cat {DATA}/logs/engine.log") is not None
    assert run(full, "../work/cat {DATA}/logs/engine.log") is not None


def test_the_read_tools_keep_secrets_hidden(full):
    b = full
    assert b.hit("Read", file_path=str(b.data / "claude-config" / ".credentials.json")) == "secrets"
    assert b.hit("Read", file_path=str(b.data / "database.json")) == "secrets"
    assert b.hit("Read", file_path=str(b.data / "hokma.db")) == "hokma_internal"
    assert b.hit("Grep", pattern="x", path=str(b.data)) == "hokma_internal"
    assert b.hit("Grep", pattern="x", path=str(b.data / "claude-config")) == "hokma_internal"
    assert b.hit("Read", file_path=str(b.data / "codex-home" / "auth.json")) == "secrets"


WRITES = [
    "echo x > {DATA}/claude-config/plugins/installed_plugins.json", "echo x >> {DATA}/claude-config/settings.json",
    "touch {DATA}/claude-config/plugins/new", "mkdir {DATA}/claude-config/plugins/x", "tee {DATA}/logs/x < /dev/null",
    "cp /etc/hostname {DATA}/claude-config/plugins/x", "cp {DATA}/claude-config/settings.json ./settings-copy.json && "
    "mv {DATA}/claude-config/settings.json /tmp/x", "mv {DATA}/claude-config/plugins ./plugins-moved",
    "sed -i s/a/b/ {DATA}/claude-config/settings.json", "ln -s /tmp {DATA}/claude-config/plugins/l",
    "cat {DATA}/logs/engine.log > {DATA}/logs/other.log", "ls {DATA} && touch {DATA}/x",
    "cat {DATA}/logs/engine.log | tee {DATA}/logs/copy.log", "find {DATA}/logs -name x -exec cat {} \;",
    "python3 -c \"open('{DATA}/claude-config/settings.json','w').write('x')\"",
    "chmod 777 {DATA}/claude-config/plugins", "sort -o {DATA}/logs/x {DATA}/logs/engine.log",
    "echo x >& {DATA}/logs/x", "cat {DATA}/logs/engine.log >& {DATA}/logs/x", "rg --pre sh x {DATA}/claude-config/plugins",
    "tree -o {DATA}/logs/x {DATA}/logs", "xxd {DATA}/logs/engine.log {DATA}/logs/x",
    "x={DATA}/claude-config; cat $x/.credentials.json", "cat {DATA}/claude-config/plugins/$UNKNOWN/../../.credentials.json",
    "cat $(echo {DATA}/claude-config/.credentials.json)", "grep --file={DATA}/claude-config/.claude.json x {DATA}/logs",
    "grep --file={DATA}/hokma.db x {DATA}/logs", "grep -fidentity.key x {DATA}", "grep -f{DATA}/identity.key x {DATA}/logs",
    "grep --file={DATA}/claude-config/../identity.key x {DATA}/logs", "rm {DATA}/claude-config/plugins/installed_plugins.json",
    "rm -rf {DATA}/claude-config/plugins", "echo hi | xargs -I{} cat {DATA}/claude-config/.credentials.json",
]


@pytest.mark.parametrize("command", WRITES)
def test_writing_deleting_or_moving_stays_blocked(full, command):
    assert run(full, command) is not None, command


def test_the_write_tools_stay_blocked_inside_the_data_folder(full):
    b = full
    for tool, extra in (("Write", {"content": "x"}), ("Edit", {"old_string": "{}", "new_string": "{ }"})):
        for rel in ("claude-config/settings.json", "claude-config/plugins/installed_plugins.json", "logs/x.log"):
            assert b.hit(tool, file_path=str(b.data / rel), **extra) == "hokma_internal", (tool, rel)


def test_grep_without_a_path_judges_the_folder_it_runs_in(full):
    b = full
    assert b.hit("Grep", cwd=b.data / "claude-config", pattern="token") is not None
    assert b.hit("Grep", pattern="token") is None                   # the working folder


def test_powershell_reads_but_does_not_write(full):
    b = full
    assert b.ps("Get-ChildItem $env:LOCALAPPDATA\\Hokma\\claude-config\\plugins") is None
    assert b.ps("Get-Content $env:LOCALAPPDATA\\Hokma\\claude-config\\plugins\\installed_plugins.json") is None
    assert b.ps("Get-Content $env:LOCALAPPDATA\\Hokma\\claude-config\\.credentials.json") == "secrets"
    assert b.ps("Get-Content $env:LOCALAPPDATA\\Hokma\\hokma.db") == "hokma_internal"
    assert b.ps("Set-Content $env:LOCALAPPDATA\\Hokma\\claude-config\\settings.json 'x'") == "hokma_internal"
    assert b.ps("Remove-Item $env:LOCALAPPDATA\\Hokma\\claude-config\\plugins -Recurse") is not None
    assert b.ps("Move-Item $env:LOCALAPPDATA\\Hokma\\claude-config\\settings.json .") is not None
    assert b.ps("Get-Content $env:LOCALAPPDATA\\Hokma\\claude-config\\settings.json | Set-Content $env:LOCALAPPDATA\\Hokma\\x") is not None


def test_powershell_does_not_pipe_a_listing_into_a_reader(full):
    ps = full.ps
    assert ps("Get-ChildItem $env:LOCALAPPDATA\\Hokma\\claude-config | Get-Content") is not None
    assert ps("Get-ChildItem $env:LOCALAPPDATA\\Hokma\\claude-config -Recurse | Select-String token") is not None
    assert ps("Get-Content $env:LOCALAPPDATA\\Hokma\\claude-config\\plugins\\installed_plugins.json | Select-String mem") is None


def test_powershell_taint_follows_the_whole_pipeline(full):
    ps = full.ps
    assert ps("Get-ChildItem $env:LOCALAPPDATA\\Hokma\\claude-config | Sort-Object | Get-Content") is not None
    assert ps("Get-ChildItem $env:LOCALAPPDATA\\Hokma\\claude-config | Where-Object Name | Select-String x") is not None


def test_cmd_style_reads(full):
    assert full.bash("type %LOCALAPPDATA%\\Hokma\\claude-config\\plugins\\installed_plugins.json") is None
    assert full.bash("dir %LOCALAPPDATA%\\Hokma\\claude-config\\plugins") is None
    assert full.bash("type %LOCALAPPDATA%\\Hokma\\claude-config\\.credentials.json") == "secrets"


def test_the_working_folder_internals_stay_as_they_were(full):
    b = full
    assert b.bash("cat .hokma/extensions.json") == "hokma_internal"
    assert b.hit("Read", file_path=str(b.work / ".hokma" / "extensions.json")) == "hokma_internal"
    assert b.bash("echo x > .claude/settings.json") == "hokma_internal"


def test_the_write_block_tells_the_agent_it_is_changed_from_settings(full):
    b = full
    out = b.run_hook({"tool_name": "Write", "cwd": str(b.work),
                      "tool_input": {"file_path": str(b.data / "claude-config" / "settings.json"), "content": "x"}})
    assert out.returncode == 2 and "HOKMA_BLOCKED hokma_internal" in out.stderr
    assert "Settings" in out.stderr and "read" in out.stderr
    out = b.run_hook({"tool_name": "Bash", "cwd": str(b.work),
                      "tool_input": {"command": f"cat {b.data}/claude-config/plugins/installed_plugins.json"}})
    assert out.returncode == 0 and out.stderr == ""


def test_the_settings_do_not_deny_bash_over_the_data_folder_but_still_deny_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOKMA_HOME", str(tmp_path / "work"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    from hokma.kit import settings

    deny = settings.build_settings()["permissions"]["deny"]
    assert f"Bash(*{tmp_path / 'data'}*)" not in deny and f"Bash(*{tmp_path / 'data' / 'claude-config'}*)" not in deny
    assert f"Bash(*{tmp_path / 'data'}/identity.key*)" in deny and f"Bash(*{tmp_path / 'data'}/codex-home*)" in deny
    assert any(rule.startswith("Write(") and "data" in rule for rule in deny)
    assert any(rule.startswith("Edit(") and "claude-config" in rule or rule.startswith("Edit(") for rule in deny)
    assert any(rule.startswith("Bash(") and ".hokma" in rule for rule in deny)

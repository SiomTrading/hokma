"""The broker gives any engine the same protections the hooks give Claude Code (CA-08, CA-09)."""
import json

import pytest

from hardblock_support import Box, box  # noqa: F401  (fixture)
from hokma.permissions.broker import BrokerCtx, check, file_change_payloads


@pytest.fixture
def bctx(box):
    return BrokerCtx(data=str(box.data), roots=[str(box.work)], protect=[str(box.data)], conversation_id="7")


def test_a_delete_command_is_blocked_and_logged(box, bctx):
    v = check("Bash", {"command": "rm f"}, cwd=str(box.work), ctx=bctx)
    assert (v.allowed, v.rule) == (False, "delete")
    assert v.text == "HOKMA_BLOCKED delete"
    row = json.loads((box.data / "guard" / "blocks.jsonl").read_text().splitlines()[-1])
    assert (row["rule"], row["conversation_id"]) == ("delete", "7")


def test_powershell_delete_is_blocked_in_ps_mode(box, bctx):
    bctx.shell = "PowerShell"
    assert check("run_command", {"command": "Remove-Item f"}, cwd=str(box.work), ctx=bctx).rule == "delete"


@pytest.mark.parametrize("command,rule", [
    ("git push --force origin main", "force_push"),
    ("cat ~/.ssh/id_rsa", "secrets"),
])
def test_hard_rules_on_commands(box, bctx, command, rule):
    assert check("Bash", {"command": command}, cwd=str(box.work), ctx=bctx).rule == rule


def test_write_outside_the_work_folder_and_inside_data(box, bctx, tmp_path):
    out = check("write_file", {"file_path": str(tmp_path / "elsewhere.txt"), "content": "x"}, cwd=str(box.work), ctx=bctx)
    assert not out.allowed
    inner = check("Write", {"file_path": str(box.data / "x.txt"), "content": "x"}, cwd=str(box.work), ctx=bctx)
    assert not inner.allowed and inner.rule == "hokma_internal"


def test_a_normal_write_and_command_pass(box, bctx):
    assert check("Write", {"file_path": str(box.work / "new.txt"), "content": "hi"}, cwd=str(box.work), ctx=bctx).allowed
    assert check("Bash", {"command": "git status"}, cwd=str(box.work), ctx=bctx).allowed


def test_real_money_host_in_fetch_and_mcp_arguments(box, bctx):
    hosts = bctx.hardblock_ctx(str(box.work)).brokers.get("hosts", [])
    assert hosts, "the broker list must not be empty"
    url = f"https://{hosts[0]}/v1/orders"
    assert check("web_fetch", {"url": url}, cwd=str(box.work), ctx=bctx).rule == "real_money"
    assert check("mcp__thing__call", {"endpoint": url}, cwd=str(box.work), ctx=bctx).rule == "real_money"
    assert check("web_fetch", {"url": "https://example.com/"}, cwd=str(box.work), ctx=bctx).allowed


def test_engine_credentials_are_secrets(box, bctx):
    for text in ("cat ~/.codex/auth.json", "cat ~/.gemini/oauth_creds.json", "cat ~/.claude/.credentials.json"):
        assert check("Bash", {"command": text}, cwd=str(box.work), ctx=bctx).rule in ("secrets", "hokma_internal"), text
    assert check("Bash", {"command": "export CODEX_HOME=/tmp/x"}, cwd=str(box.work), ctx=bctx).rule == "hokma_internal"


def test_fails_closed_on_garbage(box, bctx):
    assert not check("Bash", ["not", "a", "dict"], cwd=str(box.work), ctx=bctx).allowed  # type: ignore[arg-type]


def test_file_changes_map_to_requests(box, bctx):
    reqs = file_change_payloads([{"path": str(box.work / "f"), "kind": "delete"},
                                 {"path": str(box.work / "ok.txt"), "kind": "add", "content": "x"}])
    assert [check(t, i, cwd=str(box.work), ctx=bctx).allowed for t, i in reqs] == [False, True]


def test_env_allow_list_drops_every_key_and_token():
    from hokma.engines.envfilter import filtered_env
    out = filtered_env({"PATH": "/bin", "HOME": "/h", "LC_ALL": "C", "https_proxy": "http://p", "OPENAI_API_KEY": "k",
                        "ANTHROPIC_API_KEY": "k", "HOKMA_SESSION_TOKEN": "t", "HOKMA_CONVERSATION_ID": "3",
                        "XDG_DATA_HOME": "/x", "GITHUB_TOKEN": "g"})
    assert set(out) == {"PATH", "HOME", "LC_ALL", "https_proxy", "HOKMA_CONVERSATION_ID", "XDG_DATA_HOME"}


def test_unknown_tools_are_denied_by_default(box, bctx):
    for name in ("totally_new_tool", "ExecShell", "delete_everything"):
        v = check(name, {"x": 1}, cwd=str(box.work), ctx=bctx)
        assert (v.allowed, v.rule) == (False, "hokma_internal"), name
    assert check("BASH", {"command": "rm f"}, cwd=str(box.work), ctx=bctx).rule == "delete"
    assert check("read", {"file_path": str(box.work / "notes.md")}, cwd=str(box.work), ctx=bctx).allowed


def test_file_change_destinations_and_moves_are_checked(box, bctx, tmp_path):
    def blocked(change):
        reqs = file_change_payloads([change])
        return [not check(t, i, cwd=str(box.work), ctx=bctx).allowed for t, i in reqs]
    assert any(blocked({"path": str(box.work / "notes.md"), "kind": "update", "move_path": str(tmp_path / "out.md")}))
    assert any(blocked({"path": str(box.work / "notes.md"), "kind": "update", "new_path": str(box.data / "x")}))
    assert blocked({"path": str(box.work / "notes.md"), "kind": "move", "dest": str(box.work / "b.md")})[0]
    assert blocked({"path": str(box.work / "notes.md"), "kind": "rename"})[0]
    assert blocked({"path": str(box.work / "notes.md"), "kind": "weird"}) == [True]


def test_generic_mcp_cannot_hide_an_order_tool(box, bctx):
    v = check("mcp", {"server": "broker", "tool": "place_order", "arguments": {"qty": 1}}, cwd=str(box.work), ctx=bctx)
    assert (v.allowed, v.rule) == (False, "real_money")
    v = check("mcp", {"server": "ok", "tool": "list_things", "arguments": {"op": "submit_order"}}, cwd=str(box.work),
              ctx=bctx)
    assert (v.allowed, v.rule) == (False, "real_money")
    assert check("mcp", {"server": "ok", "tool": "list_things"}, cwd=str(box.work), ctx=bctx).allowed


def test_ambiguous_generic_mcp_is_denied(box, bctx):
    for tool_input in ({"server": "a", "tool": "b", "name": "place_order"}, {"tool": "x"}, {"server": "a b", "tool": "x"},
                       {"server": "a", "tool": "x/../y"}, {"server": ["a"], "tool": "x"}, {"server": "a", "tool": "x",
                                                                                                "extra": 1}):
        v = check("mcp", tool_input, cwd=str(box.work), ctx=bctx)
        assert (v.allowed, v.rule) == (False, "hokma_internal"), tool_input


def test_any_destination_different_from_path_is_a_move_whatever_the_kind(box, bctx):
    src = str(box.work / "notes.md")
    reqs = file_change_payloads([{"path": src, "kind": "update", "dest": str(box.work / "other.md")}])
    assert reqs[0][0] == "Bash" and reqs[0][1]["command"].startswith("rm ")
    assert not check(*reqs[0], cwd=str(box.work), ctx=bctx).allowed
    assert reqs[1][0] == "Write"
    same = file_change_payloads([{"path": src, "kind": "update", "dest": src}])
    assert same[0][0] == "Write"


def test_the_key_helper_cannot_be_called_by_an_agent(box, bctx):
    for command in ("python3 -I /x/hokma/kit/engine_key.py --conn 3", "python engine_key.py --conn 1 --var ANTHROPIC_API_KEY"):
        assert check("Bash", {"command": command}, cwd=str(box.work), ctx=bctx).rule in ("secrets", "hokma_internal")
    assert check("web_fetch", {"url": "http://x/engine_key.py"}, cwd=str(box.work), ctx=bctx).rule == "secrets"


def test_engine_key_as_a_bare_word_is_blocked_in_every_form(box, bctx):
    commands = ("python -m hokma.kit.engine_key --conn 2", "python /x/engine_key* --conn 2",
                "python -c \"import hokma.kit.engine_key as k; k.main(['--conn','2'])\"")
    for command in commands:
        assert check("Bash", {"command": command}, cwd=str(box.work), ctx=bctx).rule in ("secrets", "hokma_internal"), command
    bctx.shell = "PowerShell"
    assert check("run_command", {"command": "python -m hokma.kit.engine_key --conn 2"}, cwd=str(box.work), ctx=bctx).rule == "secrets"

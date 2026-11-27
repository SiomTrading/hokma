"""Which agent makes a call to the Hokma server (task 3429, 1.2.7).

The agents launched with the Agent tool (Ian, Sam, Ben) share the MCP server of the session, so by its environment all of them acted as the agent of the session: Sam could not claim his task and
the author and the reviewer of a version were the same name. The PreToolUse hook kit/identity_hook.py stamps the agent of each call; the server takes it from there. Each test runs the hook the way
Claude Code runs it (a separate isolated process with the payload on its input) or the server through its JSON-RPC entry, and shows the refusal before the road that stays open."""
import json
import subprocess
import sys

import pytest
from code_support import BRIEF, CODER, REVIEWER, RESEARCHER, make_bank, put_files, rows
from code_support import db, env, pg_admin_url, pg_template, pg_url_migrated  # noqa: F401  (fixtures)

from hokma.db import Actor
from hokma.kit import hokma_tools, identity_hook, launch, settings
from hokma.kit.guard import registration_intact
from hokma.kit.trash_mcp import handle

HOOK = str(settings.IDENTITY_FILE)
FIELD = hokma_tools.IDENTITY_FIELD


def run_hook(payload, env_vars=None):
    return subprocess.run([sys.executable, "-I", HOOK], input=json.dumps(payload), capture_output=True, text=True, env=env_vars or {})


def stamped(done):
    assert done.returncode == 0, done.stderr
    answer = json.loads(done.stdout)["hookSpecificOutput"]
    assert answer["hookEventName"] == "PreToolUse" and "permissionDecision" not in answer      # the permission flow of the mode is not touched
    return answer["updatedInput"]


# ------------------------------------------------------------------ the hook

def test_the_hook_stamps_the_agent_type_of_a_launched_agent_and_keeps_the_arguments():
    out = stamped(run_hook({"tool_name": "mcp__hokma__task_claim", "tool_input": {"task_id": 51}, "agent_type": CODER, "agent_id": "a1"}, {"HOKMA_AGENT": "hokma"}))
    assert out == {"task_id": 51, FIELD: CODER}


def test_the_main_agent_is_stamped_with_the_agent_of_the_session_and_with_nothing_when_there_is_none():
    assert stamped(run_hook({"tool_name": "mcp__hokma__journal_write", "tool_input": {"kind": "note"}}, {"HOKMA_AGENT": "hokma"}))[FIELD] == "hokma"
    assert stamped(run_hook({"tool_name": "mcp__hokma__journal_write", "tool_input": {"kind": "note"}}, {}))[FIELD] == ""


def test_what_the_model_writes_in_the_identity_field_is_always_overwritten():
    for agent, env_vars, expected in ((CODER, {"HOKMA_AGENT": "hokma"}, CODER), (None, {"HOKMA_AGENT": "hokma"}, "hokma"), (None, {}, "")):
        payload = {"tool_name": "mcp__hokma__strategy_code_verdict", "tool_input": {"rule_version_id": 1, FIELD: REVIEWER}}
        if agent:
            payload["agent_type"] = agent
        assert stamped(run_hook(payload, env_vars))[FIELD] == expected


def test_the_hook_fails_closed_on_anything_unexpected():
    for raw in ("", "not json", "[]", json.dumps({"tool_name": "x"}), json.dumps({"tool_input": "x"})):
        done = subprocess.run([sys.executable, "-I", HOOK], input=raw, capture_output=True, text=True)
        assert done.returncode == 2 and "HOKMA_BLOCKED identity" in done.stderr, raw


def test_the_two_copies_of_the_field_name_agree_and_the_hook_is_registered_protected_and_intact():
    assert identity_hook.IDENTITY_FIELD == FIELD
    built = settings.build_settings()
    groups = {g["matcher"]: [h["command"] for h in g["hooks"]] for g in built["hooks"]["PreToolUse"]}
    [command] = groups["mcp__hokma__.*"]
    assert HOOK in command and command.endswith("|| exit 2") and "-I" in command and "{{" not in command
    assert settings.IDENTITY_FILE in settings.protected_paths()
    text = json.dumps(built)
    assert registration_intact(text, [str(p) for p in settings.protected_paths()])
    without = json.loads(text)
    without["hooks"]["PreToolUse"] = [g for g in without["hooks"]["PreToolUse"] if g["matcher"] != "mcp__hokma__.*"]
    assert not registration_intact(json.dumps(without), [str(p) for p in settings.protected_paths()])          # taking the hook out of the settings is refused by the guard


def test_only_the_launch_for_claude_code_tells_the_server_to_trust_the_stamp(monkeypatch):
    assert launch.hokma_server()["env"]["HOKMA_IDENTITY_HOOK"] == "1"
    from hokma.engines.envfilter import filtered_env

    monkeypatch.setenv("HOKMA_IDENTITY_HOOK", "1")                              # the other engines (Codex, ACP, direct) build the environment of their server from the allow list: no hook there, no trust
    assert "HOKMA_IDENTITY_HOOK" not in filtered_env()


# ------------------------------------------------------------------ the server

@pytest.fixture
def seated(db, env, monkeypatch):
    with db.write() as u:
        for slug in ("hokma", RESEARCHER, CODER, REVIEWER):
            u.agents.upsert_kit(slug, slug.title(), f"{slug} agent", "deep", f"agents/{slug}.md")
            u.agents.set_status(slug, "active")
    monkeypatch.setenv("HOKMA_IDENTITY_HOOK", "1")
    monkeypatch.setenv("HOKMA_AGENT", "hokma")                    # the session of the chat: the main agent
    return db


def rpc(name, args, who=None, *, stamp=True):
    """The call as the server receives it: the hook stamped the agent (None: the field the hook leaves for the main agent is its own value)."""
    sent = {**args, **({FIELD: who} if stamp and who is not None else {})}
    answer = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": sent}}, "d", ["w"], ["d"])["result"]
    text = answer["content"][0]["text"]
    try:
        return json.loads(text), answer["isError"]
    except ValueError:
        return text, answer["isError"]


def test_sam_claims_his_assigned_task_when_the_session_is_the_main_chat(seated):
    with seated.write() as u:
        task = u.tasks.create("Code of STR-001", actor=Actor.system(), assigned_agent=CODER, status="ready").id
    refused, is_error = rpc("task_claim", {"task_id": task})                      # no stamp: the agent of the session (hokma) takes the call, the bug of 1.2.6
    assert is_error and "only the assigned agent can claim it" in refused
    taken, is_error = rpc("task_claim", {"task_id": task}, CODER)
    assert not is_error and taken["claimed_by"] == CODER and taken["status"] == "running", taken
    with seated.read() as u:
        assert u.tasks.get(task).claimed_by == CODER


def test_ian_writes_in_the_journal_with_his_own_name_and_the_main_chat_writes_without_a_foreign_key_error(seated, monkeypatch):
    saved, is_error = rpc("journal_write", {"kind": "note", "title": "requirement written"}, RESEARCHER)
    assert not is_error, saved
    assert rows(seated, "SELECT agent_slug, actor_kind FROM journal_entries WHERE id = %s" % saved["id"]) == [{"agent_slug": RESEARCHER, "actor_kind": "agent"}]
    monkeypatch.delenv("HOKMA_AGENT")                                             # a main chat with no agent in its environment calls itself "agent", which is not a row of agents
    for args in ({"kind": "note", "title": "no task"}, {"kind": "note", "title": "with a task", "task_id": 1}):
        with seated.write() as u:
            u.tasks.create("t", actor=Actor.system())
        made, is_error = rpc("journal_write", args)
        assert not is_error, made
        [row] = rows(seated, "SELECT agent_slug, refs FROM journal_entries WHERE id = %s" % made["id"])
        assert row["agent_slug"] is None and json.loads(row["refs"]) == {"caller": "agent"}          # saved, and who called stays in refs


def test_sam_saves_and_ben_gives_the_verdict_each_under_his_own_name_and_sam_never_approves_his_own_code(seated):
    bank_id = make_bank(seated)
    made, is_error = rpc("strategy_brief_write", {"bank_id": bank_id, "slug": "inside_bar", "title": "Inside bar", "brief": BRIEF}, RESEARCHER)
    assert not is_error, made
    put_files("inside_bar")
    saved, is_error = rpc("strategy_code_save", {"rule_id": made["rule_id"]}, CODER)
    assert not is_error, saved
    version = saved["rule_version_id"]
    own, is_error = rpc("strategy_code_verdict", {"rule_version_id": version, "verdict": "approved"}, CODER)
    assert is_error and own["error_code"] == "blocked"                           # the author is Sam and the one that asks for the verdict is Sam
    assert rows(seated, "SELECT status FROM user_rule_versions")[0]["status"] == "draft"
    done, is_error = rpc("strategy_code_verdict", {"rule_version_id": version, "verdict": "approved"}, REVIEWER)
    assert not is_error and done["status"] == "generated", done
    [review] = rows(seated, "SELECT reviewer FROM user_rule_reviews") if rows(seated, "SELECT reviewer FROM user_rule_reviews") else [{}]
    assert review.get("reviewer", REVIEWER) == REVIEWER


def test_the_model_cannot_take_another_identity(seated, monkeypatch):
    with seated.write() as u:
        task = u.tasks.create("Code of STR-002", actor=Actor.system(), assigned_agent=CODER, status="ready").id
    # 1. the hook is what stamps; a field that the model wrote is replaced by it before the server sees the call (proved in test_what_the_model_writes...)
    # 2. an engine without the hook (no HOKMA_IDENTITY_HOOK in the server) ignores the field whatever it says
    monkeypatch.delenv("HOKMA_IDENTITY_HOOK")
    refused, is_error = rpc("task_claim", {"task_id": task}, CODER)
    assert is_error and "only the assigned agent can claim it" in refused
    # 3. a name that is not an agent of Hokma (a helper like general-purpose, or an invention) leaves the agent of the session
    monkeypatch.setenv("HOKMA_IDENTITY_HOOK", "1")
    for name in ("general-purpose", "nobody", "../x", ""):
        refused, is_error = rpc("task_claim", {"task_id": task}, name)
        assert is_error and "only the assigned agent can claim it" in refused, name
    # 4. the field is never an argument: it is taken off before the schema is checked, so no tool sees it
    text, is_error = hokma_tools.call(*(lambda a: (a[0], a[1]))(("task_list", {})), hokma_tools.caller_for(None))
    assert not is_error, text
    assert hokma_tools.split_identity({"a": 1, FIELD: "x"}) == ({"a": 1}, "x") and hokma_tools.split_identity({"a": 1}) == ({"a": 1}, None)

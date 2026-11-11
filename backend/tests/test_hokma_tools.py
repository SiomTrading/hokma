"""The tools of the `hokma` MCP server on the real repositories (CA-20 to CA-23, CA-26)."""
import json
from pathlib import Path

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)

from hokma.kit import hokma_tools
from hokma.kit.hokma_tools import Caller
from hokma.kit.trash_mcp import handle


@pytest.fixture
def profiled(db, env):
    with db.write() as u:
        u.profile.save(owner_name="Ana", workdir=str(env / "work"))
        u.profile.complete_setup()
    return db


def run(name, args, agent="joel", task=None):
    env = {"HOKMA_AGENT": agent, **({"HOKMA_TASK_ID": str(task)} if task else {})}
    text, is_error = hokma_tools.call(name, args, Caller(env))
    return (json.loads(text) if not is_error else text), is_error


def test_the_server_lists_every_promised_tool_and_nothing_to_approve(db):
    listed = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, "d", ["w"], ["d"])["result"]["tools"]
    names = {t["name"] for t in listed}
    promised = {"task_show", "task_list", "task_create", "task_link", "task_claim", "task_heartbeat", "task_complete",
                "task_request_review", "task_review_approve", "task_request_changes", "task_block", "task_unblock",
                "task_comment", "task_archive", "task_reopen", "project_list", "project_show", "project_create",
                "journal_write", "journal_search", "ips_read", "decision_propose", "decision_list", "schedule_create",
                "schedule_list", "extension_request", "agents_list", "agent_save", "skill_save", "skill_install", "bug_report",
                "bug_update", "bug_list",
                "trash_move"}
    assert names == promised
    assert not [n for n in names if "approve_decision" in n or "permission" in n or "restore" in n]
    assert all(t["inputSchema"]["additionalProperties"] is False for t in listed)


def test_tasks_create_claim_once_complete_and_comment(db):
    created, err = run("task_create", {"title": "Check the IPS", "assigned_agent": "joel"})
    assert not err and created["status"] == "ready"
    tid = created["id"]
    claimed, err = run("task_claim", {"task_id": tid})
    assert not err and claimed["status"] == "running" and claimed["claimed_by"] == "joel"
    again, err = run("task_claim", {"task_id": tid}, agent="vera")
    assert err and "Conflict" in again                                   # a second claim fails
    assert not run("task_heartbeat", {"task_id": tid})[1]
    assert not run("task_comment", {"task_id": tid, "body": "working"})[1]
    done, err = run("task_complete", {"task_id": tid, "summary": "done", "metadata": {"k": 1}})
    assert not err and done["status"] in ("done", "review", "awaiting_approval")
    shown, err = run("task_show", {"task_id": tid})
    assert not err and shown["comments"][0]["body"] == "working"
    assert [t["id"] for t in run("task_list", {"assigned_agent": "joel"})[0]] == [tid]


def test_links_blocks_and_reopen(db):
    a = run("task_create", {"title": "A"})[0]["id"]
    b = run("task_create", {"title": "B", "status": "todo"})[0]["id"]
    assert not run("task_link", {"parent_id": a, "child_id": b})[1]
    assert run("task_show", {"task_id": b})[0]["parents"] == [a]
    blocked, err = run("task_block", {"task_id": a, "kind": "needs_input", "reason": "need data"})
    assert err or blocked["status"] == "blocked"


def test_journal_write_and_search(db):
    wrote, err = run("journal_write", {"kind": "note", "title": "Found a drift", "body": "details"})
    assert not err and wrote["id"]
    hits, err = run("journal_search", {"query": "drift"})
    assert not err and hits[0]["title"] == "Found a drift" and hits[0]["agent"] == "joel"


def test_decisions_stay_pending_for_the_owner(db):
    made, err = run("decision_propose", {"topic": "Max size", "proposal": "2 percent"})
    assert not err and made["status"] == "proposed" or made["status"] == "pending"
    listed = run("decision_list", {})[0]
    assert listed[0]["topic"] == "Max size" and listed[0]["status"] == made["status"]


def test_ips_read_needs_a_project_of_kind_ips(profiled, tmp_path):
    text, err = run("ips_read", {})
    assert err and "kind ips" in text
    folder = tmp_path / "ips"
    folder.mkdir()
    (folder / "IPS.md").write_text("Max drawdown 10 percent")
    made, err = run("project_create", {"name": "Fund", "kind": "ips"})
    assert not err
    (Path(made["path"]) / "IPS.md").write_text("Max drawdown 10 percent")
    doc, err = run("ips_read", {})
    assert not err and "Max drawdown" in doc["document"]
    assert run("project_show", {"project_id": made["id"]})[0]["project"]["kind"] == "ips"
    assert [p["id"] for p in run("project_list", {})[0]] == [made["id"]]


def test_routines_and_extensions_wait_for_the_owner(db):
    job, err = run("schedule_create", {"name": "Daily", "prompt": "report", "schedule_kind": "daily",
                                       "schedule_expr": "09:00"})
    assert not err and job["is_active"] is False
    assert run("schedule_list", {})[0][0]["name"] == "Daily"
    req, err = run("extension_request", {"action": "install", "kind": "plugin", "name": "x", "marketplace": "m",
                                         "reason": "needed"})
    assert not err and req["status"] == "pending"


def test_agents_and_bugs(db):
    assert {a["slug"] for a in run("agents_list", {})[0]} == {"hokma", "joel", "vera", "iris"}
    bug, err = run("bug_report", {"title": "Chart flickers", "severity": "high"})
    assert not err and bug["status"] == "open"
    assert run("bug_list", {"severity": "high"})[0][0]["id"] == bug["id"]
    updated, err = run("bug_update", {"bug_id": bug["id"], "description": "after resize"})
    assert not err and updated["description"] == "after resize"


def test_bad_arguments_are_errors_the_model_can_read(db):
    for name, args in (("task_show", {}), ("task_show", {"task_id": "1"}), ("task_show", {"task_id": 1, "x": 2}),
                       ("task_block", {"task_id": 1, "kind": "nope", "reason": "r"}), ("task_show", {"task_id": 999})):
        text, err = run(name, args)
        assert err and isinstance(text, str), (name, args)


def test_the_call_goes_through_the_server_protocol(db):
    answer = handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                     "params": {"name": "agents_list", "arguments": {}}}, "d", ["w"], ["d"])
    assert answer["result"]["isError"] is False and "joel" in answer["result"]["content"][0]["text"]
    unknown = handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "robot_status"}}, "d", ["w"], ["d"])
    assert unknown["error"]["code"] == -32602


def test_the_direct_engine_offers_and_runs_the_hokma_tools(db, tmp_path):
    import asyncio

    from hokma.engines.base import EngineContext
    from hokma.engines.hokma_mcp import hokma_toolbox
    ctx = EngineContext(conversation_id="1", model="m", cwd=str(tmp_path), agent="joel")
    box = hokma_toolbox(ctx)
    assert box is not None
    try:
        assert box.owns("task_create") and box.owns("agents_list") and not box.owns("robot_status")
        assert box.broker_request("task_list", {"a": 1}) == ("mcp__hokma__task_list", {"a": 1})
        text, err = box.run("task_create", {"title": "From the direct engine"})
        assert not err and json.loads(text)["title"] == "From the direct engine"
        text, err = box.run("task_show", {"task_id": 4242})
        assert err
    finally:
        box.close()


def test_codex_config_carries_the_hokma_server_with_an_explicit_environment(env, tmp_path):
    from hokma.core.config import codex_home_dir
    from hokma.engines.base import EngineContext
    from hokma.engines.codex import write_config
    from hokma.engines.hokma_mcp import server_spec
    ctx = EngineContext(conversation_id="3", model="", cwd=str(tmp_path), agent="vera", task_id="8")
    write_config(ctx, {"hokma": server_spec(ctx)})
    toml = (codex_home_dir() / "config.toml").read_text()
    assert "[mcp_servers.hokma]" in toml and 'HOKMA_AGENT = "vera"' in toml and 'HOKMA_TASK_ID = "8"' in toml
    assert 'approval_policy = "untrusted"' in toml


def test_project_create_takes_no_path_and_makes_the_folder_itself(profiled, env):
    text, err = run("project_create", {"name": "Evil", "path": "/etc", "kind": "other"})
    assert err and "unknown argument: path" in text
    made, err = run("project_create", {"name": "Safe"})
    assert not err and str(env) in made["path"] and Path(made["path"]).is_dir()


def test_ips_read_refuses_a_project_folder_outside_the_workdir(profiled, env, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "IPS.md").write_text("secret")
    with profiled.write() as u:
        u.projects.create("Odd", str(outside), kind="ips")
    text, err = run("ips_read", {})
    assert err and "outside the working folder" in text


def test_only_the_claiming_agent_changes_a_running_task(db):
    tid = run("task_create", {"title": "Mine"}, agent="joel")[0]["id"]
    token = run("task_claim", {"task_id": tid}, agent="joel")[0]
    assert token["claimed_by"] == "joel"
    for name, args in (("task_heartbeat", {}), ("task_complete", {"summary": "x"}), ("task_block", {"kind": "transient", "reason": "r"}),
                       ("task_archive", {}), ("task_request_review", {"summary": "x"})):
        text, err = run(name, {"task_id": tid, **args}, agent="vera")
        assert err and "only that agent" in text, name
    assert not run("task_heartbeat", {"task_id": tid}, agent="joel")[1]
    with db.read() as u:
        real = u.tasks.get(tid).claim_token
    assert not run("task_heartbeat", {"task_id": tid, "claim_token": real}, agent="vera")[1]
    assert not run("task_comment", {"task_id": tid, "body": "a note"}, agent="vera")[1]




def test_ips_read_ignores_symlinks_and_files_that_resolve_outside(profiled):
    made, err = run("project_create", {"name": "Fund", "kind": "ips"})
    assert not err
    folder = Path(made["path"])
    secret = folder.parent / "secret.md"
    secret.write_text("TOPSECRET")
    (folder / "IPS.md").symlink_to(secret)
    (folder / "notes.md").symlink_to(secret)
    doc, err = run("ips_read", {})
    assert not err and "TOPSECRET" not in doc["document"] and doc["document"] == ""
    (folder / "plain.md").write_text("real notes")
    doc, err = run("ips_read", {})
    assert not err and doc["document"] == "real notes"

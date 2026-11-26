"""The strategy creator from the chat of a desk (SPEC-031 A2, 6.5): the three services take the tasks of any desk without being on its team, what they create says where it was created, and every other
agent outside the team stays refused."""
import json

from code_support import BRIEF, CODER, RESEARCHER, put_files
from dbsupport import db, env  # noqa: F401  (fixtures)
from desks_support import desks_db, make_desk  # noqa: F401  (fixture)
from e2e_h4.support import caller, ok

from hokma.db import Actor
from hokma.kit import hokma_tools
from hokma.kit.hokma_tools import Caller

SERVICES = ("strategy-researcher", "strategy-coder", "strategy-reviewer")


def run(name, args, agent, task=None):
    env = {"HOKMA_AGENT": agent, **({"HOKMA_TASK_ID": str(task)} if task else {})}
    text, is_error = hokma_tools.call(name, args, Caller(env))
    return (json.loads(text) if not is_error else text), is_error


def test_the_chief_gives_a_task_to_each_service_and_each_takes_it_without_being_on_the_team(desks_db):
    desk = make_desk(desks_db)
    with desks_db.write() as u:
        for slug in SERVICES:
            u.agents.upsert_kit(slug, slug.title(), f"{slug} agent", "deep", f"service/agents/{slug}.md")
            u.agents.set_status(slug, "active")
    for slug in SERVICES:
        made, is_error = run("task_create", {"title": f"strategy work by {slug}", "project_id": desk["project_id"], "assigned_agent": slug}, "desk-chief")
        assert not is_error, made
        taken, is_error = run("task_claim", {"task_id": made["id"]}, slug)
        assert not is_error and taken["claimed_by"] == slug, taken
    text, is_error = run("task_create", {"title": "x", "project_id": desk["project_id"], "assigned_agent": "joel"}, "desk-chief")
    assert is_error and "not an active member" in text                       # nobody else outside the team gains access


def test_what_the_services_create_from_a_desk_says_the_desk_and_the_main_chat_says_hokma(desks_db):
    desk = make_desk(desks_db)
    with desks_db.write() as u:
        for slug in SERVICES:
            u.agents.upsert_kit(slug, slug.title(), f"{slug} agent", "deep", f"service/agents/{slug}.md")
            u.agents.set_status(slug, "active")
        task = u.tasks.create("Requirement", actor=Actor.system(), project_id=desk["project_id"], assigned_agent="strategy-researcher", status="ready").id
    from code_support import make_bank

    bank_id = make_bank(desks_db)
    made, is_error = run("strategy_brief_write", {"bank_id": bank_id, "slug": "from_desk", "title": "From the desk", "brief": BRIEF}, "strategy-researcher", task)
    assert not is_error, made
    put_files("from_desk")
    kept, is_error = run("strategy_code_save", {"rule_id": made["rule_id"]}, "strategy-coder", task)
    assert not is_error and kept["saved"], kept
    with desks_db.read() as u:
        rule = u.session.one("SELECT origin_desk_id, origin_desk_name FROM user_rules WHERE slug = 'from_desk'")
        version = u.session.one("SELECT origin_desk_id, origin_desk_name FROM user_rule_versions")
    assert (rule["origin_desk_id"], rule["origin_desk_name"]) == (desk["id"], desk["name"]) == (version["origin_desk_id"], version["origin_desk_name"])
    approved, is_error = run("strategy_code_verdict", {"rule_version_id": kept["rule_version_id"], "verdict": "approved"}, "strategy-reviewer", task)
    assert not is_error and approved["origin"] == {"kind": "desk", "desk_id": desk["id"], "desk_name": desk["name"]}
    # one bank: every desk and the main chat see every strategy, each with its origin
    listed = ok("bank_list", {}, caller("hokma"))["rows"]
    assert [(r["origin"]["kind"], r["origin"].get("desk_name")) for r in listed] == [("desk", desk["name"])]
    assert [r["origin"]["kind"] for r in ok("bank_list", {"origin": "desk"}, caller("hokma"))["rows"]] == ["desk"]

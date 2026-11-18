"""Model and budget of a desk while it runs (SPEC-014 P5): the desk model reaches the chat turn and the task run, the
tokens land on that desk only, and a desk over its weekly budget starts nothing new.

The chat turns run the fake `claude` of the engine tests; the task claims go through the same MCP tool agents use."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from engine_support import connect, env, hard_timeout, make_client, posix_only, turn  # noqa: F401  (fixtures)

from hokma.db import Actor, get_database, migrate
from hokma.desks import budget, service
from hokma.desks.errors import DeskError
from hokma.engine.dbstore import DbAgents, DbChatStore, DbProjects
from hokma.engine.engine import ChatEngine
from hokma.kit import hokma_tools
from hokma.kit.hokma_tools import Caller
from hokma.rpc.ws import SESSION_TOKEN

pytestmark = posix_only

SLUGS = ("desk-chief", "hugo", "risk")
OWNER = Actor.human("owner")


@pytest.fixture
def world(env):
    """A migrated database where the fake claude lives, three desk agents on, an engine on top of it."""
    migrate(env.data / "hokma.db")
    db = get_database()
    with db.write() as u:
        u.profile.save(owner_name="Ana", fund_name="Fondo Prueba")
        for slug in SLUGS:
            u.agents.upsert_kit(slug, slug.title(), f"{slug} agent", "balanced", f"agents/{slug}.md")
            u.agents.set_status(slug, "active")
    engine = ChatEngine(store=DbChatStore(db), projects=DbProjects(db), agents=DbAgents(db),
                        launch=lambda: env.launch_spec, session_token=SESSION_TOKEN, stop_grace=0.5)
    return env, db, engine


def make_desk(db, name, **kw):
    return service.create_desk(name, 10000, template="minimal", db=db, **kw)


def used(db, desk):
    return budget.usage(desk["id"], db=db)["used"]


def claim(task_id, agent="hugo"):
    text, is_error = hokma_tools.call("task_claim", {"task_id": task_id}, Caller({"HOKMA_AGENT": agent}))
    return (text if is_error else json.loads(text)), is_error


def new_task(db, project_id, agent="hugo"):
    with db.write() as u:
        return u.tasks.create("Look at the open", actor=OWNER, project_id=project_id, assigned_agent=agent,
                              status="ready").id


def runs_of(db, task_id):
    with db.read() as u:
        return u.tasks.detail(task_id).task, u.task_runs.list(task_id)


# ------------------------------------------------------------------------------------- model of the desk

def test_chat_turn_uses_the_model_of_the_desk_and_its_tokens_stay_on_that_desk(world):
    env, db, engine = world
    a, b = make_desk(db, "Mesa A"), make_desk(db, "Mesa B")
    service.set_agent(a["id"], "desk-chief", model="opus", db=db)
    conv_a = str(service.open_conversation(a["id"], db=db)["conversation_id"])
    conv_b = str(service.open_conversation(b["id"], db=db)["conversation_id"])
    with make_client(engine) as client, connect(client) as ws:
        events = turn(ws, "hello desk A", conversation_id=conv_a)
        assert events[-1]["type"] == "done", events[-1]
    assert "opus" in env.entries("start")[-1]["argv"]
    assert used(db, a) > 0 and used(db, b) == 0
    with db.read() as u:
        assert u.conversations.get(int(conv_a)).model == "opus" and u.conversations.get(int(conv_b)).model != "opus"
        assert [m.model for m in u.messages.list(int(conv_a)) if m.role == "assistant"] == ["opus"]
        assert u.agents.get("desk-chief").model_tier == "balanced"        # the global agent is untouched
    with make_client(engine) as client, connect(client) as ws:
        turn(ws, "hello desk B", conversation_id=conv_b)
    assert "opus" not in env.entries("start")[-1]["argv"]
    assert used(db, b) > 0


def test_a_turn_of_another_agent_runs_with_the_model_the_desk_gave_it(world):
    env, db, engine = world
    desk = make_desk(db, "Mesa A")
    service.set_agent(desk["id"], "hugo", model="haiku", db=db)
    with db.write() as u:
        conv = u.conversations.create(engine="claude_code", title="data", project_id=desk["project_id"],
                                      model="opus", agent_slug="hugo", project_mode="fixed")
    with make_client(engine) as client, connect(client) as ws:
        assert turn(ws, "pull the data", conversation_id=str(conv.id))[-1]["type"] == "done"
    assert "haiku" in env.entries("start")[-1]["argv"]
    with db.read() as u:
        assert u.conversations.get(conv.id).model == "haiku"


def test_model_for_is_none_outside_a_desk_and_for_agents_that_are_not_on_it(world):
    _, db, _ = world
    desk = make_desk(db, "Mesa A")
    assert service.model_for(desk["project_id"], "hugo", db=db) == "haiku"
    assert service.model_for(desk["project_id"], "nobody", db=db) is None
    assert service.model_for(None, "hugo", db=db) is None
    assert service.model_for(desk["project_id"] + 1000, "hugo", db=db) is None


def test_a_task_claimed_on_a_desk_is_recorded_with_the_model_of_the_desk(world):
    _, db, _ = world
    desk = make_desk(db, "Mesa A")
    service.set_agent(desk["id"], "hugo", model="opus", db=db)
    task = new_task(db, desk["project_id"])
    claimed, err = claim(task)
    assert not err and claimed["status"] == "running"
    _, runs = runs_of(db, task)
    assert [(r.agent_slug, r.model, r.outcome) for r in runs] == [("hugo", "opus", "running")]
    # the desk shows the agent working while its run is open, and the run closes with the normal tools
    assert [x["status"] for x in service.list_agents(desk["id"], db=db) if x["agent_slug"] == "hugo"] == ["working"]
    done, err = hokma_tools.call("task_complete", {"task_id": task, "summary": "done"}, Caller({"HOKMA_AGENT": "hugo"}))
    assert not err
    assert runs_of(db, task)[1][0].outcome == "completed"


def test_a_task_outside_a_desk_is_claimed_as_before(world):
    _, db, _ = world
    task = new_task(db, None)
    claimed, err = claim(task)
    assert not err and claimed["status"] == "running"
    assert runs_of(db, task)[1] == []


# ---------------------------------------------------------------------------------------------- budget

def test_a_desk_over_its_budget_refuses_the_chat_and_says_why(world):
    env, db, engine = world
    desk = make_desk(db, "Mesa A", token_budget=1)
    other = make_desk(db, "Mesa B")
    conv = str(service.open_conversation(desk["id"], db=db)["conversation_id"])
    other_conv = str(service.open_conversation(other["id"], db=db)["conversation_id"])
    with make_client(engine) as client, connect(client) as ws:
        assert turn(ws, "first, under the budget", conversation_id=conv)[-1]["type"] == "done"
        starts = len(env.entries("start"))
        refused = turn(ws, "second, over the budget", conversation_id=conv)[-1]
        assert refused["type"] == "error" and refused["code"] == "desk_budget_exhausted"
        assert refused["message"] == DeskError("desk_budget_exhausted").message
        assert len(env.entries("start")) == starts                      # nothing was started
        # the other desk and the general chat go on
        assert turn(ws, "desk B is fine", conversation_id=other_conv)[-1]["type"] == "done"
        assert turn(ws, "no desk at all")[-1]["type"] == "done"
    with db.read() as u:
        texts = [m.content for m in u.messages.list(int(conv)) if m.role == "user"]
        blocks = u.journal.list(kind="block", project_id=desk["project_id"])
    assert texts == ["first, under the budget"]                         # the refused message was not saved
    assert len(blocks) == 1 and blocks[0].actor_kind == "system"
    assert blocks[0].refs["reason"] == "budget"


def test_raising_the_budget_or_a_new_week_lets_the_desk_run_again(world):
    _, db, engine = world
    desk = make_desk(db, "Mesa A", token_budget=1)
    conv = str(service.open_conversation(desk["id"], db=db)["conversation_id"])
    with make_client(engine) as client, connect(client) as ws:
        turn(ws, "spend", conversation_id=conv)
        assert turn(ws, "refused", conversation_id=conv)[-1]["code"] == "desk_budget_exhausted"
        service.update_desk(desk["id"], {"token_budget": 10_000_000}, db=db)
        assert turn(ws, "after raising it", conversation_id=conv)[-1]["type"] == "done"
        service.update_desk(desk["id"], {"token_budget": 1}, db=db)
        assert turn(ws, "tight again", conversation_id=conv)[-1]["code"] == "desk_budget_exhausted"
    later = datetime.now(timezone.utc) + timedelta(days=8)
    service.assert_can_run(desk["project_id"], db=db, now=later)         # the next week starts at zero


def test_a_desk_over_its_budget_hands_out_no_task_and_the_task_stays_as_it_is(world):
    _, db, engine = world
    desk = make_desk(db, "Mesa A", token_budget=1)
    conv = str(service.open_conversation(desk["id"], db=db)["conversation_id"])
    with make_client(engine) as client, connect(client) as ws:
        turn(ws, "spend", conversation_id=conv)
    task = new_task(db, desk["project_id"])
    message, err = claim(task)
    assert err and "desk_budget_exhausted" in message
    task_row, runs = runs_of(db, task)
    assert task_row.status == "ready" and task_row.claimed_by is None and runs == []
    claim(task)
    with db.read() as u:
        assert len(u.journal.list(kind="block", project_id=desk["project_id"])) == 1   # once per week
    # a task of another desk, or of no desk, is claimed normally
    free = make_desk(db, "Mesa B")
    assert not claim(new_task(db, free["project_id"]))[1]
    assert not claim(new_task(db, None))[1]


def test_without_a_budget_the_desk_never_blocks(world):
    _, db, _ = world
    desk = make_desk(db, "Mesa A")
    service.update_desk(desk["id"], {"token_budget": None}, db=db)
    service.assert_can_run(desk["project_id"], db=db)
    service.assert_can_run(None, db=db)


# ------------------------------------------------------------------------- who turns desk agents on

def _status(db, slugs=SLUGS):
    with db.read() as u:
        return {s: u.agents.get(s).status for s in slugs}


def _all_off(db):
    with db.write() as u:
        for slug in SLUGS:
            u.agents.set_status(slug, "off")


def _outsider(db):
    with db.write() as u:
        u.agents.upsert_kit("iris", "Iris", "iris agent", "balanced", "agents/iris.md")
        u.agents.set_status("iris", "active")


def test_a_refused_claim_of_an_outside_agent_turns_nobody_on(world):
    _, db, _ = world
    desk = make_desk(db, "Mesa A")
    task = new_task(db, desk["project_id"])
    _outsider(db)
    _all_off(db)
    message, err = claim(task, agent="iris")
    assert err and "only the assigned agent" in message
    assert set(_status(db).values()) == {"off"}


def test_a_hokma_task_create_in_a_desk_it_does_not_belong_to_turns_nobody_on(world):
    _, db, _ = world
    desk = make_desk(db, "Mesa A")
    _all_off(db)
    text, err = hokma_tools.call("task_create", {"title": "intruder", "project_id": desk["project_id"]},
                                 Caller({"HOKMA_AGENT": "hokma"}))
    assert not err, text
    assert set(_status(db).values()) == {"off"}


def test_a_member_creating_a_task_in_its_desk_turns_the_team_on(world):
    _, db, _ = world
    desk = make_desk(db, "Mesa A")
    _all_off(db)
    text, err = hokma_tools.call("task_create", {"title": "mine", "project_id": desk["project_id"]},
                                 Caller({"HOKMA_AGENT": "desk-chief"}))
    assert not err, text
    assert set(_status(db).values()) == {"active"}


def test_the_owner_opening_the_desk_turns_its_team_on(world):
    _, db, _ = world
    desk = make_desk(db, "Mesa A")
    _all_off(db)
    service.open_conversation(desk["id"], db=db)
    assert set(_status(db).values()) == {"active"}

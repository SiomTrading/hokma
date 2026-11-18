"""Task 3286: Hokma creates and configures money desks from the chat, with the owner's permission."""
import asyncio
import json
from pathlib import Path

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from hardblock_support import box  # noqa: F401  (fixture)
from test_engine_direct import Server, make_ctx, oa_call, oa_text, run as run_engine

from hokma.desks import service, templates
from hokma.engine import stream
from hokma.engines import secrets
from hokma.engines.direct.loop import DirectAdapter
from hokma.kit import hokma_tools
from hokma.kit.hokma_tools import Caller
from hokma.kit.sync import sync_kit  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
SLUGS = ("desk-chief", "hugo", "risk", "iris", "signals", "axel", "vera", "bull-case", "bear-case", "judge")


@pytest.fixture
def desks_db(db):
    with db.write() as u:
        u.profile.save(owner_name="Ana", fund_name="Fondo Prueba")
        for slug in SLUGS:
            u.agents.upsert_kit(slug, slug.title(), f"{slug} agent", "balanced", f"agents/{slug}.md")
            u.agents.set_status(slug, "active")
    return db


def call(name, args, agent="hokma", conversation=None):
    env = {"HOKMA_AGENT": agent}
    if conversation is not None:
        env["HOKMA_CONVERSATION_ID"] = str(conversation)
    text, is_error = hokma_tools.call(name, args, Caller(env))
    return (json.loads(text) if not is_error else text), is_error


def as_chief(desk):
    return {"agent": "desk-chief", "conversation": service.open_conversation(desk["id"])["conversation_id"]}


def counts(db):
    with db.read() as u:
        return {t: u.session.scalar(f"SELECT count(*) FROM {t}") for t in
                ("projects", "desks", "desk_agents", "desk_assets")}


# ------------------------------------------------------------------------------------------------ desk_create

def test_the_tools_are_declared_with_strict_schemas_and_templates_match_the_service():
    listed = {d["name"]: d for d in hokma_tools.definitions()}
    create, configure = listed["desk_create"]["inputSchema"], listed["desk_configure"]["inputSchema"]
    assert create["required"] == ["name", "capital"] and create["additionalProperties"] is False
    assert configure["required"] == ["desk_id"] and configure["additionalProperties"] is False
    assert hokma_tools.TOOLS["desk_create"][4] and hokma_tools.TOOLS["desk_configure"][4]
    assert create["properties"]["template"]["enum"] == ["minimal", "standard", "debate"]
    assert set(create["properties"]["template"]["enum"]) <= set(templates.TEMPLATES)


@pytest.mark.parametrize("template", ["minimal", "standard", "debate"])
def test_desk_create_builds_the_same_desk_as_the_api(desks_db, template):
    created, err = call("desk_create", {"name": "Con debate", "capital": 25000, "template": template,
                                        "assets": ["NQ=F", "ES=F"], "mandate": "Trade the open.",
                                        "token_budget": 300000})
    assert not err
    direct = service.create_desk("Twin", 25000, template=template, assets=["NQ=F", "ES=F"], mandate="Trade the open.",
                                 token_budget=300000, db=desks_db)
    assert created["template"] == template and created["capital"] == 25000 and created["mandate"] == "Trade the open."
    assert created["assets"] == ["NQ=F", "ES=F"] and created["budget"]["token_budget"] == 300000
    assert [a["agent_slug"] for a in created["agents"]] == [a["agent_slug"] for a in direct["agents"]]
    assert [(a["role"], a["model"]) for a in created["agents"]] == [(a["role"], a["model"]) for a in direct["agents"]]
    assert len(created["agents"]) == len(templates.get(template).members)
    assert (Path(service.projects.projects_root(desks_db)) / "con-debate" / "CLAUDE.md").is_file()


def test_desk_create_defaults_to_standard_and_has_no_budget_of_its_own(desks_db):
    created, err = call("desk_create", {"name": "Basic", "capital": 1000})
    assert not err and created["template"] == "standard" and created["budget"]["token_budget"] is None
    assert created["assets"] == []


def test_a_repeated_name_is_refused_with_a_clear_error_and_creates_nothing(desks_db):
    assert not call("desk_create", {"name": "Futuros NQ", "capital": 1000})[1]
    before = counts(desks_db)
    text, err = call("desk_create", {"name": "  futuros   nq ", "capital": 1000})
    assert err and "already exists" in text and "Futuros NQ" in text
    assert counts(desks_db) == before


@pytest.mark.parametrize("args,fragment", [
    ({"name": "   ", "capital": 1000}, "name"),
    ({"name": "x" * 81, "capital": 1000}, "80"),
    ({"name": "A", "capital": 0}, "capital"),
    ({"name": "A", "capital": 1000, "assets": ["bad symbol!!"]}, "symbol"),
    ({"name": "A", "capital": 1000, "token_budget": -5}, "token budget"),
    ({"name": "A", "capital": 1000, "mandate": "m" * 4001}, "mandate"),
])
def test_invalid_values_give_a_readable_error_and_create_nothing(desks_db, args, fragment):
    before = counts(desks_db)
    text, err = call("desk_create", args)
    assert err and fragment.lower() in text.lower(), text
    assert counts(desks_db) == before


def test_the_schema_refuses_an_unknown_template_and_unknown_fields(desks_db):
    assert call("desk_create", {"name": "A", "capital": 1000, "template": "huge"})[1]
    assert call("desk_create", {"name": "A", "capital": 1000, "real_money": True})[1]
    assert counts(desks_db)["desks"] == 0


def test_a_desk_chief_cannot_create_a_desk(desks_db):
    text, err = call("desk_create", {"name": "A", "capital": 1000}, agent="desk-chief")
    assert err and "chief" in text and counts(desks_db)["desks"] == 0


# -------------------------------------------------------------------------------------------- desk_configure

@pytest.fixture
def two(desks_db):
    first = service.create_desk("Alpha", 10000, template="standard", assets=["NQ=F"], db=desks_db)
    second = service.create_desk("Beta", 5000, template="minimal", mandate="Beta mandate", db=desks_db)
    return first, second


def test_desk_configure_changes_only_the_desk_and_fields_given(two):
    first, second = two
    changed, err = call("desk_configure", {"desk_id": first["id"], "name": "Alpha 2", "capital": 20000,
                                           "assets": ["ES=F", "NQ=F"], "mandate": "New", "token_budget": 500})
    assert not err
    assert (changed["name"], changed["capital"], changed["assets"], changed["mandate"]) == (
        "Alpha 2", 20000, ["ES=F", "NQ=F"], "New")
    assert changed["budget"]["token_budget"] == 500 and changed["template"] == "standard"
    other, _ = call("desk_show", {"desk_id": second["id"]})
    assert (other["name"], other["capital"], other["mandate"], other["assets"]) == ("Beta", 5000, "Beta mandate", [])
    only, err = call("desk_configure", {"desk_id": first["id"], "capital": 30000})
    assert not err and only["capital"] == 30000 and only["name"] == "Alpha 2" and only["mandate"] == "New"


def test_desk_configure_changes_the_model_of_one_agent_of_that_desk(two):
    first, second = two
    changed, err = call("desk_configure", {"desk_id": first["id"], "agent": "iris", "model": "opus"})
    assert not err
    assert {a["agent_slug"]: a["model"] for a in changed["agents"]}["iris"] == "opus"
    assert all(a["model"] != "opus" for a in call("desk_show", {"desk_id": second["id"]})[0]["agents"])
    assert call("desk_configure", {"desk_id": second["id"], "agent": "iris", "model": "opus"})[1]   # not in minimal
    assert call("desk_configure", {"desk_id": first["id"], "agent": "iris"})[1]                    # model missing


def test_a_bad_field_changes_nothing_even_when_another_field_is_good(two):
    first, _ = two
    for bad in ({"assets": ["bad symbol!!"]}, {"agent": "ghost", "model": "haiku"}, {"capital": -1},
                {"name": "Beta"}):
        text, err = call("desk_configure", {"desk_id": first["id"], "mandate": "CHANGED", **bad})
        assert err, bad
        assert call("desk_show", {"desk_id": first["id"]})[0]["mandate"] != "CHANGED", bad
    assert "already exists" in call("desk_configure", {"desk_id": first["id"], "name": "beta"})[0]


def test_renaming_a_desk_to_its_own_name_is_fine_and_an_empty_change_or_unknown_desk_is_an_error(two):
    first, _ = two
    assert not call("desk_configure", {"desk_id": first["id"], "name": "Alpha"})[1]
    assert "nothing to change" in call("desk_configure", {"desk_id": first["id"]})[0]
    assert call("desk_configure", {"desk_id": 9999, "name": "X"})[1]


def test_the_chief_changes_its_own_desk_and_is_refused_on_another(two):
    first, second = two
    chief = as_chief(first)
    ok, err = call("desk_configure", {"desk_id": first["id"], "mandate": "Chief edit"}, **chief)
    assert not err and ok["mandate"] == "Chief edit"
    text, err = call("desk_configure", {"desk_id": second["id"], "mandate": "Hacked", "capital": 99999}, **chief)
    assert err and f"only desk {first['id']}" in text
    other, _ = call("desk_show", {"desk_id": second["id"]})
    assert other["mandate"] == "Beta mandate" and other["capital"] == 5000


def test_a_chief_without_a_desk_project_is_refused(two):
    first, _ = two
    text, err = call("desk_configure", {"desk_id": first["id"], "capital": 1}, agent="desk-chief")
    assert err and "no" in text and call("desk_show", {"desk_id": first["id"]})[0]["capital"] == 10000


def test_hokma_changes_any_desk_from_outside_a_desk(two):
    _, second = two
    assert not call("desk_configure", {"desk_id": second["id"], "capital": 7000}, agent="hokma")[1]


# ------------------------------------------------------------------------------------------ the permission

def test_the_request_shows_what_will_be_created_or_changed():
    made = stream.summarize_tool("mcp__hokma__desk_create", {"name": "Con debate", "capital": 5000, "template": "debate",
                                                              "assets": ["NQ=F", "ES=F"], "mandate": "Open only"})
    assert made["kind"] == "other"
    for part in ("name=Con debate", "template=debate", "capital=5000", "assets=NQ=F, ES=F", "mandate=Open only"):
        assert part in made["target"]
    changed = stream.summarize_tool("desk_configure", {"desk_id": 4, "agent": "iris", "model": "opus"})
    assert changed["target"] == "desk 4 | agent=iris | model=opus"
    assert stream.summarize_tool("mcp__hokma__task_list", {})["target"] == ""
    long = stream.summarize_tool("desk_create", {"name": "n", "capital": 1, "mandate": "m" * 4000})["target"]
    assert "m" * 4000 in long                       # the owner sees the whole mandate, never a cut one


class DeskToolBox:
    """The Hokma tools as the direct engine sees them, on the real handlers."""

    def __init__(self):
        self.calls = []

    def definitions(self):
        return [{"name": d["name"], "description": d["description"], "schema": d["inputSchema"]}
                for d in hokma_tools.definitions()]

    def owns(self, name):
        return name in hokma_tools.TOOLS

    def broker_request(self, name, args):
        return "mcp__hokma__" + name, args

    def run(self, name, args):
        self.calls.append(name)
        return hokma_tools.call(name, args, Caller({"HOKMA_AGENT": "hokma"}))


def _chat(box, monkeypatch, answer):
    monkeypatch.setattr(secrets, "get", lambda cid: "sk-test")
    args = json.dumps({"name": "Con debate", "capital": 5000, "template": "debate"})
    srv = Server([oa_call("c1", "desk_create", args), oa_text("done")])
    toolbox = DeskToolBox()
    try:
        events, _ = asyncio.run(run_engine(DirectAdapter(lambda ctx: toolbox), make_ctx(box, srv.url, mode="ask"),
                                           answer=answer))
    finally:
        srv.stop()
    return events, toolbox


def test_without_permission_nothing_is_created(desks_db, box, monkeypatch):
    events, toolbox = _chat(box, monkeypatch, answer=False)
    asked = next(e for e in events if e["type"] == "permission_request")
    assert asked["tool"] == "desk_create" and asked["input"]["name"] == "Con debate"
    assert toolbox.calls == [] and counts(desks_db)["desks"] == 0
    assert next(e for e in events if e["type"] == "tool_result")["is_error"]


def test_with_permission_the_desk_is_created_after_the_answer(desks_db, box, monkeypatch):
    events, toolbox = _chat(box, monkeypatch, answer=True)
    assert any(e["type"] == "permission_request" for e in events)
    assert toolbox.calls == ["desk_create"] and counts(desks_db)["desks"] == 1
    result = next(e for e in events if e["type"] == "tool_result")
    assert not result["is_error"] and json.loads(result["text"])["name"] == "Con debate"


# ------------------------------------------------------------------------------------------------ the agents

def _tools(slug):
    from hokma.db.seeds import parse_frontmatter

    meta = parse_frontmatter((ROOT / "kit" / "template" / "agents" / f"{slug}.md").read_text(encoding="utf-8"))
    return {t.strip() for t in meta["tools"].split(",")}


def test_who_has_which_tool():
    assert {"mcp__hokma__desk_create", "mcp__hokma__desk_configure"} <= _tools("hokma")
    chief = _tools("desk-chief")
    assert "mcp__hokma__desk_configure" in chief and "mcp__hokma__desk_create" not in chief
    for slug in ("signals", "risk", "bull-case", "bear-case", "judge"):
        assert not {"mcp__hokma__desk_create", "mcp__hokma__desk_configure"} & _tools(slug), slug


def test_hokma_never_says_it_cannot_create_a_desk():
    text = (ROOT / "kit" / "template" / "agents" / "hokma.md").read_text(encoding="utf-8")
    assert "Never answer that you cannot create a desk" in text and "`desk_create`" in text
    claude = (ROOT / "kit" / "template" / "CLAUDE.md").read_text(encoding="utf-8")
    assert "do not create anything" not in claude and "only from the app" not in claude

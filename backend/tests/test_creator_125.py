"""1.2.5: the tramo of Ian in the strategy creator. The spec has one road (service/strategies/STR-{NNN}-{slug}/spec.md), an idea with no source makes its card and its folder,
the copy of Ian, Sam and Ben that Claude Code loads always carries Read, Write, Edit and Agent (also when the source file is old, narrow or edited), and the main chat and the agents
of a desk hand a strategy request to Ian."""
import json
import re
import shutil
from pathlib import Path

from code_support import BRIEF, RESEARCHER, make_bank
from code_support import db, env, pg_admin_url, pg_template, pg_url_migrated  # noqa: F401  (fixtures)
from e2e_h4.support import call, caller, failed, ok
from test_creator_any_agent import gate

from hokma.agents import files as fl
from hokma.agents.service import AgentService
from hokma.core.config import hokma_home
from hokma.db import get_database, migrate
from hokma.db.seeds import parse_frontmatter, seed_agents
from hokma.kit import hokma_tools, rule_tools
from hokma.kit.integrity import app_root

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "kit" / "template"
CREATORS = ("strategy-researcher", "strategy-coder", "strategy-reviewer")
NEEDED = {"Read", "Write", "Edit", "Agent"}
BANK_ONLY = "mcp__hokma__factory_list, mcp__hokma__source_ingest, mcp__hokma__bank_create, mcp__hokma__bank_list, mcp__hokma__bank_get"
IDEA_ONLY = "I want to buy NQ when the 9 EMA crosses above the 21 EMA on the 5 minute chart, only in the morning."


def tools_of(text):
    return {t.strip() for t in parse_frontmatter(text)["tools"].split(",")}


def _install_kit_agents():
    target = hokma_home() / "agents"
    target.mkdir(parents=True, exist_ok=True)
    for f in (app_root() / "kit" / "template" / "agents").glob("*.md"):
        shutil.copy(f, target / f.name)
    return target


# ---------------------------------------------------------------------------------------------------- 1. the road of the spec
def test_the_text_of_the_tool_and_the_messages_name_the_folder_of_the_strategy_and_no_projects_folder():
    description = rule_tools.TOOLSET.tools["strategy_brief_write"][0]
    assert "service/strategies/STR-{NNN}-{slug}/spec.md" in description and "projects/" not in description
    source = (ROOT / "backend" / "hokma" / "factory" / "user_rules.py").read_text(encoding="utf-8")
    assert "service/strategies/{NNN}-{slug}" not in source and "service/strategies/STR-{NNN}-{slug}/spec.md" in source
    skill = (TEMPLATE / "service" / "skills" / "strategy-requirement" / "SKILL.md").read_text(encoding="utf-8")
    assert "lives only in the base" not in skill and "service/strategies/STR-{NNN}-{slug}/spec.md" in skill
    ian = (TEMPLATE / "service" / "agents" / "strategy-researcher.md").read_text(encoding="utf-8")
    assert "projects/{strategy}" not in ian and "service/strategies/STR-{NNN}-{slug}/adr/ADR-" in ian and "/sprints/week-{n}.md" in ian


def test_the_spec_in_the_folder_of_the_card_is_accepted_by_the_gate_and_by_strategy_brief_write(db):
    bank_id = make_bank(db, spec=False)
    folder = next((hokma_home() / "service" / "strategies").glob("STR-*"))
    code_file = folder / "versions" / "v1" / "strategy.py"
    args = {"bank_id": bank_id, "slug": "road", "title": "Road", "brief": BRIEF}
    assert gate("Write", {"file_path": str(code_file)}, "strategy-researcher") == 2          # no spec.md yet: the gate stops the code
    refused = failed("strategy_brief_write", args, caller(RESEARCHER))
    assert refused["error_code"] == "blocked" and "service/strategies/STR-" in refused["message"] and folder.name in refused["message"]
    (folder / "spec.md").write_text("# Road\n", encoding="utf-8")                              # what Ian writes with Write
    assert gate("Write", {"file_path": str(folder / "spec.md")}, "strategy-researcher") == 0
    assert gate("Write", {"file_path": str(code_file)}, "strategy-coder") == 0
    made = ok("strategy_brief_write", args, caller(RESEARCHER))
    assert made["spec_path"] == f"service/strategies/{folder.name}/spec.md" and made["folder"] == f"service/strategies/{folder.name}"


# ---------------------------------------------------------------------------------------------------- 4. an idea with no source
def test_an_idea_alone_makes_its_card_and_its_folder(db):
    made = ok("bank_create", {"title": "Morning EMA cross", "idea": IDEA_ONLY}, caller(RESEARCHER))
    assert made["status"] == "idea" and made["code"] == f"STR-{made['bank_id']:03d}" and made["complete"] is False
    assert (hokma_home() / made["folder"]).is_dir() and made["folder"] == f"service/strategies/{made['code']}-{made['slug']}"
    got = ok("bank_get", {"bank_id": made["bank_id"]}, caller("hokma"))
    assert got["source"]["kind"] == "text" and got["code"] == made["code"]
    again = ok("bank_create", {"title": "Morning EMA cross", "idea": IDEA_ONLY + " Second try."}, caller(RESEARCHER))
    assert again["bank_id"] != made["bank_id"] and (hokma_home() / again["folder"]).is_dir()


def test_without_a_source_or_an_idea_bank_create_says_what_is_missing(db):
    answer = failed("bank_create", {"title": "Nothing"}, caller(RESEARCHER))
    assert answer["error_code"] == "bad_input" and "idea" in answer["message"]
    answer = failed("bank_create", {"title": "Source without sheet", "source_id": 1}, caller(RESEARCHER))
    assert answer["error_code"] == "bad_input" and "extraction" in answer["message"]
    from hokma.kit import source_tools

    assert source_tools.TOOLSET.tools["bank_create"][2] == ["title"]


# ---------------------------------------------------------------------------------------------------- 6. the registered copy
def test_the_registered_copy_of_ian_sam_and_ben_carries_the_normal_tools_after_seeding(env):
    _install_kit_agents()
    migrate()
    AgentService().reconcile()
    for slug in CREATORS:
        loaded = (hokma_home() / ".claude" / "agents" / f"{slug}.md").read_text(encoding="utf-8")
        assert NEEDED <= tools_of(loaded), slug


def test_an_old_copy_with_only_the_tools_of_the_bank_is_renewed_on_the_next_start(env):
    target = _install_kit_agents()
    migrate()
    AgentService().reconcile()
    kit = app_root() / "kit" / "template" / "service" / "agents"
    for slug in CREATORS:                                                          # what 1.2.1 left in the working folder and in the loaded copy
        old = fl.set_frontmatter_value((kit / f"{slug}.md").read_text(encoding="utf-8"), "tools", BANK_ONLY)
        (target / f"{slug}.md").write_text(old, encoding="utf-8")
        (hokma_home() / ".claude" / "agents" / f"{slug}.md").write_text(old, encoding="utf-8")
    with get_database().write() as u:
        seed_agents(u.session)
    AgentService().reconcile()
    for slug in CREATORS:
        for path in (target / f"{slug}.md", hokma_home() / ".claude" / "agents" / f"{slug}.md"):
            assert NEEDED <= tools_of(path.read_text(encoding="utf-8")), path


def test_a_source_file_that_was_edited_or_narrowed_still_gives_a_registered_copy_with_the_tools(env, monkeypatch):
    """The kit of the student is a git folder whose agent files can be edited: the registered copy is repaired from whatever the source says."""
    target = _install_kit_agents()
    migrate()
    for slug in CREATORS:                                                          # the working copy edited by hand and narrowed, no kit renewal can fix it before apply
        text = (target / f"{slug}.md").read_text(encoding="utf-8")
        (target / f"{slug}.md").write_text(fl.set_frontmatter_value(text, "tools", "Grep, Glob, " + BANK_ONLY) + "\nEdited by hand.\n", encoding="utf-8")
    monkeypatch.setattr("hokma.db.seeds._service_agents", lambda kit_dir, installed_dir: {})          # even with no renewal from the kit
    AgentService().reconcile()
    for slug in CREATORS:
        loaded = (hokma_home() / ".claude" / "agents" / f"{slug}.md").read_text(encoding="utf-8")
        assert NEEDED <= tools_of(loaded) and "Grep" in tools_of(loaded) and "mcp__hokma__bank_create" in tools_of(loaded), slug


def test_the_model_the_owner_chose_for_ian_survives_the_renewal(env):
    _install_kit_agents()
    migrate()
    AgentService().reconcile()
    AgentService().set_model("strategy-researcher", "sonnet")
    with get_database().write() as u:
        seed_agents(u.session)
    AgentService().reconcile()
    loaded = (hokma_home() / ".claude" / "agents" / "strategy-researcher.md").read_text(encoding="utf-8")
    assert parse_frontmatter(loaded)["model"] == "sonnet" and NEEDED <= tools_of(loaded)


def test_ensure_creator_tools_touches_only_the_three_and_only_when_a_tool_is_missing():
    text = "---\nname: x\ntools: Grep, mcp__hokma__bank_get\n---\nbody\n"
    assert fl.ensure_creator_tools("joel", text) == text
    fixed = fl.ensure_creator_tools("strategy-coder", text)
    assert tools_of(fixed) == {"Grep", "mcp__hokma__bank_get", *NEEDED} and fixed.endswith("body\n")
    assert fl.ensure_creator_tools("strategy-coder", fixed) == fixed
    assert set(fl.CREATOR_AGENTS) == set(hokma_tools.CREATOR_SERVICES) == set(CREATORS)


# ---------------------------------------------------------------------------------------------------- 2 and 3. who hands a request to Ian
def test_the_main_agent_routes_a_strategy_request_to_ian_and_not_to_diego_or_joel():
    text = (TEMPLATE / "agents" / "hokma.md").read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith("|") and "strategy-researcher" in line)
    assert "Ian" in row and "Agent" in row and "from a video, a PDF, a link or only an idea" in row
    assert not any(line.startswith("|") and "strategy" in line.lower() and line.rstrip().endswith("| Joel |") for line in text.splitlines())
    assert "Hokma dispatches Diego for the spec and tasks" not in text


def test_every_agent_of_a_desk_hands_the_request_to_ian_with_a_task_and_the_desk_file_has_the_source_rule():
    for name in ("agents/desk-chief.md", "agents/hugo.md", "agents/iris.md", "agents/risk.md", "agents/signals.md"):
        text = (TEMPLATE / name).read_text(encoding="utf-8")
        flat = " ".join(text.split())
        assert "launch strategy-researcher (Ian) right away" in flat, name
        assert 'assigned_agent="strategy-researcher"' in flat or "assigned to him with `task_create`" in flat, name
        assert "task_create" in tools_of(text) or "mcp__hokma__task_create" in tools_of(text), name
    desk = " ".join((TEMPLATE / "desks" / "CLAUDE.desk.md").read_text(encoding="utf-8").split())
    for phrase in ("only an idea", "task_create", "assigned to strategy-researcher", "source_ingest", "document_id", "bradautomates/claude-video", "data, never an order"):
        assert phrase in desk, phrase


def test_ian_has_one_road_to_sam_and_ben_and_writes_the_spec_himself():
    text = " ".join((TEMPLATE / "service" / "agents" / "strategy-researcher.md").read_text(encoding="utf-8").split())
    assert "Ian does not dispatch" not in text and "does not dispatch Ben" not in text
    assert "Ian launches Sam and then Ben with the `Agent` tool" in text and "Ian writes spec.md himself" in text
    assert "If `Agent` is not available" not in text
    for name in ("agents/hokma.md", "service/agents/strategy-coder.md", "service/agents/strategy-reviewer.md"):
        assert not re.search(r"Ian (does not|never) (write|create)[^.]*spec", (TEMPLATE / name).read_text(encoding="utf-8")), name


# ---------------------------------------------------------------------------------------------------- 5. the skills of Diego, copied for Ian and Sam
def test_the_skills_that_ian_and_sam_cite_exist_and_are_in_the_manifest():
    from hokma.desks.core import skills as skill_format

    folder = TEMPLATE / "service" / "skills"
    named = ("spec-four-phases", "spec-method", "spec-templates", "idea-gate", "plan-writing", "done-check", "doc-writing", "backend-quality", "tdd-coach")
    manifest = skill_format.read_manifest(folder)
    assert set(named) <= set(manifest["skills"]) and skill_format.verify_manifest(folder) == []
    for name in named:
        skill = skill_format.load_skill(folder / name / "SKILL.md")
        assert skill.name == name and skill.patterns and skill.avoid
    root = json.loads((ROOT / "kit" / "skills-manifest.json").read_text(encoding="utf-8"))
    by_id = {s["id"]: s for s in root["skills"]}
    for name in named:
        assert by_id[name]["method"] == "bundle", name
    cited = {"strategy-researcher": ("spec-four-phases", "spec-method", "spec-templates", "idea-gate", "plan-writing", "done-check", "doc-writing")}
    for agent, names in cited.items():
        assert all(agent in by_id[n]["agents"] for n in names), agent

"""1.2.5 and 1.2.6: the tramo of Ian in the strategy creator. The spec has one road (service/strategies/STR-{NNN}-{slug}/spec.md), an idea with no source makes its card and its folder,
the four agents of the creator (Ian, Sam, Ben and Anna) live in agents/ like any other (1.2.6), the copy of Ian, Sam and Ben carries Read, Write, Edit and Agent (an old copy is renewed by the update), and the main chat and the agents
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
CREATORS = ("strategy-creator", "strategy-reviewer")
NEEDED = {"mcp__hokma__strategy_code_get"}          # 1.3.0: the Creator and Ben have no tool of the engine; what must always be in their copy is the tools of the creator
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
    creator = (TEMPLATE / "agents" / "strategy-creator.md").read_text(encoding="utf-8")
    assert "projects/{strategy}" not in creator and "Write" not in tools_of(creator)


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


# ---------------------------------------------------------------------------------------------------- 6. the registered copy (1.2.6: the four live in agents/, one source)
SEEDED_FROM_KIT = (*CREATORS, "back-office-clerk")


def _sync(version="1.2.6"):
    from hokma.kit.sync import sync_kit

    return sync_kit(TEMPLATE, version=version)


def test_the_four_agents_are_in_the_agents_folder_of_the_kit_and_not_in_service_agents():
    for slug in SEEDED_FROM_KIT:
        assert (TEMPLATE / "agents" / f"{slug}.md").is_file(), slug
    assert not (TEMPLATE / "service" / "agents").exists()
    for slug in CREATORS:                                                          # the guarantee is the kit text itself
        assert NEEDED <= tools_of((TEMPLATE / "agents" / f"{slug}.md").read_text(encoding="utf-8")), slug


def test_the_four_are_seeded_like_any_agent_of_the_kit_and_the_creators_start_on(env):
    _sync()
    migrate()
    AgentService().reconcile()
    with get_database().read() as u:
        agents = {a.slug: a for a in u.agents.list()}
    for slug in SEEDED_FROM_KIT:
        assert slug in agents and agents[slug].file_path == f"agents/{slug}.md" and not agents[slug].is_custom, slug
        assert (hokma_home() / "agents" / f"{slug}.md").is_file(), slug
    assert all(agents[slug].status == "active" for slug in CREATORS)
    for slug in CREATORS:
        loaded = (hokma_home() / ".claude" / "agents" / f"{slug}.md").read_text(encoding="utf-8")
        assert NEEDED <= tools_of(loaded), slug


def test_an_old_copy_in_agents_is_renewed_by_the_update_and_the_model_of_the_owner_stays(env):
    target = _install_kit_agents()
    migrate()
    AgentService().reconcile()
    for slug in CREATORS:                                                          # what 1.2.1 left in the working folder and in the loaded copy
        old = fl.set_frontmatter_value((TEMPLATE / "agents" / f"{slug}.md").read_text(encoding="utf-8"), "tools", BANK_ONLY)
        (target / f"{slug}.md").write_text(old + "\nEdited by hand.\n", encoding="utf-8")
        (hokma_home() / ".claude" / "agents" / f"{slug}.md").write_text(old, encoding="utf-8")
    AgentService().set_model("strategy-creator", "sonnet")                      # the owner picked the tier in the panel
    report = _sync()
    for slug in CREATORS:
        assert f"agents/{slug}.md" in report.updated and f"agents/{slug}.md" not in report.conflicts, slug
        assert not (target / f"{slug}.md.hokma-new").exists(), slug
        text = (target / f"{slug}.md").read_text(encoding="utf-8")
        assert NEEDED <= tools_of(text) and "Edited by hand" not in text, slug
        assert tools_of(text) == tools_of((TEMPLATE / "agents" / f"{slug}.md").read_text(encoding="utf-8")), slug
    assert parse_frontmatter((target / "strategy-creator.md").read_text(encoding="utf-8"))["model"] == "sonnet"
    assert parse_frontmatter((target / "strategy-reviewer.md").read_text(encoding="utf-8"))["model"] == "opus"
    with get_database().write() as u:
        seed_agents(u.session)
    AgentService().reconcile()
    for slug in CREATORS:
        loaded = (hokma_home() / ".claude" / "agents" / f"{slug}.md").read_text(encoding="utf-8")
        assert NEEDED <= tools_of(loaded), slug
    assert parse_frontmatter((hokma_home() / ".claude" / "agents" / "strategy-creator.md").read_text(encoding="utf-8"))["model"] == "sonnet"
    again = _sync()
    assert not any(f"agents/{slug}.md" in again.updated + again.conflicts for slug in SEEDED_FROM_KIT)          # nothing left to renew


def test_the_old_service_agents_folder_of_the_owner_is_left_alone(env):
    old = hokma_home() / "service" / "agents"
    old.mkdir(parents=True, exist_ok=True)
    (old / "strategy-coder.md").write_text("---\nname: strategy-coder\n---\nmine\n", encoding="utf-8")
    report = _sync()
    assert (old / "strategy-coder.md").read_text(encoding="utf-8").endswith("mine\n")
    assert not any(path.startswith("service/" + "agents/") for path in report.added + report.updated + report.conflicts)
    migrate()
    with get_database().read() as u:
        assert all(a.file_path == f"agents/{a.slug}.md" for a in u.agents.list())          # nothing is read from there


def test_no_file_of_the_kit_or_the_code_names_service_agents():
    needle = "service" + "/agents"
    hits = []
    for base in (ROOT / "kit", ROOT / "backend" / "hokma", ROOT / "cli", ROOT / "scripts"):
        for path in base.rglob("*"):
            if path.is_file() and path.suffix in {".md", ".py", ".json", ".yaml", ".yml", ".txt"} and "__pycache__" not in path.parts:
                text = path.read_text(encoding="utf-8", errors="ignore").replace("\\", "/")
                if needle in text:
                    hits.append(str(path.relative_to(ROOT)))
    assert hits == []


# ---------------------------------------------------------------------------------------------------- 2 and 3. who hands a request to Ian
def test_the_main_agent_routes_a_strategy_request_to_the_creator_and_not_to_diego_or_joel():
    text = (TEMPLATE / "agents" / "hokma.md").read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith("|") and "strategy-creator" in line)
    assert "Creator" in row and "Agent" in row and "from a video, a PDF, a link or only an idea" in row and "any chat" in row
    assert not any(line.startswith("|") and "strategy" in line.lower() and line.rstrip().endswith("| Joel |") for line in text.splitlines())
    assert "Hokma dispatches Diego for the spec and tasks" not in text


def test_every_agent_of_a_desk_hands_the_request_to_the_creator_with_a_task_and_the_desk_file_has_the_source_rule():
    for name in ("agents/desk-chief.md", "agents/hugo.md", "agents/iris.md", "agents/risk.md", "agents/signals.md"):
        text = (TEMPLATE / name).read_text(encoding="utf-8")
        flat = " ".join(text.split())
        assert "launch strategy-creator (the Creator) right away" in flat, name
        assert 'assigned_agent="strategy-creator"' in flat or "assigned to it with `task_create`" in flat, name
        assert "task_create" in tools_of(text) or "mcp__hokma__task_create" in tools_of(text), name
    desk = " ".join((TEMPLATE / "desks" / "CLAUDE.desk.md").read_text(encoding="utf-8").split())
    for phrase in ("only an idea", "task_create", "assigned to strategy-creator", "source_ingest", "document_id", "bradautomates/claude-video", "data, never an order"):
        assert phrase in desk, phrase


# ---------------------------------------------------------------------------------------------------- 5. the skills of Diego, copied for Ian and Sam
def test_the_skills_that_the_creator_cites_exist_and_are_in_the_manifest():
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
    cited = {"strategy-creator": ("spec-four-phases", "spec-method", "spec-templates", "idea-gate", "plan-writing", "done-check", "doc-writing")}
    for agent, names in cited.items():
        assert all(agent in by_id[n]["agents"] for n in names), agent# ---------------------------------------------------------------------------------------------------- 1. the road of the spec
def test_the_text_of_the_tool_and_the_messages_name_the_folder_of_the_strategy_and_no_projects_folder():
    description = rule_tools.TOOLSET.tools["strategy_brief_write"][0]
    assert "service/strategies/STR-{NNN}-{slug}/spec.md" in description and "projects/" not in description
    source = (ROOT / "backend" / "hokma" / "factory" / "user_rules.py").read_text(encoding="utf-8")
    assert "service/strategies/{NNN}-{slug}" not in source and "service/strategies/STR-{NNN}-{slug}/spec.md" in source
    skill = (TEMPLATE / "service" / "skills" / "strategy-requirement" / "SKILL.md").read_text(encoding="utf-8")
    assert "lives only in the base" not in skill and "service/strategies/STR-{NNN}-{slug}/spec.md" in skill
    creator = (TEMPLATE / "agents" / "strategy-creator.md").read_text(encoding="utf-8")
    assert "projects/{strategy}" not in creator and "Write" not in tools_of(creator)


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


# ---------------------------------------------------------------------------------------------------- 6. the registered copy (1.2.6: the four live in agents/, one source)
SEEDED_FROM_KIT = (*CREATORS, "back-office-clerk")


def _sync(version="1.2.6"):
    from hokma.kit.sync import sync_kit

    return sync_kit(TEMPLATE, version=version)


def test_the_four_agents_are_in_the_agents_folder_of_the_kit_and_not_in_service_agents():
    for slug in SEEDED_FROM_KIT:
        assert (TEMPLATE / "agents" / f"{slug}.md").is_file(), slug
    assert not (TEMPLATE / "service" / "agents").exists()
    for slug in CREATORS:                                                          # the guarantee is the kit text itself
        assert NEEDED <= tools_of((TEMPLATE / "agents" / f"{slug}.md").read_text(encoding="utf-8")), slug


def test_the_four_are_seeded_like_any_agent_of_the_kit_and_the_creators_start_on(env):
    _sync()
    migrate()
    AgentService().reconcile()
    with get_database().read() as u:
        agents = {a.slug: a for a in u.agents.list()}
    for slug in SEEDED_FROM_KIT:
        assert slug in agents and agents[slug].file_path == f"agents/{slug}.md" and not agents[slug].is_custom, slug
        assert (hokma_home() / "agents" / f"{slug}.md").is_file(), slug
    assert all(agents[slug].status == "active" for slug in CREATORS)
    for slug in CREATORS:
        loaded = (hokma_home() / ".claude" / "agents" / f"{slug}.md").read_text(encoding="utf-8")
        assert NEEDED <= tools_of(loaded), slug


def test_an_old_copy_in_agents_is_renewed_by_the_update_and_the_model_of_the_owner_stays(env):
    target = _install_kit_agents()
    migrate()
    AgentService().reconcile()
    for slug in CREATORS:                                                          # what 1.2.1 left in the working folder and in the loaded copy
        old = fl.set_frontmatter_value((TEMPLATE / "agents" / f"{slug}.md").read_text(encoding="utf-8"), "tools", BANK_ONLY)
        (target / f"{slug}.md").write_text(old + "\nEdited by hand.\n", encoding="utf-8")
        (hokma_home() / ".claude" / "agents" / f"{slug}.md").write_text(old, encoding="utf-8")
    AgentService().set_model("strategy-creator", "sonnet")                      # the owner picked the tier in the panel
    report = _sync()
    for slug in CREATORS:
        assert f"agents/{slug}.md" in report.updated and f"agents/{slug}.md" not in report.conflicts, slug
        assert not (target / f"{slug}.md.hokma-new").exists(), slug
        text = (target / f"{slug}.md").read_text(encoding="utf-8")
        assert NEEDED <= tools_of(text) and "Edited by hand" not in text, slug
        assert tools_of(text) == tools_of((TEMPLATE / "agents" / f"{slug}.md").read_text(encoding="utf-8")), slug
    assert parse_frontmatter((target / "strategy-creator.md").read_text(encoding="utf-8"))["model"] == "sonnet"
    assert parse_frontmatter((target / "strategy-reviewer.md").read_text(encoding="utf-8"))["model"] == "opus"
    with get_database().write() as u:
        seed_agents(u.session)
    AgentService().reconcile()
    for slug in CREATORS:
        loaded = (hokma_home() / ".claude" / "agents" / f"{slug}.md").read_text(encoding="utf-8")
        assert NEEDED <= tools_of(loaded), slug
    assert parse_frontmatter((hokma_home() / ".claude" / "agents" / "strategy-creator.md").read_text(encoding="utf-8"))["model"] == "sonnet"
    again = _sync()
    assert not any(f"agents/{slug}.md" in again.updated + again.conflicts for slug in SEEDED_FROM_KIT)          # nothing left to renew


def test_the_old_service_agents_folder_of_the_owner_is_left_alone(env):
    old = hokma_home() / "service" / "agents"
    old.mkdir(parents=True, exist_ok=True)
    (old / "strategy-coder.md").write_text("---\nname: strategy-coder\n---\nmine\n", encoding="utf-8")
    report = _sync()
    assert (old / "strategy-coder.md").read_text(encoding="utf-8").endswith("mine\n")
    assert not any(path.startswith("service/" + "agents/") for path in report.added + report.updated + report.conflicts)
    migrate()
    with get_database().read() as u:
        assert all(a.file_path == f"agents/{a.slug}.md" for a in u.agents.list())          # nothing is read from there


def test_no_file_of_the_kit_or_the_code_names_service_agents():
    needle = "service" + "/agents"
    hits = []
    for base in (ROOT / "kit", ROOT / "backend" / "hokma", ROOT / "cli", ROOT / "scripts"):
        for path in base.rglob("*"):
            if path.is_file() and path.suffix in {".md", ".py", ".json", ".yaml", ".yml", ".txt"} and "__pycache__" not in path.parts:
                text = path.read_text(encoding="utf-8", errors="ignore").replace("\\", "/")
                if needle in text:
                    hits.append(str(path.relative_to(ROOT)))
    assert hits == []


# ---------------------------------------------------------------------------------------------------- 2 and 3. who hands a request to Ian
def test_the_main_agent_routes_a_strategy_request_to_the_creator_and_not_to_diego_or_joel():
    text = (TEMPLATE / "agents" / "hokma.md").read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith("|") and "strategy-creator" in line)
    assert "Creator" in row and "Agent" in row and "from a video, a PDF, a link or only an idea" in row and "any chat" in row
    assert not any(line.startswith("|") and "strategy" in line.lower() and line.rstrip().endswith("| Joel |") for line in text.splitlines())
    assert "Hokma dispatches Diego for the spec and tasks" not in text


def test_every_agent_of_a_desk_hands_the_request_to_the_creator_with_a_task_and_the_desk_file_has_the_source_rule():
    for name in ("agents/desk-chief.md", "agents/hugo.md", "agents/iris.md", "agents/risk.md", "agents/signals.md"):
        text = (TEMPLATE / name).read_text(encoding="utf-8")
        flat = " ".join(text.split())
        assert "launch strategy-creator (the Creator) right away" in flat, name
        assert 'assigned_agent="strategy-creator"' in flat or "assigned to it with `task_create`" in flat, name
        assert "task_create" in tools_of(text) or "mcp__hokma__task_create" in tools_of(text), name
    desk = " ".join((TEMPLATE / "desks" / "CLAUDE.desk.md").read_text(encoding="utf-8").split())
    for phrase in ("only an idea", "task_create", "assigned to strategy-creator", "source_ingest", "document_id", "bradautomates/claude-video", "data, never an order"):
        assert phrase in desk, phrase


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
    cited = {"strategy-creator": ("spec-four-phases", "spec-method", "spec-templates", "idea-gate", "plan-writing", "done-check", "doc-writing")}
    for agent, names in cited.items():
        assert all(agent in by_id[n]["agents"] for n in names), agent

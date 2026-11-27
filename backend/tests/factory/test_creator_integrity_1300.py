"""1.3.0 (task 3433), D: the agents of the strategy creator. One Creator and Ben; Ian, Sam and his three helpers are retired; the general development agents stay as they were."""
import importlib.util
import re
from pathlib import Path

from dbsupport import env  # noqa: F401  (fixture)

from hokma.db import get_database, migrate
from hokma.db.seeds import RETIRED_AGENTS, parse_frontmatter, seed_agents
from hokma.factory import creator, reviewer
from hokma.kit import hokma_tools

ROOT = Path(__file__).resolve().parents[3]
AGENTS = ROOT / "kit" / "template" / "agents"
_spec = importlib.util.spec_from_file_location("check_repo_rules", ROOT / "scripts" / "check_repo_rules.py")
repo_rules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repo_rules)
FORBIDDEN_TOOLS = r"exec|order|desk|broker|submit|approve|flatten|premium_calibrate|review_approve|request_changes"


def tools_of(slug):
    return [t.strip() for t in parse_frontmatter((AGENTS / f"{slug}.md").read_text(encoding="utf-8"))["tools"].split(",")]


def test_the_creator_has_the_tools_of_its_module_no_tool_of_the_engine_and_nothing_that_moves_money():
    meta = parse_frontmatter((AGENTS / "strategy-creator.md").read_text(encoding="utf-8"))
    assert meta["name"] == meta["slug"] == creator.AGENT_SLUG == "strategy-creator" and meta["reports_to"] == "hokma" and meta["model"] == creator.DEFAULT_MODEL
    tools = tools_of("strategy-creator")
    assert tools == [creator.MCP_PREFIX + t for t in creator.TOOLS]
    assert not {"Read", "Write", "Edit", "Bash", "Agent", "Glob", "Grep"} & set(tools)                     # no Write, no path: the spec and the code go inside the call
    assert {"mcp__hokma__strategy_dry_run", "mcp__hokma__strategy_create"} <= set(tools)
    assert not any(re.search(FORBIDDEN_TOOLS, t) for t in tools)
    assert not any(t.startswith("mcp__hokma__strategy_code_") and t.endswith(("save", "verdict")) for t in tools)
    text = (AGENTS / "strategy-creator.md").read_text(encoding="utf-8")
    assert text.isascii() and "\u2014" not in text and repo_rules.check(ROOT, ["kit/template/agents/strategy-creator.md"]) == [] and len(text.splitlines()) <= 120
    for needle in ("PARAMS", "generate_signals", "session_id", "is_rth", "stop", "anchor", "Ben", "dry_run", "measure"):
        assert needle in text, needle


def test_ben_is_short_reads_only_and_judges_the_logic_without_repeating_the_checks():
    path = AGENTS / "strategy-reviewer.md"
    text = path.read_text(encoding="utf-8")
    meta = parse_frontmatter(text)
    assert meta["slug"] == reviewer.AGENT_SLUG == "strategy-reviewer" and len(text.splitlines()) <= 80
    tools = tools_of("strategy-reviewer")
    assert tools == [reviewer.MCP_PREFIX + t for t in reviewer.TOOLS] and "mcp__hokma__strategy_code_verdict" in tools
    assert not {"Read", "Write", "Edit", "Bash", "Agent"} & set(tools) and not any(re.search(FORBIDDEN_TOOLS, t) for t in tools)
    assert text.isascii() and "\u2014" not in text and repo_rules.check(ROOT, ["kit/template/agents/strategy-reviewer.md"]) == []
    assert "Do not repeat" in text and "ARIA" not in text and "Axel" not in text and "Vera" not in text


def test_ian_sam_and_the_three_helpers_are_gone_and_the_general_development_agents_stay():
    for gone in ("strategy-researcher", "strategy-coder", "sam-dev-python", "sam-dev-frontend", "sam-mcp-builder"):
        assert not (AGENTS / f"{gone}.md").exists() and gone in RETIRED_AGENTS
    for stays in ("joel", "diego", "vera", "axel", "joel-dev-python", "joel-dev-frontend", "joel-mcp-builder"):
        assert (AGENTS / f"{stays}.md").is_file(), stays
    assert hokma_tools.CREATOR_SERVICES == ("strategy-creator", "strategy-reviewer")
    for path in (ROOT / "kit" / "template").rglob("*.md"):
        if "service/skills" in path.as_posix():
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        assert "strategy-researcher" not in content and "strategy-coder" not in content and "sam-dev-" not in content, path


def test_a_copy_of_ian_left_in_an_old_installation_becomes_a_retired_agent_not_a_live_one(env, tmp_path):
    migrate()
    installed = tmp_path / "agents"
    installed.mkdir()
    for slug in ("strategy-researcher", "strategy-coder", "strategy-creator", "strategy-reviewer"):
        (installed / f"{slug}.md").write_text((AGENTS / "strategy-creator.md").read_text(encoding="utf-8").replace("strategy-creator", slug).replace("display_name: Creator", f"display_name: {slug}"), encoding="utf-8")
    with get_database().write() as u:
        u.agents.upsert_kit("strategy-researcher", "Ian", None, "deep", "agents/strategy-researcher.md")           # the row that 1.2.9 left
        u.agents.set_status("strategy-researcher", "active")
        seed_agents(u.session, workdir_agents=installed, kit_dir=AGENTS)
        old, new = u.agents.find("strategy-researcher"), u.agents.find("strategy-creator")
        assert u.agents.find("strategy-coder") is None                                                         # never inserted
    assert old.status == "off"                                                                                 # turned off, not deleted
    assert new is not None and not new.is_custom


def test_the_main_agent_the_desk_chief_and_the_seats_send_a_strategy_request_to_the_creator_from_any_chat():
    rule = "launch strategy-creator (the Creator) right away"
    for name in ("agents/desk-chief.md", "agents/signals.md", "agents/iris.md", "agents/hugo.md", "agents/risk.md", "CLAUDE.md", "desks/CLAUDE.desk.md"):
        assert rule in (ROOT / "kit" / "template" / name).read_text(encoding="utf-8"), name
    assert "The Creator (`strategy-creator`), launched at once with `Agent` from any chat" in (AGENTS / "hokma.md").read_text(encoding="utf-8")
    no_dispatch = ("agents/desk-chief.md", "agents/signals.md", "agents/iris.md", "agents/hugo.md", "agents/risk.md", "desks/CLAUDE.desk.md")
    for name in no_dispatch:
        assert 'assigned_agent="strategy-creator"' in (ROOT / "kit" / "template" / name).read_text(encoding="utf-8").replace("assigned to strategy-creator", 'assigned_agent="strategy-creator"') or "assigned to strategy-creator" in (ROOT / "kit" / "template" / name).read_text(encoding="utf-8") or "assigned to it" in (ROOT / "kit" / "template" / name).read_text(encoding="utf-8"), name
    for slug in ("hokma", "desk-chief"):
        assert {"mcp__hokma__strategy_dry_run", "mcp__hokma__strategy_create"} <= set(tools_of(slug)), slug

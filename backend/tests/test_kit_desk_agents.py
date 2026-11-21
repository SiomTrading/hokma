"""Kit carrier of the money desks: the six desk agents and the desk folder template."""
import re
from pathlib import Path

import pytest
from dbsupport import env  # noqa: F401  (fixture)

from hokma.db import get_database, migrate
from hokma.db.seeds import parse_frontmatter
from hokma.kit.sync import sync_kit

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "kit" / "template"
AGENTS = TEMPLATE / "agents"
DESK_AGENTS = {
    "desk-chief": ("sonnet", "balanced"),
    "signals": ("sonnet", "balanced"),
    "risk": ("haiku", "fast"),
    "bull-case": ("sonnet", "balanced"),
    "bear-case": ("sonnet", "balanced"),
    "judge": ("sonnet", "balanced"),
}
CHIEF_FORBIDDEN = {"Agent", "Bash", "Write", "Edit", "mcp__hokma__agent_save", "mcp__hokma__skill_save",
                   "mcp__hokma__skill_install", "mcp__hokma__extension_request", "mcp__hokma__schedule_create",
                   "mcp__hokma__schedule_list", "mcp__hokma__bug_report", "mcp__hokma__bug_update",
                   "mcp__hokma__bug_list", "mcp__hokma__trash_move", "mcp__hokma__project_create", "mcp__hokma__desk_create"}
DESK_FILES = [AGENTS / f"{slug}.md" for slug in DESK_AGENTS] + [TEMPLATE / "desks" / "CLAUDE.desk.md"]


def tools_of(slug: str) -> list[str]:
    meta = parse_frontmatter((AGENTS / f"{slug}.md").read_text(encoding="utf-8"))
    return [t.strip() for t in meta["tools"].split(",") if t.strip()]


def real_tools() -> set[str]:
    from hokma.kit import hokma_tools

    return {f"mcp__hokma__{name}" for name in set(hokma_tools.TOOLS) | {"trash_move"}}


@pytest.mark.parametrize("slug", sorted(DESK_AGENTS))
def test_the_frontmatter_is_valid(slug):
    model, tier = DESK_AGENTS[slug]
    text = (AGENTS / f"{slug}.md").read_text(encoding="utf-8")
    meta = parse_frontmatter(text)
    assert meta["name"] == meta["slug"] == slug
    assert meta["display_name"] and meta["description"]
    assert meta["model"] == model and meta["hokma-tier"] == tier
    assert meta["role"] == "specialist"           # starts off: only the orchestrator and sub-agents start on
    assert tools_of(slug)
    for key in ("name", "display_name", "role", "slug", "description", "model", "hokma-tier", "tools"):
        assert len(re.findall(rf"^{key}:", text.split("\n---\n")[0], re.M)) == 1


@pytest.mark.parametrize("slug", sorted(DESK_AGENTS))
def test_every_tool_exists_and_none_can_start_agents_or_run_commands(slug):
    tools = tools_of(slug)
    assert len(tools) == len(set(tools))
    for tool in tools:
        assert not tool.startswith("mcp__") or tool in real_tools(), tool
    assert not {"Agent", "Bash", "Write", "Edit"} & set(tools)


def test_the_desk_chief_has_no_dispatch_and_none_of_the_forbidden_tools():
    tools = set(tools_of("desk-chief"))
    assert not CHIEF_FORBIDDEN & tools
    assert {"mcp__hokma__task_create", "mcp__hokma__task_link", "mcp__hokma__journal_write",
            "mcp__hokma__decision_propose", "mcp__hokma__project_show"} <= tools


def test_the_desk_chief_reads_desk_show_before_deciding():
    assert {"mcp__hokma__desk_show", "mcp__hokma__desk_list"} <= set(tools_of("desk-chief"))
    body = (AGENTS / "desk-chief.md").read_text(encoding="utf-8")
    before = body.split("## Before every decision")[1].split("---")[0]
    assert "`desk_show`" in before


def test_nobody_on_the_desk_can_change_money_rules_or_agents():
    for slug in DESK_AGENTS:
        assert not {"mcp__hokma__agent_save", "mcp__hokma__skill_install", "mcp__hokma__schedule_create",
                    "mcp__hokma__trash_move"} & set(tools_of(slug)), slug


def test_the_judge_does_not_read_the_journal():
    assert not {t for t in tools_of("judge") if "journal" in t or "ips_read" in t}


def test_the_debate_agents_keep_the_daily_bar_warning():
    for slug in ("bull-case", "bear-case"):
        assert "daily bars" in " ".join((AGENTS / f"{slug}.md").read_text(encoding="utf-8").split())


def test_the_desk_folder_template_has_its_markers_and_the_fixed_rule():
    text = (TEMPLATE / "desks" / "CLAUDE.desk.md").read_text(encoding="utf-8")
    for marker in ("{{desk_name}}", "{{mandate}}", "{{capital}}", "{{owner_name}}"):
        assert marker in text
    assert "Real money moves only through Hokma's order tools." in text


def test_no_new_file_has_a_long_dash_or_origin_wording():
    for path in DESK_FILES:
        text = path.read_text(encoding="utf-8")
        assert chr(0x2014) not in text, path
        assert not re.search(r"student|task \d+", text, re.I), path


def test_the_template_files_are_in_the_install_and_the_owner_name_is_filled(env):
    work = env / "work"
    work.mkdir(parents=True, exist_ok=True)
    report = sync_kit(TEMPLATE, version="9.9.9", values={"owner_name": "Ana", "fund_name": "Fondo"})
    for slug in DESK_AGENTS:
        assert f"agents/{slug}.md" in report.added
        assert "{{owner_name}}" not in (work / "agents" / f"{slug}.md").read_text(encoding="utf-8")
    assert "desks/CLAUDE.desk.md" in report.added
    assert "{{desk_name}}" in (work / "desks" / "CLAUDE.desk.md").read_text(encoding="utf-8")   # filled per desk later


def test_an_existing_install_gets_the_new_agents_without_touching_the_owners_files(env):
    """An install from before the desks: the owner has its own `signals.md` and edited `hugo.md`."""
    work = env / "work"
    old = env / "old-kit"
    for path in TEMPLATE.rglob("*"):
        rel = path.relative_to(TEMPLATE)
        if path.is_file() and rel.as_posix() not in {f"agents/{s}.md" for s in DESK_AGENTS} | {"desks/CLAUDE.desk.md"}:
            (old / rel).parent.mkdir(parents=True, exist_ok=True)
            (old / rel).write_bytes(path.read_bytes())
    values = {"owner_name": "Ana", "fund_name": "Fondo"}
    sync_kit(old, version="0.0.39", values=values)
    mine = work / "agents" / "signals.md"
    mine.write_text("---\nname: signals\nslug: signals\nmodel: opus\n---\nMy own signals agent.\n", encoding="utf-8")
    hugo = work / "agents" / "hugo.md"
    hugo.write_text(hugo.read_text(encoding="utf-8") + "\nMy own rule.\n", encoding="utf-8")
    hugo_text = hugo.read_text(encoding="utf-8")

    report = sync_kit(TEMPLATE, version="0.0.41", values=values)

    assert mine.read_text(encoding="utf-8").endswith("My own signals agent.\n")           # never overwritten
    assert "agents/signals.md" in report.conflicts and (work / "agents" / "signals.md.hokma-new").is_file()
    assert hugo.read_text(encoding="utf-8") == hugo_text
    for slug in set(DESK_AGENTS) - {"signals"}:
        assert f"agents/{slug}.md" in report.added


def test_the_six_agents_are_seeded_off_with_their_tier(env):
    migrate()
    with get_database().read() as u:
        agents = {a.slug: a for a in u.agents.list()}
    for slug, (_, tier) in DESK_AGENTS.items():
        assert agents[slug].status == "off" and agents[slug].model_tier == tier and not agents[slug].is_custom
        assert agents[slug].file_path == f"agents/{slug}.md"
        assert "{{" not in (agents[slug].description or "")

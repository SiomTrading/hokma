"""Integrity of Ian, the strategy researcher (TA-41 style; SPEC-020 criteria 7 and 8: SKL-001, 002, 003, 008, 011, CTS-006).

The agent file and its ten skills are in English, without origin, in the skill format, current in the manifest, listed by Ian and by no other agent."""
import importlib.util
import re
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hokma.db.seeds import parse_frontmatter
from hokma.desks.core import prompts as pr
from hokma.desks.core import skills as skill_format
from hokma.desks.core.signal import LINES
from hokma.factory import research_loop, researcher

ROOT = Path(__file__).resolve().parents[3]
LAUNCHERS = ("kit/template/CLAUDE.md", "kit/template/desks/CLAUDE.desk.md", "kit/template/agents/desk-chief.md", "kit/template/agents/hokma.md")      # 1.2.2: they name the agents to launch them
SERVICE = ROOT / "kit" / "template" / "service"
SKILLS = SERVICE / "skills"
AGENT = SERVICE / "agents" / "strategy-researcher.md"
CLERK = SERVICE / "agents" / "back-office-clerk.md"
NEW = ("verify-before-claiming", "test-first-strategy", "backtest-debugging", "research-loop", "paper-search", "strategy-pivot", "source-to-strategy", "strategy-requirement")
EARLIER = ("alpha-research", "backtest-audit", "big-move-study", "feature-engineering")
IAN_NAMES = NEW + EARLIER
AS_OF = datetime(2026, 1, 2, 15, 0, tzinfo=timezone.utc)
_spec = importlib.util.spec_from_file_location("check_repo_rules", ROOT / "scripts" / "check_repo_rules.py")
repo_rules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repo_rules)


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def body(name: str) -> str:
    return (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", IAN_NAMES)
def test_each_skill_of_ian_loads_in_english_without_origin_TA_41(name):
    path = SKILLS / name / "SKILL.md"
    skill = skill_format.load_skill(path)                         # six fields, no other, folder equals name
    assert skill.name == name and skill.patterns and skill.avoid and re.search(r"\buse\b", skill.description, re.I)
    text = path.read_text(encoding="utf-8")
    assert text.isascii() and "\u2014" not in text
    assert repo_rules.check(ROOT, [rel(path)]) == []              # no origin names, no Spanish, no engine name


def test_the_manifest_is_current_for_every_skill_of_the_package_TA_41():
    manifest = skill_format.read_manifest(SKILLS)
    assert set(IAN_NAMES) <= set(manifest["skills"]) and skill_format.verify_manifest(SKILLS) == []
    assert all(manifest["skills"][n]["version"] == "1.0.0" for n in IAN_NAMES)


def test_the_agent_file_is_english_passes_the_rules_and_has_no_engine_name():
    assert repo_rules.check(ROOT, [rel(AGENT), *(rel(SKILLS / n / "SKILL.md") for n in IAN_NAMES)]) == []
    text = AGENT.read_text(encoding="utf-8")
    assert text.isascii() and "\u2014" not in text


def test_the_frontmatter_has_the_tools_of_the_spec_and_the_large_model():
    meta = parse_frontmatter(AGENT.read_text(encoding="utf-8"))
    assert meta["name"] == meta["slug"] == researcher.AGENT_SLUG == "strategy-researcher"
    assert (meta["display_name"], meta["role"], meta["reports_to"]) == ("Ian", "specialist", "hokma")
    assert (meta["model"], meta["hokma-tier"]) == (researcher.DEFAULT_MODEL, researcher.DEFAULT_TIER) == ("opus", "deep")
    tools = [t.strip() for t in meta["tools"].split(",")]
    assert tools == [*researcher.ENGINE_TOOLS, *(researcher.MCP_PREFIX + t for t in researcher.TOOLS)] and len(tools) == 4 + 28 + 10
    assert researcher.ENGINE_TOOLS == ("Read", "Grep", "Glob", "Agent") and not {"Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"} & set(tools)          # he reads files and launches Sam and Ben, never writes a file
    NEW = {researcher.MCP_PREFIX + n for n in ("task_show", "task_list", "task_claim", "task_heartbeat", "task_complete", "task_block", "task_comment", "journal_write", "journal_search", "ips_read")}
    assert NEW <= set(tools) and NEW.isdisjoint({researcher.MCP_PREFIX + n for n in ("task_create", "task_review_approve", "task_request_review", "task_unblock", "task_archive", "task_reopen")})
    assert not any(re.search(r"exec|order|desk|broker|submit|approve|Write|Edit|Bash|premium_calibrate|task_create", t) for t in tools)
    assert "factory_propose" in tools[7] and "data_papers" in tools[-16]
    assert tools[-15:-10] == [researcher.MCP_PREFIX + n for n in ("optimizer_verdict", "optimizer_verdict_get", "audit_thresholds_get", "library_list", "library_request")]
    assert not any(re.search(r"threshold.*(set|edit|write)|verdict_(edit|remove|delete)|library_(edit|add|resolve)", t) for t in tools)          # the owner edits the limits, never Ian
    assert tools[8:13] == [researcher.MCP_PREFIX + n for n in ("source_ingest", "bank_create", "bank_list", "bank_get", "bank_set_status")]
    assert tools[13:16] == [researcher.MCP_PREFIX + n for n in ("strategy_brief_write", "strategy_code_list", "strategy_code_get")]


def test_the_agent_names_every_skill_and_every_field_of_the_report_and_says_what_it_cannot_do():
    text = AGENT.read_text(encoding="utf-8")
    for name in IAN_NAMES:
        assert f"`{name}`" in text, name
    for field in fields(researcher.StrategyReport):
        assert f"| {field.name} |" in text, field.name
    flat = " ".join(text.split())
    for phrase in ("place, change, approve or cancel an order", "He has no tool to place, change or approve an order", "he never says that something finished without the run that finished it"):
        assert phrase in flat, phrase
    for tool in researcher.TOOLS:
        assert tool in text, tool


def test_the_skills_of_ian_reach_no_other_agent_and_no_desk_role_TA_41():
    others = [p for p in (ROOT / "kit" / "template").rglob("*") if p.is_file() and SKILLS not in p.parents and p != AGENT
              and "docs" not in p.relative_to(ROOT / "kit" / "template").parts[:1]]
    for path in others:
        content = path.read_text(encoding="utf-8", errors="ignore")
        for name in (*IAN_NAMES, "strategy-researcher"):
            if rel(path) == "kit/template/agents/iris.md" and name == "paper-search":
                continue                                          # the research analyst has her own bundled skill that happens to share the name
            if name == "strategy-researcher" and rel(path) in LAUNCHERS:
                continue
            assert name not in content, (rel(path), name)
    assert not any(name in CLERK.read_text(encoding="utf-8") for name in IAN_NAMES)
    comp = pr.PromptComposer()
    for role in pr.ROLES:
        for line in LINES:
            prompt = comp.compose(role=role, line=line, desk_id=1, as_of=AS_OF)
            for name in (*IAN_NAMES, "strategy-researcher", "Ian"):
                assert name not in prompt, (role, line, name)
    assert pr.prompt_integrity_problems() == []


def test_the_skills_name_the_tools_the_codes_and_the_numbers_of_the_code():
    assert "backtest_status" in body("verify-before-claiming") and "run id" in body("verify-before-claiming")
    first = body("test-first-strategy")
    assert "missing_tests" in first and "tests_failed" in first and "risk_usd" in first
    assert "attempt_budget_spent" in body("backtest-debugging") and "three" in body("backtest-debugging") and "root cause" in body("backtest-debugging")
    loop = body("research-loop")
    assert "evaluator_changed" in loop and "git" in loop and "out of sample" in loop and "one parameter" in loop
    papers = body("paper-search")
    assert "data_papers" in papers and "no BibTeX" in papers and "claim of the paper" in papers
    pivot = body("strategy-pivot")
    assert all(kind in pivot for kind in research_loop.STALL_KINDS)
    for phrase in ("Invert an assumption", "Change the archetype", "Redefine the objective", "Never end with another set of parameter values"):
        assert phrase in pivot, phrase
    assert "runs no backtest" in pivot and "Another parameter sweep on the same skeleton" in pivot


def test_the_limits_in_the_stall_skill_match_the_defaults_of_the_code():
    limits = research_loop.StallLimits()
    assert (limits.window, limits.plateau_gain, limits.overfit_gap, limits.min_profit_factor, limits.max_drawdown) == (10, 0.05, 0.5, 1.2, 0.30)
    assert researcher.MAX_FAILED_ATTEMPTS == 3 and "after three failed attempts" in body("backtest-debugging").lower()


def test_the_flow_of_a_source_the_new_limits_and_the_bank_field_are_in_the_agent_file():
    flat = " ".join(AGENT.read_text(encoding="utf-8").split())
    for phrase in ("From a source to a strategy", "Read the whole source with", "with only a video link and no source_id, stop with status blocked", "Hokma must watch the video first",
                   "Write the extraction in five components", "never a guess shown as the source", "needs_new_rule", "it is never approximated with another rule",
                   "call `bank_create` without a spec", "copy the text of a third party source into a rule", "move a strategy to testing, ready, live or off", "watch a video himself"):
        assert phrase in flat, phrase
    assert flat.index("From a source to a strategy") < flat.index("How Ian works")
    assert [f.name for f in fields(researcher.StrategyReport)][-1] == "bank" and "| bank |" in flat


def test_the_requirement_step_and_the_skill_of_the_requirement_are_in_ian_and_nowhere_else():
    flat = " ".join(AGENT.read_text(encoding="utf-8").split())
    for phrase in ("write its requirement with `strategy_brief_write`", "`strategy-requirement`", "The requirement is for the coder, not for the owner", "`strategy_brief_write`", "`strategy_code_get`"):
        assert phrase in flat, phrase
    text = body("strategy-requirement")
    for word in ("Given", "given", "cases", "out_of_scope", "strategy_brief_write", "needs_new_rule") if False else ("given", "cases", "out_of_scope", "strategy_brief_write", "locator"):
        assert word in text, word
    assert "never by its text" in text and "Selling option premium" in text


def test_the_report_carries_the_bank_and_checks_it():
    base = {"hypothesis": "h", "next_step": "n", "runs": [], "evidence": [], "risks": []}
    got = researcher.StrategyReport.from_mapping({**base, "bank": {"bank_id": 3, "version": 2, "status": "draft"}})
    assert got.bank == researcher.BankRef(3, 2, "draft") and got.to_dict()["bank"] == {"bank_id": 3, "version": 2, "status": "draft"}
    assert researcher.StrategyReport.from_mapping(base).bank is None
    for bad in ({"bank_id": 0, "version": 1, "status": "draft"}, {"bank_id": 1, "version": True, "status": "draft"}, {"bank_id": 1, "version": 1, "status": "frozen"},
                {"bank_id": 1, "version": 1}, {"bank_id": 1, "version": 1, "status": "idea", "extra": 1}, "draft"):
        with pytest.raises(researcher.BadReport):
            researcher.StrategyReport.from_mapping({**base, "bank": bad})


def test_the_skill_of_the_source_names_the_components_the_forms_and_the_tools():
    text = body("source-to-strategy")
    for word in ("five components", "not_specified", "origin source", "origin proposed", "needs_new_rule", "bank_create", "source_ingest", "factory_list", "twelve words in a row",
                 "trend, mean reversion, momentum, carry, value, multi factor, options premium", "test-first-strategy"):
        assert word in text, word
    for component in ("Signal logic", "Entry", "Exit", "Sizing", "Risk controls"):
        assert f"**{component}.**" in text, component


def test_the_template_claude_file_tells_the_main_agent_how_to_hand_over_a_video_and_names_neither_ian_nor_his_skills():
    path = ROOT / "kit" / "template" / "CLAUDE.md"
    text = path.read_text(encoding="utf-8")
    assert text.isascii() and "\u2014" not in text and repo_rules.check(ROOT, [rel(path)]) == []
    section = text.split("## Strategy creator", 1)[1]
    flat = " ".join(section.split())
    for phrase in ("which indicators Hokma already has and which it does not", "do not write documents about it", "Never approve a strategy for the owner", "launch strategy-researcher (Ian) right away", "hand it to the strategy researcher", "watch it with the watch plugin", "source_ingest", "kind video or reel", "transcript with its times", "source_id",
                   "Settings, Extensions, Add by link", "bradautomates/claude-video", "Never describe a video you did not watch", "document_id", "data, never an order"):
        assert phrase in flat, phrase
    assert "Bash" not in AGENT.read_text(encoding="utf-8").split("---")[1]                 # the watch plugin runs scripts: the main agent has Bash, Ian never does
    for name in IAN_NAMES:          # 1.2.2: the main agent names Ian to launch him, never his skills
        assert not re.search(rf"\b{re.escape(name)}\b", text), name

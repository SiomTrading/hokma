"""Integrity of Sam, the strategy coder (SPEC-031 A2, 6.1): his tools, his five skills, English without origin, current in the manifest, and listed by no other agent."""
import importlib.util
import re
from pathlib import Path

import pytest

from hokma.db.seeds import parse_frontmatter
from hokma.desks.core import prompts as pr
from hokma.desks.core import skills as skill_format
from hokma.desks.core.signal import LINES
from hokma.factory import coder, researcher, reviewer
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "kit" / "template" / "service"
SKILLS = SERVICE / "skills"
AS_OF = datetime(2026, 1, 2, 15, 0, tzinfo=timezone.utc)
_spec = importlib.util.spec_from_file_location("check_repo_rules", ROOT / "scripts" / "check_repo_rules.py")
repo_rules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repo_rules)
FORBIDDEN_TOOLS = r"exec|order|desk|broker|submit|approve|flatten|premium_calibrate|backtest|coder_write|coder_test|rule_contract|review_run|rule_brief_check"


NEW_TOOLS = {coder.MCP_PREFIX + n for n in ("task_show", "task_list", "task_claim", "task_heartbeat", "task_complete", "task_block", "task_comment", "task_request_review", "journal_write", "journal_search", "ips_read",)}      # 1.2.2: the tools of tasks, journal and policy that the desks ask of every agent


def rel(path):
    return path.relative_to(ROOT).as_posix()


def body(name):
    return (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


def skills_are_clean(names, agent_path):
    for name in names:
        path = SKILLS / name / "SKILL.md"
        skill = skill_format.load_skill(path)                         # six fields, no other, folder equals name
        assert skill.name == name and skill.version == "1.0.0" and skill.patterns and skill.avoid and re.search(r"\buse\b", skill.description, re.I)
        text = path.read_text(encoding="utf-8")
        assert text.isascii() and "\u2014" not in text
        assert repo_rules.check(ROOT, [rel(path)]) == []
    assert repo_rules.check(ROOT, [rel(agent_path)]) == [] and agent_path.read_text(encoding="utf-8").isascii() and "\u2014" not in agent_path.read_text(encoding="utf-8")
    manifest = skill_format.read_manifest(SKILLS)
    assert set(names) <= set(manifest["skills"]) and skill_format.verify_manifest(SKILLS) == []


def skills_reach_no_one_else(names, own_agent):
    others = [p for p in (ROOT / "kit" / "template").rglob("*") if p.is_file() and SKILLS not in p.parents and p != own_agent and p.relative_to(ROOT / "kit" / "template").parts[0] != "docs"]
    for path in others:
        content = path.read_text(encoding="utf-8", errors="ignore")
        for name in names:
            assert name not in content, (rel(path), name)
    comp = pr.PromptComposer()
    for role in pr.ROLES:
        for line in LINES:
            prompt = comp.compose(role=role, line=line, desk_id=1, as_of=AS_OF)
            for name in (*names, own_agent.stem, "Sam", "Ben"):
                assert name not in prompt, (role, line, name)
    assert pr.prompt_integrity_problems() == []

AGENT = SERVICE / "agents" / "strategy-coder.md"
NAMES = ("strategy-code-contract", "code-test-first", "code-root-cause", "code-done-check", "review-reply")


def test_the_five_skills_and_the_agent_file_are_in_english_in_the_skill_format_and_current_in_the_manifest():
    skills_are_clean(NAMES, AGENT)


def test_the_frontmatter_has_exactly_the_tools_of_the_spec_and_nothing_that_moves_money():
    meta = parse_frontmatter(AGENT.read_text(encoding="utf-8"))
    assert meta["name"] == meta["slug"] == coder.AGENT_SLUG == "strategy-coder"
    assert (meta["display_name"], meta["role"], meta["agent_type"], meta["reports_to"]) == ("Sam", "specialist", "service_strategy_coding", "hokma")
    assert (meta["model"], meta["hokma-tier"]) == (coder.DEFAULT_MODEL, coder.DEFAULT_TIER) == ("opus", "deep")
    tools = [t.strip() for t in meta["tools"].split(",")]
    assert tools == [*coder.ENGINE_TOOLS, *(coder.MCP_PREFIX + t for t in coder.TOOLS)]
    assert tools[:12] == ["Read", "Write", "Edit", "Glob", "Grep", "Bash", "Agent", "mcp__hokma__strategy_code_get", "mcp__hokma__strategy_code_list", "mcp__hokma__strategy_code_save", "mcp__hokma__bank_get", "mcp__hokma__factory_describe", "mcp__hokma__library_list"][:12]
    assert NEW_TOOLS <= set(tools) and not any(re.search(r"approve|review_approve|request_changes", t) for t in tools)
    assert not any(re.search(FORBIDDEN_TOOLS, t) for t in tools if t not in NEW_TOOLS) and "strategy_code_verdict" not in " ".join(tools)


def test_the_agent_names_its_skills_its_tools_and_what_it_cannot_do():
    text = AGENT.read_text(encoding="utf-8")
    for name in NAMES:
        assert f"`{name}`" in text, name
    for tool in coder.TOOLS:
        assert tool in text, tool
    flat = " ".join(text.split())
    for phrase in ("writes the tests first", "He writes the code of a strategy in", "place, change or cancel an order", "review, approve or judge his own code", "change the cases of the requirement",
                   "overwrite a version that was saved", "CodeReport", "rule_id, rule_version_id, registry_id"):
        assert phrase in flat, phrase


def test_the_skills_name_what_they_need_the_import_the_check_and_the_stop():
    contract = body("strategy-code-contract")
    for word in ("from user_api import", "strategy_code_save", "dry_run", "hold()", "ATR", "ctx.indicator", "frozen", "closed bar"):
        assert word in contract, word
    assert "Importing the package by its long name" in contract
    assert "fail" in body("code-test-first") and "red step" in body("code-test-first")
    assert "three attempts" in body("code-root-cause").lower() and "root cause" in body("code-root-cause").lower() and "Never edit a case of the requirement" in body("code-root-cause")
    assert "just now" in body("code-done-check") and "registry_id" in body("code-done-check")
    assert "one finding at a time" in body("review-reply") and "After three rejections" in body("review-reply")


def test_the_skills_of_sam_reach_no_other_agent_no_desk_role_and_not_the_main_agent():
    skills_reach_no_one_else(NAMES, AGENT)
    text = (ROOT / "kit" / "template" / "CLAUDE.md").read_text(encoding="utf-8")
    for name in NAMES:          # 1.2.2: the main agent names the three agents to launch them, never the skills
        assert not re.search(rf"\b{re.escape(name)}\b", text), name

"""Integrity of the prompts of the back office clerk (TA-41 style): the ten skills and the agent file exist in English, pass the repo rules, load in the
skill format, are referenced by the agent and reach no desk role (SPEC-018: AFS-001, 002, 003, 016, 017, 043, CTS-019, 025, ECT-078, SKL-014, 015)."""
import importlib.util
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hokma.db.seeds import parse_frontmatter
from hokma.desks.core import prompts as pr
from hokma.desks.core import skills as skill_format
from hokma.desks.core.signal import LINES

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "kit" / "template" / "service"
SKILLS = SERVICE / "skills"
AGENT = SERVICE / "agents" / "back-office-clerk.md"
NAMES = ("account-reconciliation", "break-trace", "roll-forward", "accrual-schedule", "account-report", "setup-practice", "weekly-digest",
         "performance-attribution", "statement-pdf", "spreadsheet-export")
IAN_NAMES = ("alpha-research", "backtest-audit", "big-move-study", "feature-engineering", "verify-before-claiming", "test-first-strategy",
             "backtest-debugging", "research-loop", "paper-search", "strategy-pivot")      # the skills of the strategy researcher (SPEC-020), listed in test_ian_integrity
AS_OF = datetime(2026, 1, 2, 15, 0, tzinfo=timezone.utc)
_spec = importlib.util.spec_from_file_location("check_repo_rules", ROOT / "scripts" / "check_repo_rules.py")
repo_rules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repo_rules)


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


@pytest.mark.parametrize("name", NAMES)
def test_each_skill_exists_in_the_skill_format_and_passes_the_repo_rules(name):
    path = SKILLS / name / "SKILL.md"
    assert path.is_file()
    skill = skill_format.load_skill(path)                       # six fields, no other, folder equals name
    assert skill.name == name and skill.avoid and skill.patterns and re.search(r"\buse\b", skill.description, re.I)
    assert repo_rules.check(ROOT, [rel(path)]) == []
    assert path.read_text(encoding="utf-8").isascii()           # English text: no accented letter, no inverted mark


def test_the_package_holds_exactly_these_skills_and_its_manifest_is_current():
    creator = ("source-to-strategy", "strategy-requirement", "strategy-code-contract", "code-test-first", "code-root-cause", "code-done-check", "review-reply", "strategy-code-review", "review-evidence")
    expected = sorted((*NAMES, *IAN_NAMES, *creator))
    assert sorted(p.parent.name for p in SKILLS.glob("*/SKILL.md")) == expected
    manifest = skill_format.read_manifest(SKILLS)
    assert sorted(manifest["skills"]) == expected and manifest["schema_version"] == skill_format.SCHEMA_VERSION
    assert skill_format.verify_manifest(SKILLS) == []


def test_the_agent_file_is_english_passes_the_rules_and_names_every_skill():
    assert repo_rules.check(ROOT, [rel(AGENT), *(rel(p) for p in SKILLS.rglob("*") if p.is_file())]) == []
    text = AGENT.read_text(encoding="utf-8")
    meta = parse_frontmatter(text)
    assert meta["name"] == meta["slug"] == "back-office-clerk" and meta["display_name"] == "Anna" and meta["reports_to"] == "hokma"
    for name in NAMES:
        assert f"`{name}`" in text, name
    tools = [t.strip() for t in meta["tools"].split(",")]
    assert not any(re.search(r"order|place|flatten|propose|Write|Edit|Bash", t) for t in tools)
    assert "change positions or orders" in text and "never writes an adjustment" in text


def test_no_desk_role_prompt_holds_a_skill_of_the_clerk_or_the_clerk_itself():
    comp = pr.PromptComposer()
    for role in pr.ROLES:
        for line in LINES:
            prompt = comp.compose(role=role, line=line, desk_id=1, as_of=AS_OF)
            for name in (*NAMES, "back-office-clerk", "Anna"):
                assert name not in prompt, (role, line, name)


def test_no_desk_agent_or_desk_rule_names_them_either():
    desk_files = [*(ROOT / "kit" / "template" / "desk").rglob("*"), *(ROOT / "kit" / "template" / "agents").glob("*.md"), ROOT / "kit" / "template" / "desks" / "CLAUDE.desk.md"]
    for path in (p for p in desk_files if p.is_file()):
        body = path.read_text(encoding="utf-8")
        assert not any(name in body for name in (*NAMES, "back-office-clerk")), path

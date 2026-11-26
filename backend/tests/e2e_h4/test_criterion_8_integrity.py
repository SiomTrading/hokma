"""Criterion 8 of SPEC-020: the file of Ian and every skill of this spec are in English, name no origin, are injected only into Ian, and the name of the backtest
engine appears only in the files that N-43 allows. The one known exception: the research analyst Iris has her own bundled skill that shares the name paper-search."""
import importlib.util
import re
from pathlib import Path

import pytest

from hokma.db.seeds import parse_frontmatter
from hokma.desks.core import prompts as prompt_rules
from hokma.desks.core import skills as skill_format
from hokma.desks.core.signal import LINES
from hokma.factory import researcher

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "kit" / "template" / "service"
SKILLS = SERVICE / "skills"
AGENT = SERVICE / "agents" / "strategy-researcher.md"
SKILLS_OF_THE_SPEC = ("verify-before-claiming", "test-first-strategy", "backtest-debugging", "research-loop", "paper-search", "strategy-pivot",
                      "alpha-research", "backtest-audit", "big-move-study", "feature-engineering")
IRIS = ("kit/template/agents/iris.md", "paper-search")
_found = importlib.util.spec_from_file_location("check_repo_rules", ROOT / "scripts" / "check_repo_rules.py")
rules = importlib.util.module_from_spec(_found)
_found.loader.exec_module(rules)


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


@pytest.mark.parametrize("name", SKILLS_OF_THE_SPEC)
def test_each_skill_of_the_spec_is_english_without_origin_and_in_the_format(name):
    path = SKILLS / name / "SKILL.md"
    skill = skill_format.load_skill(path)
    assert skill.name == name and skill.patterns and skill.avoid
    text = path.read_text(encoding="utf-8")
    assert text.isascii() and "\u2014" not in text
    assert rules.check(ROOT, [rel(path)]) == []                                              # no origin names, no Spanish, no engine name


def test_the_file_of_ian_is_english_names_no_origin_and_lists_exactly_the_tools_of_the_spec():
    text = AGENT.read_text(encoding="utf-8")
    assert text.isascii() and "\u2014" not in text and rules.check(ROOT, [rel(AGENT)]) == []
    meta = parse_frontmatter(text)
    assert [t.strip() for t in meta["tools"].split(",")] == [researcher.MCP_PREFIX + t for t in researcher.TOOLS]
    for name in SKILLS_OF_THE_SPEC:
        assert f"`{name}`" in text, name
    assert skill_format.verify_manifest(SKILLS) == []


def test_the_skills_and_the_agent_reach_no_other_agent_no_desk_role_and_no_clerk():
    templates = ROOT / "kit" / "template"
    for path in (p for p in templates.rglob("*") if p.is_file() and SKILLS not in p.parents and p != AGENT):
        if path.relative_to(templates).parts[0] == "docs":
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        for name in (*SKILLS_OF_THE_SPEC, researcher.AGENT_SLUG):
            if (rel(path), name) == IRIS:
                continue                                                                     # her own skill, bundled before this spec, shares the name
            assert name not in content, (rel(path), name)
    composer = prompt_rules.PromptComposer()
    from datetime import datetime, timezone

    for role in prompt_rules.ROLES:
        for line in LINES:
            prompt = composer.compose(role=role, line=line, desk_id=1, as_of=datetime(2026, 1, 2, 15, 0, tzinfo=timezone.utc))
            for name in (*SKILLS_OF_THE_SPEC, researcher.AGENT_SLUG, "Ian"):
                assert not re.search(rf"\b{re.escape(name)}\b", prompt), (role, line, name)
    assert prompt_rules.prompt_integrity_problems() == []


def test_the_engine_name_appears_only_in_the_files_that_are_allowed_to_hold_it():
    offenders = []
    for path in rules.tracked_files(ROOT):
        if path.startswith(rules.ENGINE_ALLOWED) or path.startswith(rules.SKIPPED) or Path(path).suffix in rules.BINARY_SUFFIXES or not (ROOT / path).is_file():
            continue
        try:
            text = (ROOT / path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if rules.engine_hits(text) or rules.engine_hits(path.replace("/", " ").replace(".", " ")):
            offenders.append(path)
    assert offenders == []
    assert "backend/hokma/backtest/_lib.py" in rules.ENGINE_ALLOWED and ".notices/" in rules.ENGINE_ALLOWED


def test_the_new_files_of_the_tools_and_the_proof_pass_the_rules_of_the_repository():
    new = ["backend/hokma/kit/backtest_tools.py", "backend/hokma/kit/factory_tools.py", "backend/tests/test_backtest_tools.py", "backend/tests/test_factory_tools.py",
           *sorted(rel(p) for p in (ROOT / "backend" / "tests" / "e2e_h4").glob("*.py")), "changelog.d/h4-backtest-tools.new.md"]
    assert rules.check(ROOT, new) == []

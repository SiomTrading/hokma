"""Skills of the strategy researcher (SPEC-020: ECT-006, ECT-036, ECT-069, CTS-018): the format, English, no origin, and the numbers they teach."""
import importlib.util
import re
from pathlib import Path

import pytest

from hokma.desks.core import skills as skill_format

ROOT = Path(__file__).resolve().parents[3]
SKILLS = ROOT / "kit" / "template" / "service" / "skills"
NAMES = ("backtest-audit", "alpha-research", "feature-engineering", "big-move-study")
_spec = importlib.util.spec_from_file_location("check_repo_rules", ROOT / "scripts" / "check_repo_rules.py")
repo_rules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repo_rules)


@pytest.mark.parametrize("name", NAMES)
def test_each_skill_loads_in_the_skill_format_in_english_without_origin(name):
    path = SKILLS / name / "SKILL.md"
    skill = skill_format.load_skill(path)                                      # six fields, no other, folder equals name
    assert skill.name == name and skill.patterns and skill.avoid and re.search(r"\buse\b", skill.description, re.I)
    text = path.read_text(encoding="utf-8")
    assert text.isascii() and "\u2014" not in text
    assert repo_rules.check(ROOT, [path.relative_to(ROOT).as_posix()]) == []


def test_the_audit_skill_teaches_the_bound_per_year_and_the_house_fill():
    text = (SKILLS / "backtest-audit" / "SKILL.md").read_text(encoding="utf-8")
    assert "2.15, 3.03 and 3.72" in text and "0.57 a year" in text and "square root of the years" in text
    assert "next open one tick against" in text and "stop first" in text
    assert text.count("| ") > 0 and all(phase in text for phase in ("Data integrity", "Specification", "Execution", "Statistics", "Overfitting", "Reporting"))
    assert "0.40" not in text and "0.60" not in text                            # no fixed PBO limit in the prompt


def test_the_research_skills_carry_their_traps_corrected():
    alpha = (SKILLS / "alpha-research" / "SKILL.md").read_text(encoding="utf-8")
    assert "square root of the number of periods" in alpha and "n / (1 + (n - 1) rho)" in alpha and "made up factor" in alpha
    features = (SKILLS / "feature-engineering" / "SKILL.md").read_text(encoding="utf-8")
    assert "per dollar traded" in features and "never interpolate" in features and "open of the next bar" in features
    study = (SKILLS / "big-move-study" / "SKILL.md").read_text(encoding="utf-8")
    assert "CURRENT_UNIVERSE_BACKFILL_SURVIVORSHIP_BIAS" in study and "multiplier" in study and "one tick against" in study and "never at the close of the event day" in study

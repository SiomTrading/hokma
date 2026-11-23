"""The four skills written for the options module (AFS-005, ECT-031, ECT-098, ECT-033): format, and every tool they name exists."""
import re
from pathlib import Path

import pytest

from hokma.desks.core.skills import load_skill
from hokma.quant import registry

ROOT = Path(__file__).resolve().parents[4]
SKILLS = ROOT / "kit" / "template" / "desk" / "skills"
EXPECTED = {"volatility-richness": "AFS-005", "greeks-management": "ECT-031", "options-structures": "ECT-098", "liquidity-risk": "ECT-033"}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_skill_loads_in_the_desk_format_and_names_only_registered_functions(name):
    path = SKILLS / name / "SKILL.md"
    skill = load_skill(path)
    text = path.read_text(encoding="utf-8")
    assert skill.name == name and skill.patterns and skill.avoid and skill.description and skill.identity
    assert "—" not in text
    registry.load_all()
    registered = {s.name for s in registry.list("options")}
    named = set(re.findall(r"options\.[a-z_0-9]+", text))
    assert named, "a skill names the functions it uses"
    assert named <= registered, sorted(named - registered)
    assert any(EXPECTED[name] in s.piece_ids for s in registry.list("options") if s.name in named), "the skill calls the function of its own row"


def test_the_skills_never_write_a_contract_multiplier():
    for name in EXPECTED:
        text = (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
        for line in text.splitlines():
            if "100" in line and "multiplier" in line.lower():
                assert any(w in line.lower() for w in ("never", "not", "from memory", "no number")), (name, line)

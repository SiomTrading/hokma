"""Sam in 1.2.6 (task 3427): the folder of a strategy is service/strategies/STR-{NNN}-{slug}/, the hook is described as it is, his sub-agents exist like Joel's,
and every skill he or they cite has its file in the kit (one set, no skill without a file)."""
import json
import re
from pathlib import Path

from hokma.agents.files import frontmatter_value

ROOT = Path(__file__).resolve().parents[2]
AGENTS = ROOT / "kit" / "template" / "agents"
SAM = AGENTS / "strategy-coder.md"
SUBS = ("sam-dev-python", "sam-dev-frontend", "sam-mcp-builder")


def skill_files() -> set[str]:
    found: set[str] = set()
    for folder in ("service", "desk"):
        found |= {p.parent.name for p in (ROOT / "kit" / "template" / folder / "skills").glob("*/SKILL.md")}
    return found


def known_skill_ids() -> set[str]:
    return {s["id"] for s in json.loads((ROOT / "kit" / "skills-manifest.json").read_text(encoding="utf-8"))["skills"]}


def test_sam_uses_the_folder_of_the_strategy_and_never_a_projects_folder():
    text = SAM.read_text(encoding="utf-8")
    assert "projects/" not in text
    assert "service/strategies/STR-{NNN}-{slug}/adr/" in text and "service/strategies/STR-{NNN}-{slug}/ (the code goes in versions/v{N}/)" in text
    assert "mcp__hokma__bank_list" in (frontmatter_value(text, "tools") or "")


def test_the_hook_sentence_says_what_the_gate_does_and_nothing_else():
    text = SAM.read_text(encoding="utf-8")
    assert "outside `versions/v{N}/` of a strategy folder, or in a folder that has no `spec.md`" in text
    assert "are free: the hook never blocks them" in text and "and any other file name" not in text


def test_sam_has_three_sub_agents_in_the_kit_like_joel_with_the_same_tools():
    assert "coordinates_with" not in SAM.read_text(encoding="utf-8").split("---")[1]            # the integrity tests keep the other agents out of his frontmatter
    declared = frontmatter_value(SAM.read_text(encoding="utf-8"), "subagents")
    assert declared == "[" + ", ".join(SUBS) + "]"
    for sub, joel in zip(SUBS, ("joel-dev-python", "joel-dev-frontend", "joel-mcp-builder")):
        mine, theirs = (AGENTS / f"{sub}.md").read_text(encoding="utf-8"), (AGENTS / f"{joel}.md").read_text(encoding="utf-8")
        assert frontmatter_value(mine, "tools") == frontmatter_value(theirs, "tools") and "Write" in frontmatter_value(mine, "tools")
        assert frontmatter_value(mine, "name") == sub == frontmatter_value(mine, "slug") and frontmatter_value(mine, "role") == "sub-agent"
        assert frontmatter_value(mine, "reports_to") == "strategy-coder" and "projects/" not in mine and "Joel" not in mine and "Vera" not in mine
        assert "service/strategies/STR-{NNN}" in mine and "strategy.py" in mine


def test_every_skill_sam_and_his_sub_agents_cite_has_its_file_and_there_is_one_set():
    have, declared = skill_files(), known_skill_ids()
    for name in ("strategy-coder", *SUBS):
        text = (AGENTS / f"{name}.md").read_text(encoding="utf-8")
        cited = {m for m in re.findall(r"`([a-z][a-z0-9-]+)`", text) if m in declared}
        assert cited and cited <= have, (name, sorted(cited - have))
    sam = SAM.read_text(encoding="utf-8")
    assert "`code-test-first`" in sam and "`code-root-cause`" in sam
    assert not re.search(r"`(test-first|root-cause-debugging|focused-repair)`", sam)


def test_the_example_registry_id_of_the_tools_is_the_one_of_a_numbered_strategy():
    text = (ROOT / "backend" / "hokma" / "kit" / "rule_tools.py").read_text(encoding="utf-8")
    assert "u_str001_v1" in text and "u_inside_bar" not in text

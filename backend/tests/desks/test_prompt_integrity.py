"""Integrity of the prompts themselves (TA-41): rule files exist, reach every role, recipes reach their lines, repo rules pass."""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hokma.desks.core import prompts as pr
from hokma.desks.core.signal import LINES, contains_execution_terms

ROOT = Path(__file__).resolve().parents[3]
DESK_KIT = ROOT / "kit" / "template" / "desk"
AS_OF = datetime(2026, 1, 2, 15, 0, tzinfo=timezone.utc)
_spec = importlib.util.spec_from_file_location("check_repo_rules", ROOT / "scripts" / "check_repo_rules.py")
repo_rules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repo_rules)


def kit_files():
    return sorted(p.relative_to(ROOT).as_posix() for p in DESK_KIT.rglob("*") if p.is_file())


def test_kit_has_no_integrity_problems_TA_41():
    assert pr.prompt_integrity_problems() == []


@pytest.mark.parametrize("key", pr.COMMON_RULES)
def test_every_rule_file_exists_and_is_injected_in_each_role_TA_41(key):
    assert (DESK_KIT / "rules" / f"{key}.md").is_file()
    comp = pr.PromptComposer()
    for role in pr.ROLES:
        for line in LINES:
            assert f"## Rule: {key}\n" in comp.compose(role=role, line=line, desk_id=1, as_of=AS_OF)


def test_validations_file_exists_and_every_role_gets_rules_from_it_ED_038():
    assert (DESK_KIT / "rules" / "validations.yaml").is_file()
    data = pr.load_validations()
    assert {"id", "rule", "applies_to"} <= set(data["validations"][0]) and data["collaboration"]
    for role in pr.ROLES:
        assert len(pr.validations_for(role)) > len(data["collaboration"]), role
        assert "## Rule: validations" in pr.PromptComposer().compose(role=role, line="LP", desk_id=1, as_of=AS_OF)
    roles_named = {r for v in data["validations"] for r in v["applies_to"]}
    assert roles_named <= set(pr.ROLES)


def test_kit_files_pass_the_repo_rules_TA_41():
    files = kit_files()
    assert any(f.endswith("rules/citations.md") for f in files) and any("/recipes/" in f for f in files)
    assert repo_rules.check(ROOT, files) == []


def test_integrity_check_reports_a_missing_rule_file_TA_41(tmp_path):
    problems = pr.prompt_integrity_problems(root=tmp_path)
    assert any("citations.md" in p for p in problems)


def test_integrity_check_reports_a_recipe_for_an_unknown_line_TA_41(tmp_path):
    for sub in ("rules", "recipes"):
        (tmp_path / "kit" / "template" / "desk" / sub).mkdir(parents=True)
    for p in (DESK_KIT / "rules").iterdir():
        (tmp_path / "kit" / "template" / "desk" / "rules" / p.name).write_text(p.read_text())
    (tmp_path / "kit" / "template" / "desk" / "recipes" / "bad.md").write_text("---\nname: bad\nlines: [XX]\nsummary: s\n---\nbody\n")
    assert any("recipe" in p for p in pr.prompt_integrity_problems(root=tmp_path))


def test_no_role_but_the_execution_roles_holds_sizing_or_exit_words_TA_41():
    comp = pr.PromptComposer()
    for role in pr.ROLES:
        text = comp.compose(role=role, line="SWC", desk_id=1, as_of=AS_OF)
        found = contains_execution_terms(text)
        if role not in pr.EXECUTION_ROLES:
            assert found is None, (role, found)
    assert contains_execution_terms(comp.compose(role="trader", line="SWC", desk_id=1, as_of=AS_OF))
    for path in (DESK_KIT / "rules").iterdir():
        assert contains_execution_terms(path.read_text()) is None, path.name
    for recipe in pr.load_recipes().values():
        assert contains_execution_terms(recipe.body) is None, recipe.name

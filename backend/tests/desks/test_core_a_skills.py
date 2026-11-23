"""Skill format and manifest (ED-036, OA-16, OA-17)."""
import json
from pathlib import Path

import pytest

from hokma.desks.core import skills
from hokma.desks.core.skills import SkillError, parse_skill

GOOD = """---
name: earnings-reaction
version: 1.0.0
description: >
  Use when the owner asks how a stock reacted to results. Trigger phrases: "how did X react", "post earnings drift".
identity: An analyst who reads results against expectations.
patterns:
  - Compare reported figures with the earlier consensus.
avoid:
  - Using figures published after the cut-off time.
---
1. Fetch the results.
2. Compare them.
"""


def without(field: str) -> str:
    out, skip = [], False
    for line in GOOD.splitlines():
        if line.startswith(field + ":"):
            skip = True
            continue
        if skip and line.startswith((" ", "\t")):
            continue
        skip = False
        out.append(line)
    return "\n".join(out) + "\n"


def test_valid_skill_loads_ED_036():
    s = parse_skill(GOOD)
    assert s.name == "earnings-reaction" and s.version == "1.0.0"
    assert s.patterns and s.avoid and s.body.startswith("1. Fetch")


@pytest.mark.parametrize("field", ["version", "avoid", "patterns", "identity", "description", "name"])
def test_missing_field_fails_ED_036(field):
    with pytest.raises(SkillError, match=field):
        parse_skill(without(field))


def test_bad_values_fail_OA_16():
    for old, new, word in [("name: earnings-reaction", "name: Earnings Reaction", "name"),
                           ("version: 1.0.0", "version: one", "version"),
                           ("Use when the owner asks", "The owner asks", "description")]:
        with pytest.raises(SkillError, match=word):
            parse_skill(GOOD.replace(old, new))
    with pytest.raises(SkillError, match="unknown"):
        parse_skill(GOOD.replace("identity:", "extra: 1\nidentity:"))
    with pytest.raises(SkillError, match="frontmatter"):
        parse_skill("no frontmatter here")


def package(tmp_path: Path, version="1.0.0") -> Path:
    folder = tmp_path / "skills"
    (folder / "earnings-reaction").mkdir(parents=True)
    (folder / "earnings-reaction" / "SKILL.md").write_text(GOOD.replace("1.0.0", version, 1))
    return folder


def test_manifest_records_versions_OA_17(tmp_path):
    folder = package(tmp_path)
    manifest = skills.write_manifest(folder, "2.3.0")
    assert manifest["package_version"] == "2.3.0" and manifest["schema_version"] == 1
    assert manifest["skills"]["earnings-reaction"]["version"] == "1.0.0"
    assert json.loads((folder / skills.MANIFEST_NAME).read_text()) == manifest
    assert skills.verify_manifest(folder) == []


def test_manifest_detects_drift_OA_17(tmp_path):
    folder = package(tmp_path)
    skills.write_manifest(folder, "1.0.0")
    path = folder / "earnings-reaction" / "SKILL.md"
    path.write_text(GOOD.replace("Compare it", "Compare them twice") .replace("2. Compare them.", "2. Compare them twice."))
    assert "without a version bump" in skills.verify_manifest(folder)[0]
    path.write_text(GOOD.replace("version: 1.0.0", "version: 1.1.0").replace("2. Compare them.", "2. Compare."))
    assert skills.verify_manifest(folder) == ["earnings-reaction: content changed"]
    (folder / "extra").mkdir()
    (folder / "extra" / "SKILL.md").write_text(GOOD.replace("earnings-reaction", "extra"))
    assert any("not in the manifest" in p for p in skills.verify_manifest(folder))


def test_folder_must_match_name_and_names_are_unique(tmp_path):
    folder = package(tmp_path)
    (folder / "other").mkdir()
    (folder / "other" / "SKILL.md").write_text(GOOD)
    with pytest.raises(SkillError, match="folder name"):
        skills.load_skills(folder)


def test_skill_format_doc_lists_every_required_field_ED_036():
    doc = (Path(__file__).resolve().parents[3] / "kit" / "template" / "desk" / "skill-format.md").read_text()
    for field in skills.REQUIRED:
        assert f"| {field} |" in doc
    assert "skills-manifest.json" in doc

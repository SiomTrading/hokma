"""ED-039: the skills of a desk are exported to the format of the active engine and importing them gives the same."""
import pytest

from hokma.desks.core import export

E_ACUTE = chr(0xE9)
SKILLS = [
    export.Skill("risk-check", 'Checks the limits of a desk: "size", position and loss; also accents ' + E_ACUTE,
                 "# Risk check\n\n1. Read the limits.\n2. Compare the order.\n\nNever skip step 2.",
                 {"refs/limits.md": "Max size: 5", "refs/notes.txt": "plain"}),
    export.Skill("daily-note", "Writes the note of the day: colon: inside, newline\nand all", "Body only."),
]


@pytest.mark.parametrize("kind", sorted(export.LAYOUTS))
def test_export_then_import_gives_the_same_skills_ED_039(tmp_path, kind):
    written = export.export_skills(SKILLS, kind, tmp_path)
    assert [p.name for p in written] == ["SKILL.md", "SKILL.md"] and all(p.is_file() for p in written)
    assert written[0].parent.parent == export.skills_dir(kind, tmp_path)
    assert export.import_skills(kind, tmp_path) == sorted(SKILLS, key=lambda s: s.name)


def test_each_engine_gets_its_own_folder_ED_039(tmp_path):
    for kind in export.LAYOUTS:
        export.export_skills(SKILLS[:1], kind, tmp_path)
    folders = {k: export.skills_dir(k, tmp_path) for k in export.LAYOUTS}
    assert len(set(folders.values())) == 3 and all((f / "risk-check" / "SKILL.md").is_file() for f in folders.values())


def test_skill_md_has_a_header_and_the_instructions_ED_039():
    text = export.render_skill_md(SKILLS[1])
    assert text.startswith("---\nname: daily-note\ndescription: ") and text.endswith("Body only.\n")
    assert export.parse_skill_md(text) == ("daily-note", SKILLS[1].description, "Body only.")


def test_exporting_twice_overwrites_and_never_deletes_ED_039(tmp_path):
    export.export_skills(SKILLS, "claude_code", tmp_path)
    keep = export.skills_dir("claude_code", tmp_path) / "risk-check" / "extra-by-owner.md"
    keep.write_text("mine", encoding="utf-8")
    changed = export.Skill("risk-check", "New description", "New body")
    export.export_skills([changed], "claude_code", tmp_path)
    assert keep.read_text(encoding="utf-8") == "mine"
    assert [s.body for s in export.import_skills("claude_code", tmp_path) if s.name == "risk-check"] == ["New body"]


def test_the_direct_api_has_no_skills_folder_ED_039(tmp_path):
    with pytest.raises(export.ExportError, match="no skills folder"):
        export.export_skills(SKILLS, "api", tmp_path)
    assert export.export_for_active_engine(SKILLS[:1], tmp_path, kind="codex")[0] == "codex"


@pytest.mark.parametrize("name", ["Risk Check", "risk_check", "-risk", "risk-", "", "a" * 65, "../x", "risk--check"])
def test_a_bad_skill_name_is_refused_ED_039(name):
    with pytest.raises(export.ExportError):
        export.Skill(name, "d", "b")


@pytest.mark.parametrize("rel", ["../x.md", "/abs.md", "C:/x.md", "a//b.md", "SKILL.md", "sub/SKILL.md", "", "./x.md"])
def test_a_supporting_file_cannot_leave_the_skill_ED_039(rel):
    with pytest.raises(export.ExportError):
        export.Skill("ok", "d", "b", {rel: "x"})


def test_duplicate_names_and_empty_descriptions_are_refused_ED_039(tmp_path):
    with pytest.raises(export.ExportError, match="same name"):
        export.export_skills([SKILLS[1], SKILLS[1]], "claude_code", tmp_path)
    with pytest.raises(export.ExportError):
        export.Skill("ok", "  ", "b")
    with pytest.raises(export.ExportError):
        export.Skill("ok", "d" * 1025, "b")


@pytest.mark.parametrize("text", ["no header", "---\nname: a\n", "---\nname: a\n---\nbody", "---\nname: a\ndescription: \"unclosed\n---\nb"])
def test_a_broken_skill_md_is_an_error_ED_039(text):
    with pytest.raises(export.ExportError):
        export.parse_skill_md(text)


def test_a_folder_with_another_name_than_its_skill_is_an_error_ED_039(tmp_path):
    export.export_skills(SKILLS[1:], "claude_code", tmp_path)
    base = export.skills_dir("claude_code", tmp_path)
    (base / "daily-note").rename(base / "other-name")
    with pytest.raises(export.ExportError, match="holds the skill"):
        export.import_skills("claude_code", tmp_path)
    assert export.import_skills("codex", tmp_path) == []

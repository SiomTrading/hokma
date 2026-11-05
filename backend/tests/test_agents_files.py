"""Agent files: frontmatter edits, safe paths and the on/off layout."""
import os
import sys

import pytest

from hokma.agents import files as fl

TEXT = "---\nname: x\nmodel: sonnet\nhokma-tier: balanced\ndescription: >\n  hello\n  there\n---\n\n# Body\nline\n"


def test_set_model_rewrites_only_the_model_lines():
    out = fl.set_frontmatter_value(fl.set_frontmatter_value(TEXT, "model", "opus"), "hokma-tier", "deep")
    assert out == TEXT.replace("model: sonnet", "model: opus").replace("balanced", "deep")


def test_missing_key_is_added_before_the_closing_fence():
    out = fl.set_frontmatter_value("---\nname: x\n---\nbody\n", "model", "haiku")
    assert out == "---\nname: x\nmodel: haiku\n---\nbody\n"
    assert fl.frontmatter_value(out, "model") == "haiku"


def test_text_without_frontmatter_is_refused():
    with pytest.raises(fl.AgentFileError) as err:
        fl.set_frontmatter_value("# no frontmatter", "model", "haiku")
    assert err.value.code == "agent_file_invalid"


def test_slugify_and_clean_name():
    assert fl.slugify("Analista de Gr\u00e1ficos") == "analista-de-graficos"
    assert fl.slugify("  --\u00d1and\u00fa 2!  ") == "nandu-2"
    assert fl.clean_name("Mi: agente # {x}\n[y]") == "Mi agente x y"


def test_custom_agent_text_parses_back():
    text = fl.build_custom_agent("mi-agente", "Mi agente", "Hace: cosas # raras\ny mas", "haiku", "Haz esto.\n---\nlo otro")
    from hokma.db.seeds import parse_frontmatter
    meta = parse_frontmatter(text)
    assert meta["slug"] == "mi-agente" and meta["model"] == "haiku" and meta["hokma-tier"] == "fast"
    assert meta["description"] == "Hace: cosas # raras y mas"
    assert text.endswith("Haz esto.\n---\nlo otro\n")


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_a_symlinked_agents_folder_is_never_followed(tmp_path):
    work, outside = tmp_path / "work", tmp_path / "outside"
    work.mkdir()
    outside.mkdir()
    (work / ".claude").mkdir()
    os.symlink(outside, work / ".claude" / "agents")
    (work / "agents").mkdir()
    (work / "agents" / "a.md").write_text(TEXT)
    with pytest.raises(fl.AgentFileError) as err:
        fl.AgentFiles(work).apply("a", "agents/a.md", active=True, model="haiku")
    assert err.value.code == "agent_file_unsafe"
    assert list(outside.iterdir()) == []


def test_source_path_must_be_inside_agents_folder(tmp_path):
    with pytest.raises(fl.AgentFileError):
        fl.AgentFiles(tmp_path).source_rel("a", "../etc/passwd")
    with pytest.raises(fl.AgentFileError):
        fl.AgentFiles(tmp_path).source_rel("a", "agents/sub/a.md")

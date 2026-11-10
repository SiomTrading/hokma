"""The two kit files every fund loads promise only tools that exist, and an update delivers them (task 3125)."""
import re
import shutil
from pathlib import Path

import pytest

from hokma.kit.sync import sync_kit

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "kit" / "template"
TOOL_CALL = re.compile(r"\b(robot)_[a-z_]+\(|hokma_data")
HOKMA_FILES = ("HOKMA.md", "agents/hokma.md")


def real_tools() -> set[str]:
    from hokma.kit import hokma_tools

    return set(hokma_tools.TOOLS) | {"trash_move"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "work").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOKMA_HOME", str(tmp_path / "work"))
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HOKMA_LANG", raising=False)
    return tmp_path


@pytest.mark.parametrize("rel", sorted(str(p.relative_to(TEMPLATE)) for p in TEMPLATE.rglob("*.md")))
def test_the_template_promises_only_tools_that_exist(rel):
    """CA-26: no file of the template names a Hokma tool that `tools/list` does not return."""
    text = (TEMPLATE / rel).read_text(encoding="utf-8")
    assert not TOOL_CALL.search(text)
    named = set(re.findall(r"mcp__hokma__(\w+)", text))
    assert named <= real_tools(), named - real_tools()


def test_hokma_tells_the_agent_where_the_agent_list_really_is():
    agent = (TEMPLATE / "agents" / "hokma.md").read_text(encoding="utf-8")
    manual = (TEMPLATE / "HOKMA.md").read_text(encoding="utf-8")
    assert "agents_list" in agent and "agents_list" in manual
    assert agent.splitlines()[0] == "---" and re.search(r"^tools: .*mcp__hokma__trash_move$", agent, re.M)


def previous_kit(tmp_path: Path) -> Path:
    """The kit as 0.0.7 shipped it for these two files: they promised the missing tools."""
    old = tmp_path / "old-template"
    shutil.copytree(TEMPLATE, old)
    for rel in HOKMA_FILES:
        path = old / rel
        path.write_text(path.read_text(encoding="utf-8") + "\nOLD-KIT-MARKER\n", encoding="utf-8")
    return old


def test_update_replaces_the_files_the_owner_never_edited(env):
    sync_kit(previous_kit(env), version="0.0.7")
    assert "OLD-KIT-MARKER" in (env / "work" / "agents" / "hokma.md").read_text(encoding="utf-8")
    report = sync_kit(TEMPLATE, version="0.0.8")
    assert set(HOKMA_FILES) <= set(report.updated) and not report.conflicts
    for rel in HOKMA_FILES:
        assert "OLD-KIT-MARKER" not in (env / "work" / rel).read_text(encoding="utf-8")


def test_update_keeps_an_edited_file_and_writes_the_new_one_beside_it(env):
    sync_kit(previous_kit(env), version="0.0.7")
    mine = env / "work" / "HOKMA.md"
    edited = mine.read_text(encoding="utf-8") + "My own rule.\n"
    mine.write_text(edited, encoding="utf-8")
    report = sync_kit(TEMPLATE, version="0.0.8")
    assert report.conflicts == ["HOKMA.md"] and mine.read_text(encoding="utf-8") == edited
    assert "OLD-KIT-MARKER" not in (env / "work" / ("HOKMA.md" + ".hokma-new")).read_text(encoding="utf-8")

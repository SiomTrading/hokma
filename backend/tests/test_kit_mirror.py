"""Skills and agents of the working folder reach the user source Claude Code reads (task 3168)."""
import json

import pytest
from engine_support import env  # noqa: F401

from hokma.kit import mirror

pytestmark = pytest.mark.skipif(__import__("os").name != "posix", reason="links")

AGENT = "---\nname: helper\ndescription: Helps\ntools: Read\nmodel: haiku\nhooks:\n  PreToolUse: []\npermissionMode: bypassPermissions\nmcpServers: x\n---\nBody of helper.\n"
SKILL = "---\nname: zorro\ndescription: >\n  Use for the fox.\nallowed-tools: Bash\nhooks:\n  Stop: []\n---\nSay KIWI.\n"


@pytest.fixture
def dirs(tmp_path):
    work, config = tmp_path / "work", tmp_path / "config"
    (work / ".claude" / "agents").mkdir(parents=True)
    (work / ".claude" / "skills" / "zorro").mkdir(parents=True)
    return work, config


def test_active_agents_and_skills_are_copied_without_powers(dirs):
    work, config = dirs
    (work / ".claude" / "agents" / "helper.md").write_text(AGENT)
    (work / ".claude" / "skills" / "zorro" / "SKILL.md").write_text(SKILL)
    (work / ".claude" / "skills" / "zorro" / "notes.md").write_text("extra")
    mirror.sync(work, config)
    agent = (config / "agents" / "helper.md").read_text()
    assert "Body of helper." in agent and "tools: Read" in agent and "model: haiku" in agent
    assert "hooks" not in agent and "permissionMode" not in agent and "mcpServers" not in agent
    skill = (config / "skills" / "zorro" / "SKILL.md").read_text()
    assert "Use for the fox." in skill and "Say KIWI." in skill and "hooks" not in skill and "allowed-tools" not in skill
    assert (config / "skills" / "zorro" / "notes.md").read_text() == "extra"


def test_agents_turned_off_or_deleted_leave_the_user_source(dirs):
    work, config = dirs
    (work / ".claude" / "agents" / "helper.md").write_text(AGENT)
    (work / ".claude" / "skills" / "zorro" / "SKILL.md").write_text(SKILL)
    mirror.sync(work, config)
    (work / ".claude" / "agents" / "helper.md").unlink()
    (work / ".claude" / "skills" / "zorro" / "SKILL.md").unlink()
    mirror.sync(work, config)
    assert not (config / "agents" / "helper.md").exists() and not (config / "skills" / "zorro").exists()
    assert json.loads((config / mirror.MANIFEST).read_text()) == {"agents": [], "skills": []}


def test_what_the_student_put_in_the_user_source_is_never_touched(dirs):
    work, config = dirs
    (config / "agents").mkdir(parents=True)
    (config / "agents" / "helper.md").write_text("MINE")
    (work / ".claude" / "agents" / "helper.md").write_text(AGENT)
    mirror.sync(work, config)
    assert (config / "agents" / "helper.md").read_text() == "MINE"
    (work / ".claude" / "agents" / "helper.md").unlink()
    mirror.sync(work, config)
    assert (config / "agents" / "helper.md").read_text() == "MINE"


def test_links_hard_links_and_files_without_frontmatter_are_ignored(dirs, tmp_path):
    import os
    work, config = dirs
    secret = tmp_path / "secret.md"
    secret.write_text(AGENT.replace("Body of helper.", "TOP-SECRET"))
    (work / ".claude" / "agents" / "linked.md").symlink_to(secret)
    os.link(secret, work / ".claude" / "agents" / "hard.md")
    (work / ".claude" / "agents" / "plain.md").write_text("no frontmatter")
    (work / ".claude" / "skills" / "zorro" / "SKILL.md").write_text(SKILL)
    (work / ".claude" / "skills" / "zorro" / "leak.md").symlink_to(secret)
    (work / ".claude" / "skills" / "evil").symlink_to(tmp_path)
    mirror.sync(work, config)
    assert not (config / "agents").exists() or list((config / "agents").iterdir()) == []
    assert not (config / "skills" / "evil").exists() and not (config / "skills" / "zorro" / "leak.md").exists()
    assert "TOP-SECRET" not in "".join(p.read_text() for p in config.rglob("*") if p.is_file())


def test_a_linked_claude_folder_is_not_followed(dirs, tmp_path):
    work, config = dirs
    other = tmp_path / "other" / "agents"
    other.mkdir(parents=True)
    (other / "helper.md").write_text(AGENT)
    (work / ".claude" / "agents").rmdir()
    (work / ".claude" / "agents").symlink_to(other)
    mirror.sync(work, config)
    assert not (config / "agents" / "helper.md").exists()


def test_every_real_launch_runs_the_mirror(env):
    from hokma.kit.launch import prepare_claude_launch
    from hokma.core.config import claude_config_dir
    agents = env.home / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "helper.md").write_text(AGENT)
    prepare_claude_launch()
    assert (claude_config_dir() / "agents" / "helper.md").is_file()


def test_a_hard_linked_skill_file_is_not_copied(dirs, tmp_path):
    import os
    work, config = dirs
    secret = tmp_path / "secret.md"
    secret.write_text(SKILL.replace("Say KIWI.", "TOP-SECRET"))
    os.link(secret, work / ".claude" / "skills" / "zorro" / "SKILL.md")
    mirror.sync(work, config)
    assert not (config / "skills" / "zorro").exists()


def test_a_fifo_in_the_folders_does_not_hang_the_mirror(dirs):
    import os, signal
    work, config = dirs
    os.mkfifo(work / ".claude" / "agents" / "pipe.md")
    os.mkfifo(work / ".claude" / "skills" / "zorro" / "SKILL.md")
    signal.alarm(10)
    try:
        mirror.sync(work, config)
        from hokma.kit import safefs
        try:
            safefs.read_regular(work, ".claude/agents/pipe.md")
        except safefs.UnsafePath:
            pass
    finally:
        signal.alarm(0)
    assert not (config / "agents" / "pipe.md").exists()


@pytest.mark.parametrize("sneaky", ['"hooks": {}', "hooks : {}", "? hooks", "'permissionMode': bypassPermissions", "<<: *x"])
def test_a_key_the_filter_cannot_recognise_drops_the_whole_file(dirs, sneaky):
    work, config = dirs
    (work / ".claude" / "agents" / "helper.md").write_text(f"---\nname: helper\ndescription: Helps\n{sneaky}\n---\nBody\n")
    mirror.sync(work, config)
    assert not (config / "agents" / "helper.md").exists()


def test_block_lists_and_folded_values_still_cross(dirs):
    work, config = dirs
    (work / ".claude" / "agents" / "helper.md").write_text(
        "---\nname: helper\ndescription: >\n  Long\n  text\ntools:\n- Read\n- Grep\n---\nBody\n")
    mirror.sync(work, config)
    out = (config / "agents" / "helper.md").read_text()
    assert "  text" in out and "- Grep" in out


def test_a_huge_file_is_not_read_whole(dirs):
    work, config = dirs
    (work / ".claude" / "agents" / "helper.md").write_text(AGENT + "x" * (mirror.MAX_FILE + 1))
    mirror.sync(work, config)
    assert not (config / "agents" / "helper.md").exists()


@pytest.mark.parametrize("sep", ["\r", "\x85", "\u2028", "\u2029", "\x0c"])
def test_odd_line_breaks_in_the_frontmatter_drop_the_file(dirs, sep):
    work, config = dirs
    (work / ".claude" / "agents" / "helper.md").write_text(
        f"---\nname: helper\ndescription: Helps{sep}hooks: {{}}\n---\nBody\n", encoding="utf-8", newline="")
    mirror.sync(work, config)
    assert not (config / "agents" / "helper.md").exists()


def test_the_name_is_always_the_files_own_so_nothing_is_shadowed(dirs):
    work, config = dirs
    (work / ".claude" / "agents" / "helper.md").write_text("---\nname: general-purpose\ndescription: Helps\n---\nBody\n")
    (work / ".claude" / "skills" / "zorro" / "SKILL.md").write_text("---\nname: update-config\ndescription: x\n---\nBody\n")
    mirror.sync(work, config)
    assert "name: helper\n" in (config / "agents" / "helper.md").read_text()
    assert "general-purpose" not in (config / "agents" / "helper.md").read_text()
    skill = (config / "skills" / "zorro" / "SKILL.md").read_text()
    assert "name: zorro\n" in skill and "update-config" not in skill

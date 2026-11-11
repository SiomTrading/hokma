"""Agents and skills made from the chat (task 3164): the MCP tools use the Create agent logic, an agent written into
the folder shows up in the panel, and no lock stops writing agents, skills or the project instructions file."""
import json
import ntpath
import sys

import pytest

from agents_support import HEADERS, client, db, make_client, work  # noqa: F401
from hokma.kit import guard, hardblock, hokma_tools
from hokma.kit.hokma_tools import Caller
from hokma.kit.settings import protected_paths


def call(name, args):
    text, is_error = hokma_tools.call(name, args, Caller({"HOKMA_AGENT": "hokma"}))
    return (json.loads(text) if not is_error else text), is_error


def items(client):
    data = client.get("/api/agents").json()
    return {i["slug"]: i for i in data["agents"] + ([data["orchestrator"]] if data["orchestrator"] else [])}


NEW = {"name": "Analista Macro", "description": "Lee el calendario macro", "model": "sonnet",
       "instructions": "Resume los eventos de la semana."}


def test_the_tools_are_listed_with_a_schema():
    listed = {d["name"]: d for d in hokma_tools.definitions()}
    assert {"agent_save", "skill_save"} <= set(listed)
    assert listed["agent_save"]["inputSchema"]["required"] == ["name"]


def test_agent_save_creates_an_agent_that_shows_in_the_panel_and_is_on(client, work):
    out, err = call("agent_save", {**NEW, "tools": "Read, Grep"})
    assert not err and out["created"] and out["agent"]["active"]
    shown = items(client)["analista-macro"]
    assert shown["active"] and shown["is_custom"] and shown["model"] == "sonnet"
    assert (work.home / ".claude" / "agents" / "analista-macro.md").is_file()
    assert "tools: Read, Grep" in (work.home / "agents" / "analista-macro.md").read_text()


def test_agent_save_off_when_asked(client, work):
    out, err = call("agent_save", {**NEW, "active": False})
    assert not err and not out["agent"]["active"]
    assert not items(client)["analista-macro"]["active"]
    assert "analista-macro" in work.parked()


def test_agent_save_edits_an_agent_the_owner_made(client, work):
    call("agent_save", NEW)
    out, err = call("agent_save", {"name": "Analista Macro", "model": "opus", "instructions": "Nuevo texto.",
                                   "active": False})
    assert not err and not out["created"]
    shown = items(client)["analista-macro"]
    assert shown["model"] == "opus" and not shown["active"] and shown["description"] == NEW["description"]
    text = (work.home / "agents" / "analista-macro.md").read_text()
    assert "Nuevo texto." in text and "Resume los eventos" not in text


def test_agent_save_refuses_to_rewrite_a_kit_agent(client, work):
    out, err = call("agent_save", {"name": "joel", "instructions": "x"})
    assert err and "created" in out


def test_agent_save_needs_the_fields_of_a_new_agent(work, client):
    out, err = call("agent_save", {"name": "Solo Nombre"})
    assert err and "description" in out


def test_edit_route_of_the_panel(client, work):
    call("agent_save", NEW)
    r = client.put("/api/agents/analista-macro", json={"instructions": "Otra cosa."})
    assert r.status_code == 200 and "Otra cosa." in (work.home / "agents" / "analista-macro.md").read_text()
    assert client.put("/api/agents/joel", json={"instructions": "x"}).status_code == 409


def test_an_agent_file_written_in_the_folder_appears_without_restarting(client, work):
    assert "ana-fiscal" not in items(client)
    (work.home / "agents" / "ana-fiscal.md").write_text(
        "---\nname: ana-fiscal\ndescription: Impuestos\nmodel: haiku\ntools: Read\n---\n\nAyuda con impuestos.\n")
    shown = items(client)["ana-fiscal"]
    assert shown["active"] and shown["is_custom"] and shown["model"] == "haiku"
    assert (work.home / ".claude" / "agents" / "ana-fiscal.md").is_file()


def test_skill_save_writes_skill_md_in_the_working_folder(work, client):
    out, err = call("skill_save", {"name": "Cierre Mensual", "description": "Cuando cierre el mes",
                                   "instructions": "Pasos del cierre."})
    assert not err and out["path"] == ".claude/skills/cierre-mensual/SKILL.md" and not out["updated"]
    text = (work.home / ".claude" / "skills" / "cierre-mensual" / "SKILL.md").read_text()
    assert text.startswith("---\nname: cierre-mensual\n") and "Pasos del cierre." in text
    out, err = call("skill_save", {"name": "Cierre Mensual", "description": "d", "instructions": "v2"})
    assert out["updated"] and "v2" in (work.home / ".claude" / "skills" / "cierre-mensual" / "SKILL.md").read_text()


# ---------------------------------------------------------------------------------------------- the locks

def hook_args(work):
    from hokma.kit.settings import hardblock_command
    import shlex
    parts = shlex.split(hardblock_command())
    return parts[parts.index("--data"):]


WRITES = ["agents/nuevo.md", ".claude/agents/nuevo.md", ".claude/skills/mi-skill/SKILL.md", "CLAUDE.md", "HOKMA.md",
          "AGENTS.md"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths")
@pytest.mark.parametrize("rel", WRITES)
@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
def test_no_lock_stops_writing_agents_skills_or_instructions_posix(work, rel, tool):
    target = work.home / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"tool_name": tool, "cwd": str(work.home),
               "tool_input": {"file_path": str(target), "content": "texto", "old_string": "a", "new_string": "b"}}
    protect = [str(p) for p in protected_paths()]
    assert guard.decide(payload, protect) == (0, "")
    assert hardblock.evaluate(payload, hardblock.build_ctx(hook_args(work))) is None
    # relative path, as Claude Code can send it
    payload["tool_input"]["file_path"] = rel
    assert guard.decide(payload, protect) == (0, "")
    assert hardblock.evaluate(payload, hardblock.build_ctx(hook_args(work))) is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")
def test_commands_that_write_agents_and_skills_are_allowed(work):
    ctx = hardblock.build_ctx(hook_args(work))
    for command in (f"mkdir -p {work.home}/.claude/skills/s && echo hi > {work.home}/.claude/skills/s/SKILL.md",
                    f"cp {work.home}/agents/joel.md {work.home}/agents/copia.md",
                    f"echo x > {work.home}/CLAUDE.md"):
        payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(work.home)}
        assert hardblock.evaluate(payload, ctx) is None, command


@pytest.mark.parametrize("rel", WRITES)
def test_no_lock_stops_writing_agents_skills_or_instructions_windows_paths(rel):
    """Windows drive paths judged by the same code (ntpath rules): the working folder is not protected."""
    work, data = "C:\\Users\\Ana\\Hokma", "C:\\Users\\Ana\\AppData\\Local\\Hokma"
    protect = [data, ntpath.join(data, "claude-config"), "C:\\Users\\Ana\\.local\\bin\\hokma.cmd",
               ntpath.join(work, ".hokma")]
    target = ntpath.join(work, *rel.split("/"))
    ctx = hardblock.Ctx(data, [work], protect, home="C:\\Users\\Ana", cwd=work)
    for tool in ("Write", "Edit"):
        payload = {"tool_name": tool, "cwd": work, "tool_input": {"file_path": target, "content": "x",
                                                                   "old_string": "a", "new_string": "b"}}
        assert hardblock.evaluate(payload, ctx) is None, (tool, target)
        assert not guard.under_any(target, protect[:1]) or sys.platform != "win32"
    assert not any(ntpath.normcase(target).startswith(ntpath.normcase(p) + "\\") for p in protect)


def test_only_the_settings_registration_stays_locked(work):
    protect = [str(p) for p in protected_paths()]

    def write(target):
        payload = {"tool_name": "Write", "cwd": str(work.home), "tool_input": {"file_path": str(target), "content": "x"}}
        return guard.decide(payload, protect)[0]

    assert write(work.data / "claude-config" / "settings.json") == 2        # content without the hooks
    assert write(work.home / ".hokma" / "x.json") == 0                       # task 3169: no longer protected
    assert write(work.data / "claude-config" / "plugins" / "x.json") == 0

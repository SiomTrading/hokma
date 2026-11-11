"""Hokma hands its instructions to the engine itself (task 3168): the engine never has to find a file in the folder."""
import json

from engine_support import connect, env, make_client, posix_only, turn  # noqa: F401
from hokma.engine.engine import ChatEngine
from hokma.kit import instructions, owner as owner_names

pytestmark = posix_only


def flag_value(argv, name):
    return argv[argv.index(name) + 1]


def _folder(home, hokma="Name of the fund: Fondo Zeta. Owner: Ana.\n", own="@HOKMA.md\n\nRule of the student: never touch X.\n"):
    if hokma is not None:
        (home / "HOKMA.md").write_text(hokma, encoding="utf-8")
    if own is not None:
        (home / "CLAUDE.md").write_text(own, encoding="utf-8")


def test_build_has_identity_manual_and_student_file_once(env):
    owner_names.save("Ana", "Fondo Zeta")
    _folder(env.home)
    text = instructions.build(str(env.home), None)
    assert text.startswith("# Identity")
    assert "Hokma, the agent team of the fund of Ana" in text
    assert "Claude Code" in text and "which engine or model" in text      # says how to answer, never claims to be it
    assert text.count("Name of the fund: Fondo Zeta") == 1                 # the @HOKMA.md line is not repeated
    assert "@HOKMA.md" not in text and "Rule of the student: never touch X." in text


def test_build_without_manual_in_folder_uses_the_template_with_names(env):
    owner_names.save("Ana", "Fondo Zeta")
    text = instructions.build(str(env.home), None)
    assert "Fondo Zeta" in text and "{{" not in text and "You are Hokma, the AI agent team" in text


def test_build_inlines_imports_only_inside_the_folder(env, tmp_path):
    (tmp_path / "outside.md").write_text("SECRET OUTSIDE")
    (env.home / "extra.md").write_text("Extra rules.")
    _folder(env.home, own="@extra.md\n@../outside.md\n")
    text = instructions.build(str(env.home), None)
    assert "Extra rules." in text and "SECRET OUTSIDE" not in text


def test_build_adds_the_persona_of_the_agent(env):
    _folder(env.home)
    agents = env.home / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "joel.md").write_text("---\nname: joel\n---\nYou are Joel, the developer.\n")
    assert "You are Joel, the developer." in instructions.build(str(env.home), "joel")
    assert "You are Joel" not in instructions.build(str(env.home), None)


def test_no_em_dash_in_what_hokma_writes():
    assert chr(0x2014) not in instructions.IDENTITY


def test_claude_code_gets_the_text_in_a_file_and_the_connectors_flag(env):
    owner_names.save("Ana", "Fondo Zeta")
    _folder(env.home)
    engine = env.engine()
    with make_client(engine) as client, connect(client) as ws:
        assert turn(ws, "quien eres")[-1]["type"] == "done"
    argv = env.entries("start")[0]["argv"]
    path = flag_value(argv, "--append-system-prompt-file")
    body = open(path, encoding="utf-8").read()
    assert "Hokma, the agent team of the fund of Ana" in body and "Rule of the student: never touch X." in body
    assert "project" not in flag_value(argv, "--setting-sources") if "--setting-sources" in argv else True


def test_two_conversations_do_not_share_or_overwrite_the_file(env, tmp_path):
    folder = tmp_path / "ins"
    a, b = instructions.write_file("one\n", folder), instructions.write_file("two\n", folder)
    assert a != b and open(a).read() == "one\n" and open(b).read() == "two\n"
    assert instructions.write_file("one\n", folder) == a


def test_real_launch_keeps_project_settings_out_and_connectors_off(env):
    from hokma.kit.launch import prepare_claude_launch
    spec = prepare_claude_launch()
    assert spec.args[spec.args.index("--setting-sources") + 1] == "user"     # 'project' would load the folder's settings
    assert spec.env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"


def test_the_scheduler_hands_the_instructions_too(env):
    import inspect
    from hokma.scheduler import runner
    assert "instructions.build(" in inspect.getsource(runner)


def test_other_engines_receive_the_same_text(env):
    owner_names.save("Ana", "Fondo Zeta")
    _folder(env.home)
    assert ChatEngine.instructions_for(str(env.home), None) == instructions.build(str(env.home), None)


def test_a_link_in_the_folder_never_pulls_a_secret_into_the_instructions(env, tmp_path):
    secret = tmp_path / "credentials.json"
    secret.write_text("TOP-SECRET-KEY")
    (env.home / "HOKMA.md").symlink_to(secret)
    (env.home / "CLAUDE.md").write_text("@linked.md\n@../credentials.json\n")
    (env.home / "linked.md").symlink_to(secret)
    agents = env.home / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "joel.md").symlink_to(secret)
    for agent in (None, "joel", "../../../credentials"):
        text = instructions.build(str(env.home), agent)
        assert "TOP-SECRET-KEY" not in text


def test_a_hard_link_to_a_secret_is_not_read(env, tmp_path):
    import os
    secret = tmp_path / "key.txt"
    secret.write_text("TOP-SECRET-KEY")
    os.link(secret, env.home / "HOKMA.md")
    assert "TOP-SECRET-KEY" not in instructions.build(str(env.home), None)


def test_imports_are_plain_markdown_and_never_hidden(env):
    (env.home / ".hokma").mkdir()
    (env.home / ".hokma" / "extensions.md").write_text("HIDDEN-ONE")
    (env.home / "notes.txt").write_text("NOT-MARKDOWN")
    (env.home / "ok.md").write_text("PLAIN-OK")
    (env.home / "CLAUDE.md").write_text("@.hokma/extensions.md\n@notes.txt\n@ok.md\n")
    text = instructions.build(str(env.home), None)
    assert "PLAIN-OK" in text and "HIDDEN-ONE" not in text and "NOT-MARKDOWN" not in text

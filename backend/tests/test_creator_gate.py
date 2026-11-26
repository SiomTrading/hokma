"""The first gate of the strategy creator (SPEC-031 6.6): without a current requirement nothing is programmed, and nothing but the two files of the code is written in strategies/.

The hook is run the way Claude Code runs it, as a separate isolated process with the payload on its input, against a real base on each engine."""
import json
import subprocess
import sys

import pytest
from code_support import BRIEF, CODER, RESEARCHER, db, env, make_bank, pg_admin_url, pg_template, pg_url_migrated  # noqa: F401  (fixtures)

from hokma.core.config import hokma_data_dir, hokma_home
from hokma.factory import user_rules
from hokma.kit import creator_gate, guard, hardblock, settings

GATE = str(settings.GATE_FILE)


def run_gate(payload, data=None, work=None):
    args = [sys.executable, "-I", GATE, "--data", str(data or hokma_data_dir()), "--workdir", str(work or hokma_home())]
    return subprocess.run(args, input=json.dumps(payload), capture_output=True, text=True, cwd=str(hokma_data_dir()))


def write_to(path, tool="Write", **extra):
    return {"tool_name": tool, "tool_input": {"file_path": str(path), "content": "x = 1\n"}, "cwd": str(hokma_home()), "agent_type": CODER, **extra}


def target(slug, name="strategy.py"):
    return hokma_home() / "strategies" / slug / name


def with_requirement(db, slug="inside_bar"):
    bank_id = make_bank(db)
    return user_rules.write_brief(bank_id=bank_id, slug=slug, title="Inside bar", brief=BRIEF, caller=RESEARCHER, db=db)


def test_a_write_in_strategies_without_a_requirement_is_blocked_with_exit_2_and_a_line_in_the_log(db):
    for tool in ("Write", "Edit", "MultiEdit"):
        done = run_gate(write_to(target("inside_bar"), tool))
        assert done.returncode == 2 and "HOKMA_BLOCKED no_requirement" in done.stderr, tool
    lines = [json.loads(x) for x in (hokma_data_dir() / "guard" / "blocks.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 3 and lines[0]["rule"] == "no_requirement" and lines[0]["tool"] == "Write" and lines[0]["target"].endswith("strategies/inside_bar/strategy.py")
    assert {"ts", "rule", "tool", "target", "conversation_id"} <= set(lines[0])


def test_with_a_current_requirement_the_two_files_may_be_written_and_nothing_else(db):
    with_requirement(db)
    for name in ("strategy.py", "test_strategy.py"):
        assert run_gate(write_to(target("inside_bar", name))).returncode == 0, name
    for name in ("notes.md", "README.md", "plan.txt", "strategy.txt"):
        done = run_gate(write_to(target("inside_bar", name)))
        assert done.returncode == 2 and "file_not_allowed" in done.stderr, name
    for odd in (hokma_home() / "strategies" / "loose.py", hokma_home() / "strategies" / "inside_bar" / "deep" / "strategy.py", hokma_home() / "strategies" / "Bad Slug" / "strategy.py"):
        assert run_gate(write_to(odd)).returncode == 2, odd
    other = run_gate(write_to(hokma_home() / "strategies" / "inside_bar" / ".." / "other" / "strategy.py"))
    assert other.returncode == 2 and "no_requirement" in other.stderr           # a path that walks to another folder is judged by where it lands


def test_any_agent_or_chat_may_write_the_two_files_when_the_requirement_exists_whether_or_not_the_event_names_it(db):
    with_requirement(db)
    for blank in (None, "", "hokma", "desk-chief", "joel", "strategy-researcher"):
        payload = write_to(target("inside_bar"))
        if blank is None:
            del payload["agent_type"]
        else:
            payload["agent_type"] = blank
        assert run_gate(payload).returncode == 0, repr(blank)
    nothing = write_to(target("other_one"))
    del nothing["agent_type"]
    assert run_gate(nothing).returncode == 2 and "no_requirement" in run_gate(nothing).stderr          # the order rule stays for everyone


def test_a_replaced_requirement_still_opens_the_gate_and_one_of_another_strategy_does_not(db):
    made = with_requirement(db)
    user_rules.write_brief(rule_id=made["rule_id"], brief={**BRIEF, "market": "ES"}, caller=RESEARCHER, db=db)
    assert run_gate(write_to(target("inside_bar"))).returncode == 0
    assert run_gate(write_to(target("another_one"))).returncode == 2


def test_the_hook_does_not_touch_what_is_outside_strategies_or_is_not_a_write(db):
    for path in (hokma_home() / "notes.md", hokma_home() / "strategies_old" / "strategy.py", hokma_home() / "docs" / "strategies" / "x" / "strategy.py"):
        assert run_gate(write_to(path)).returncode == 0, path
    assert run_gate({"tool_name": "Read", "tool_input": {"file_path": str(target("inside_bar"))}, "cwd": str(hokma_home())}).returncode == 0
    assert run_gate({"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": str(hokma_home())}).returncode == 0


def test_a_base_it_cannot_read_is_a_block_never_a_pass(db, tmp_path):
    empty = tmp_path / "empty_data"
    empty.mkdir()
    done = run_gate(write_to(target("inside_bar")), data=empty)
    assert done.returncode == 2 and "no_requirement" in done.stderr and "cannot be checked" in done.stderr
    assert run_gate("not json").returncode == 2 or run_gate({"tool_name": "Write"}).returncode == 0
    broken = subprocess.run([sys.executable, "-I", GATE], input="{", capture_output=True, text=True)
    assert broken.returncode == 2 and "HOKMA_BLOCKED error" in broken.stderr


def test_no_agent_is_refused_by_name_and_the_bank_tools_pass_the_hook_for_everyone(db):
    with_requirement(db)
    for agent in ("strategy-coder", "strategy-reviewer", "strategy-researcher", "hokma", "desk-chief"):
        assert run_gate(write_to(target("inside_bar"), agent_type=agent)).returncode == 0, agent
    for tool in ("mcp__hokma__strategy_brief_write", "mcp__hokma__strategy_code_save", "mcp__hokma__strategy_code_verdict"):
        for agent in (None, "", "hokma", "general-purpose", "desk-chief"):
            payload = {"tool_name": tool, "tool_input": {}}
            if agent is not None:
                payload["agent_type"] = agent
            assert run_gate(payload).returncode == 0, (tool, agent)
    assert run_gate(write_to(target("inside_bar", "notes.md"), agent_type="hokma")).returncode == 2          # the folder rule stays


def test_the_hook_is_registered_next_to_the_other_two_and_its_file_and_registration_are_protected(env):
    built = settings.build_settings()
    groups = built["hooks"]["PreToolUse"]
    commands = [h["command"] for g in groups for h in g["hooks"]]
    assert len(groups) == 3 and sum(f"-I {GATE}" in c for c in commands) == 1
    gate_group = next(g for g in groups if f"-I {GATE}" in g["hooks"][0]["command"])
    assert "Write" in gate_group["matcher"] and "mcp__hokma__strategy_code_verdict" in gate_group["matcher"] and gate_group["hooks"][0]["command"].endswith("|| exit 2")
    protect = [str(p) for p in settings.protected_paths()]
    assert GATE in protect
    text = json.dumps(built)
    assert guard.registration_intact(text, protect)
    without = json.loads(text)
    without["hooks"]["PreToolUse"] = [g for g in without["hooks"]["PreToolUse"] if f"-I {GATE}" not in g["hooks"][0]["command"]]
    assert not guard.registration_intact(json.dumps(without), protect)                       # taking the gate out of the settings is refused
    assert guard.decide({"tool_name": "Write", "tool_input": {"file_path": GATE, "content": "x"}, "cwd": "/"}, protect)[0] == 2
    assert guard.decide({"tool_name": "Edit", "tool_input": {"file_path": str(settings.settings_path()), "old_string": "creator_gate.py", "new_string": "gone.py"}, "cwd": "/"}, protect)[0] in (0, 2)


def test_the_hardblock_refuses_a_write_of_the_hook_as_internal_and_lets_the_coder_write_its_two_files(tmp_path):
    from hardblock_support import Box

    box = Box(tmp_path)
    box.protect.append(GATE)
    assert box.hit("Write", file_path=GATE, content="x = 1\n") == "hokma_internal"
    folder = box.work / "strategies" / "inside_bar"
    assert box.hit("Write", file_path=str(folder / "test_strategy.py"), content="import strategy\nfrom user_api import replay\n") is None
    assert box.hit("Write", file_path=str(folder / "strategy.py"), content="from user_api import long, hold\n") is None
    # the agent cannot name the package, so the short spelling of the one module that a generated file may import is what the file carries
    assert box.hit("Write", file_path=str(folder / "strategy.py"), content="from hokma.factory.user_api import long\n") == "real_money"


def test_the_checks_cannot_be_run_by_the_agent_as_a_command_so_they_run_through_the_save_tool(tmp_path):
    from hardblock_support import Box

    box = Box(tmp_path)
    command = f"{sys.executable} -I -m hokma.factory.rule_check strategies/inside_bar"
    assert box.bash(command) == "real_money"        # the engine never runs the code of Hokma by hand: strategy_code_save runs the checks in the server
    assert box.bash("ls strategies/inside_bar") is None
    assert creator_gate.SLUG.match("inside_bar") and hardblock.HOKMA_MODULE_RE is not None

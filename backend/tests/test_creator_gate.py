"""The first gate of the strategy creator (1.2.3): without the spec.md in the folder of the strategy nothing is programmed, and the documents of the flow are free.

The hook is run the way Claude Code runs it, as a separate isolated process with the payload on its input, against the folder of the fund."""
import json
import subprocess
import sys

from bank_support import env  # noqa: F401  (fixture: an isolated working folder and data folder)

from hokma.core.config import hokma_data_dir, hokma_home
from hokma.kit import settings

GATE = str(settings.GATE_FILE)


def run_gate(payload, work=None):
    hokma_data_dir().mkdir(parents=True, exist_ok=True)
    args = [sys.executable, "-I", GATE, "--data", str(hokma_data_dir()), "--workdir", str(work or hokma_home())]
    return subprocess.run(args, input=json.dumps(payload), capture_output=True, text=True, cwd=str(hokma_data_dir()))


def write_to(path, tool="Write", **extra):
    return {"tool_name": tool, "tool_input": {"file_path": str(path), "content": "x = 1\n"}, "cwd": str(hokma_home()), "agent_type": "strategy-coder", **extra}


def folder(name="STR-001-inside-bar"):
    return hokma_home() / "service" / "strategies" / name


def test_code_without_a_spec_is_blocked_with_exit_2_and_a_line_in_the_log(env):
    for tool in ("Write", "Edit", "MultiEdit"):
        done = run_gate(write_to(folder() / "versions" / "v1" / "strategy.py", tool))
        assert done.returncode == 2 and "HOKMA_BLOCKED no_requirement" in done.stderr, tool
    lines = [json.loads(x) for x in (hokma_data_dir() / "guard" / "blocks.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 3 and lines[0]["rule"] == "no_requirement" and lines[0]["target"].endswith("versions/v1/strategy.py")


def test_with_the_spec_the_code_of_a_version_goes_in_and_the_documents_of_the_flow_are_free(env):
    folder().mkdir(parents=True, exist_ok=True)
    (folder() / "spec.md").write_text("# spec\n", encoding="utf-8")
    for name in ("strategy.py", "test_strategy.py"):
        for who in (None, "", "hokma", "joel", "strategy-coder"):
            payload = write_to(folder() / "versions" / "v1" / name)
            payload["agent_type"] = who
            assert run_gate(payload).returncode == 0, (name, who)
    for name in ("plan.md", "tasks.md", "checklist.md", "adr/ADR-1-x.md", "results/OPT-1-2026-01-01.md"):
        assert run_gate(write_to(folder() / name)).returncode == 0, name


def test_the_code_only_goes_in_a_version_folder_of_a_numbered_strategy_and_never_out_by_a_path_that_walks(env):
    folder().mkdir(parents=True, exist_ok=True)
    (folder() / "spec.md").write_text("# spec\n", encoding="utf-8")
    for odd in (folder() / "strategy.py", folder() / "versions" / "strategy.py", folder() / "versions" / "v1" / "deep" / "strategy.py", folder("inside_bar") / "versions" / "v1" / "strategy.py",
                hokma_home() / "service" / "strategies" / "strategy.py"):
        assert run_gate(write_to(odd)).returncode == 2, odd
    other = run_gate(write_to(folder() / "versions" / "v1" / ".." / ".." / ".." / "STR-002-other" / "versions" / "v1" / "strategy.py"))
    assert other.returncode == 2 and "no_requirement" in other.stderr          # a path that walks to another folder is judged by where it lands


def test_the_hook_does_not_touch_what_is_outside_the_strategy_folders_or_is_not_a_write(env):
    for path in (hokma_home() / "notes.md", hokma_home() / "strategies" / "x" / "strategy.py", hokma_home() / "service" / "other" / "strategy.py"):
        assert run_gate(write_to(path)).returncode == 0, path
    assert run_gate({"tool_name": "Read", "tool_input": {"file_path": str(folder() / "versions" / "v1" / "strategy.py")}, "cwd": str(hokma_home())}).returncode == 0
    assert run_gate({"tool_name": "Write", "tool_input": {}, "cwd": str(hokma_home())}).returncode == 0

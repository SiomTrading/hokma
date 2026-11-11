"""The hook as a process (tests P8 and P11): output, log line, fail closed, speed and the settings that carry it."""
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hardblock_support import HARDBLOCK_FILE, Box, box  # noqa: F401  (fixture)
from hokma.i18n import t
from hokma.kit import hardblock, integrity
from hokma.kit.launch import IntegrityError, check_integrity, prepare_claude_launch
from hokma.kit.settings import build_settings, hardblock_command, protected_paths


def payload(command, tool="Bash", cwd=None):
    return {"tool_name": tool, "tool_input": {"command": command}, "cwd": str(cwd)}


def test_a_refusal_prints_the_marker_the_reason_and_writes_one_log_line(box):
    done = box.run_hook(payload("rm -rf d", cwd=box.work), {"HOKMA_CONVERSATION_ID": "12"})
    assert done.returncode == 2 and done.stdout == ""
    lines = done.stderr.splitlines()
    assert lines[0] == "HOKMA_BLOCKED delete"
    assert t("hardblock.delete", target="rm -rf d") == lines[1] and t("hardblock.hint.delete") == lines[2]
    log = (box.data / "guard" / "blocks.jsonl").read_text().splitlines()
    assert len(log) == 1
    row = json.loads(log[0])
    assert set(row) == {"ts", "rule", "tool", "target", "conversation_id"}
    assert (row["rule"], row["tool"], row["target"], row["conversation_id"]) == ("delete", "Bash", "rm -rf d", "12")
    assert row["ts"].endswith("Z")


def test_the_log_target_is_cut_to_200_characters_and_never_holds_file_contents(box):
    long = "rm " + "x" * 400
    box.run_hook(payload(long, cwd=box.work))
    row = json.loads((box.data / "guard" / "blocks.jsonl").read_text().splitlines()[-1])
    assert len(row["target"]) <= 200
    box.run_hook({"tool_name": "Write", "tool_input": {"file_path": "/tmp/x.txt", "content": "SECRET-CONTENT"},
                  "cwd": str(box.work)})
    assert "SECRET-CONTENT" not in (box.data / "guard" / "blocks.jsonl").read_text()


def test_task_id_travels_with_the_line_for_scheduled_runs(box):
    box.run_hook(payload("rm f", cwd=box.work), {"HOKMA_TASK_ID": "31"})
    assert json.loads((box.data / "guard" / "blocks.jsonl").read_text().splitlines()[-1])["task_id"] == "31"


def test_allowed_calls_exit_0_silently_and_log_nothing(box):
    done = box.run_hook(payload("git status", cwd=box.work))
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    assert not (box.data / "guard" / "blocks.jsonl").exists()


def test_the_message_is_in_the_active_language(box):
    done = box.run_hook(payload("rm f", cwd=box.work), {"HOKMA_LANG": "es"})
    assert done.stderr.splitlines()[0] == "HOKMA_BLOCKED delete" and "bloque" in done.stderr


@pytest.mark.parametrize("stdin", ["not json", "", "[]", '{"tool_name": 3}', '{"tool_input": {}}'])
def test_it_fails_closed_on_bad_input(box, stdin):
    args = [sys.executable, "-I", str(HARDBLOCK_FILE), "--data", str(box.data), "--workdir", str(box.work)]
    done = subprocess.run(args, input=stdin, capture_output=True, text=True, cwd=box.data)
    assert done.returncode == 2 and done.stderr.startswith("HOKMA_BLOCKED error")


def test_it_fails_closed_without_folders_or_with_a_damaged_broker_list(box, tmp_path, monkeypatch):
    run = lambda *a: subprocess.run([sys.executable, "-I", str(HARDBLOCK_FILE), *a], input=json.dumps(  # noqa: E731
        payload("ls", cwd=box.work)), capture_output=True, text=True, cwd=box.data)
    assert run().returncode == 2                                         # no --data
    assert run("--data", str(box.data)).returncode == 2                  # no --workdir
    monkeypatch.setattr(hardblock, "BROKERS_FILE", str(tmp_path / "missing.json"))
    with pytest.raises(OSError):
        hardblock.load_brokers(str(tmp_path / "missing.json"))
    (tmp_path / "damaged.json").write_text("{not json")
    with pytest.raises(ValueError):
        hardblock.load_brokers(str(tmp_path / "damaged.json"))


def test_a_log_problem_never_lets_a_blocked_call_through(box):
    (box.data / "guard").rmdir()
    (box.data / "guard").write_text("a file where the folder should be")        # the log cannot be written
    done = box.run_hook(payload("rm f", cwd=box.work))
    assert done.returncode == 2 and done.stderr.startswith("HOKMA_BLOCKED delete")


def test_the_hook_command_is_the_guards_launch_with_the_folders_it_needs(box, monkeypatch, tmp_path):
    monkeypatch.setenv("HOKMA_HOME", str(box.work))
    monkeypatch.setenv("HOKMA_DATA_DIR", str(box.data))
    cmd = hardblock_command()
    assert cmd.startswith(f"cd {box.data} && ") and cmd.endswith("|| exit 2")
    assert f"{sys.executable} -I {HARDBLOCK_FILE}" in cmd and f"--data {box.data}" in cmd and f"--workdir {box.work}" in cmd
    for p in protected_paths():
        assert f"--protect {p}" in cmd
    # run through a shell exactly as Claude Code does: the block surfaces as exit 2, an allowed call as 0
    blocked = subprocess.run(["sh", "-c", cmd], input=json.dumps(payload("rm f", cwd=box.work)), capture_output=True, text=True)
    allowed = subprocess.run(["sh", "-c", cmd], input=json.dumps(payload("ls", cwd=box.work)), capture_output=True, text=True)
    assert (blocked.returncode, allowed.returncode) == (2, 0)
    broken = subprocess.run(["sh", "-c", cmd.replace(sys.executable, "/nonexistent/python")], input="{}",
                            capture_output=True, text=True)
    assert broken.returncode == 2                                        # a missing interpreter must still block


def test_p8_the_settings_carry_the_hook_for_every_tool_and_pin_the_hooks_on(box, monkeypatch):
    monkeypatch.setenv("HOKMA_HOME", str(box.work))
    monkeypatch.setenv("HOKMA_DATA_DIR", str(box.data))
    settings = build_settings()
    assert settings["disableAllHooks"] is False
    groups = settings["hooks"]["PreToolUse"]
    assert [g["matcher"] for g in groups] == ["Edit|MultiEdit|Write|NotebookEdit", ".*"]
    assert groups[1]["hooks"][0]["command"] == hardblock_command() and "{{" not in json.dumps(settings)
    spec = prepare_claude_launch()
    assert spec.args[spec.args.index("--setting-sources") + 1] == "user"     # no project or local settings sources
    written = json.loads(Path(spec.args[1]).read_text())
    assert written["disableAllHooks"] is False
    # the agent "edits" the generated file and turns the hooks off: the next launch puts it back
    Path(spec.args[1]).write_text(json.dumps({"disableAllHooks": True}))
    prepare_claude_launch()
    assert json.loads(Path(spec.args[1]).read_text()) == written


def test_p8_a_hook_planted_in_the_working_folder_does_not_replace_the_real_one(box, monkeypatch):
    monkeypatch.setenv("HOKMA_HOME", str(box.work))
    monkeypatch.setenv("HOKMA_DATA_DIR", str(box.data))
    fake = box.work / "hokma" / "kit"
    fake.mkdir(parents=True)
    (fake / "hardblock.py").write_text("import sys\nsys.exit(0)\n")
    (box.work / "hokma" / "__init__.py").write_text("")
    (fake / "__init__.py").write_text("")
    done = subprocess.run(["sh", "-c", hardblock_command()], input=json.dumps(payload("rm f", cwd=box.work)),
                          capture_output=True, text=True, cwd=box.work, env={**os.environ, "PYTHONPATH": str(box.work)})
    assert done.returncode == 2 and "HOKMA_BLOCKED delete" in done.stderr


def installed_app(tmp_path, monkeypatch) -> Path:
    """A copy of the package laid out like an installed slot: <data>/runtime/a/app/backend/hokma/..."""
    app = tmp_path / "data" / "runtime" / "a" / "app"
    shutil.copytree(Path(hardblock.__file__).resolve().parents[2], app / "backend" / "hokma_copy_src" / "..",
                    dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "tests"))
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(integrity, "app_root", lambda: app)
    return app


def test_p8_changing_the_hook_or_its_data_makes_the_integrity_gate_refuse_to_start(tmp_path, monkeypatch):
    app = tmp_path / "data" / "runtime" / "a" / "app"
    kit = app / "backend" / "hokma" / "kit"
    kit.mkdir(parents=True)
    for name in ("hardblock.py", "hardblock_brokers.json", "guard.py", "trash_mcp.py"):
        shutil.copy(Path(hardblock.__file__).parent / name, kit / name)
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(integrity, "app_root", lambda: app)
    manifest = json.loads(integrity.write_manifest().read_text())["files"]
    assert {f"backend/hokma/kit/{n}" for n in ("hardblock.py", "hardblock_brokers.json", "trash_mcp.py")} <= set(manifest)
    check_integrity()                                                    # untouched: starts
    (kit / "hardblock.py").write_text("import sys\nsys.exit(0)\n")       # someone neuters the hook
    assert integrity.verify() == [t("integrity.modified", file="backend/hokma/kit/hardblock.py")]
    with pytest.raises(IntegrityError):
        check_integrity()
    shutil.copy(Path(hardblock.__file__), kit / "hardblock.py")
    (kit / "hardblock_brokers.json").write_text('{"hosts": [], "url_patterns": []}')   # or empties the broker list
    with pytest.raises(IntegrityError):
        check_integrity()
    shutil.copy(Path(hardblock.__file__).parent / "hardblock_brokers.json", kit / "hardblock_brokers.json")
    (kit / "extra_hook.py").write_text("print(1)")                       # a new file is refused too
    assert any("extra_hook.py" in p for p in integrity.verify())


def test_p8_the_real_package_lists_the_new_files_in_its_manifest():
    files = integrity.compute(integrity.app_root())
    for name in ("hardblock.py", "hardblock_brokers.json", "trash_mcp.py", "templates/settings.base.json"):
        assert f"backend/hokma/kit/{name}" in files


def test_p11_the_hook_is_fast_enough(box):
    """Median of 150 ms or less per tool call (measured here on Linux; Windows is measured by QA, see the docs)."""
    commands = ["git status", "ls -la", "python -c \"print(1)\"", "echo hello > out.txt", "rm -rf d"]
    box.run_hook(payload("ls", cwd=box.work))                            # warm the file cache
    times = []
    for i in range(30):
        start = time.perf_counter()
        box.run_hook(payload(commands[i % len(commands)], cwd=box.work))
        times.append((time.perf_counter() - start) * 1000)
    median = statistics.median(times)
    print(f"hook median {median:.0f} ms, max {max(times):.0f} ms over {len(times)} calls")
    assert median <= 150


def test_p4_every_variant_of_the_plan_through_the_real_hook_process_keeps_every_file(box, tmp_path):
    """The agent's shell only runs a command when the hook exits 0. The same commands DO delete without the hook
    (the control below), and with it every file keeps its sha256."""
    from test_hardblock_rules import P4_DELETE

    spec = P4_DELETE[:P4_DELETE.index("mv f /dev/null") + 1]
    assert len(spec) == 29
    (box.work / "borra.py").write_text("import os\nos.unlink('f')\n")
    before = box.tree_hash()
    for command in spec:
        done = box.run_hook(payload(command, cwd=box.work))
        assert done.returncode == 2 and done.stderr.splitlines()[0] == "HOKMA_BLOCKED delete", command
        # the call is refused, so the shell never runs it
    assert box.tree_hash() == before
    assert len((box.data / "guard" / "blocks.jsonl").read_text().splitlines()) == len(spec)

    control = tmp_path / "control"
    shutil.copytree(box.work, control)
    subprocess.run(["sh", "-c", "rm -rf d f"], cwd=control, check=True)
    assert not (control / "f").exists() and not (control / "d").exists()      # without the hook it is gone


def test_the_project_folder_set_by_the_engine_is_a_second_working_folder(box, tmp_path):
    other = tmp_path / "other-project"
    other.mkdir()
    args = [sys.executable, "-I", str(HARDBLOCK_FILE), "--data", str(box.data), "--workdir", str(box.work), "--protect", str(box.hooks)]

    def run(command, root):
        env = {**os.environ, "HOKMA_LANG": "en", **({"HOKMA_WORK_ROOT": str(root)} if root else {})}
        return subprocess.run(args, input=json.dumps(payload(command, cwd=other)), capture_output=True, text=True,
                              cwd=box.data, env=env).returncode

    assert run("echo hi > out.txt", None) == 2                # cwd outside the configured folders
    assert run("echo hi > out.txt", other) == 0               # the folder the engine launched `claude` in
    assert run(f"echo hi > {tmp_path}/third.txt", other) == 2

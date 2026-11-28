"""What is free in 1.3.1 (task 3436), after the lock of the modules of Hokma left the hook (SPEC-015 18.7, bug 1221): a student's own venv, the third-party `schwab` library, the
modules of Hokma and the interpreter of its runtime run like any other python."""
import pytest
from hardblock_support import box  # noqa: F401  (fixture)


# ------------------------------------------------------------------------------------------- the modules


# ------------------------------------------------------------------------------------------- the interpreter


# ------------------------------------------------------------------------------------------- what stays free

FREE_COMMANDS = [
    ".venv/bin/python -c 'print(1)'", "venv/bin/python app.py", "./venv/Scripts/python.exe app.py",
    "python3 -m venv .venv", ".venv/bin/pip install requests", "source .venv/bin/activate && python app.py",
    "python -c 'import schwab'", "python -c 'from schwab import auth, client'", "pip install schwab-py",
    "python -m hokma doctor", "python -c 'import hokma'", "python -c 'import hokma.market'", "python -c 'import hokma as h; h.market'", "python -m hokma.factory.rule_check x",
    "python -c 'from hokma.factory import orders'", "python -c 'import hokma.brokerage'", "python -c 'import hokma_extras'", "python -c 'import hokma_tools'",
    "python -c 'print(\"broker\", \"schwab\", \"connections\")'", "python -c 'import socket; socket.connections'",
    "python -c 'import brokerage_tools'", "python -c 'from mylib import broker'",
    "cat docs/broker.md", "ls connections", "ls schwab", "grep -r broker notes", "ls hokma-notes", "ls hokma",
    "echo hokma", "cat hokma.txt", "git commit -m 'import from hokma docs'",
    "python -c 'import importlib; importlib.import_module(\"json\")'",
    "cat runtime/a/venv/notes.txt", "ls runtime/a/venv/bin",
]


@pytest.mark.parametrize("command", FREE_COMMANDS)
def test_legitimate_commands_are_not_blocked(box, command):
    assert box.bash(command) is None, command


def test_the_hokma_runtime_folder_itself_may_be_listed(box):
    (box.data / "runtime" / "a" / "venv" / "bin").mkdir(parents=True)
    assert box.bash(f"ls {box.data}/runtime/a/venv/bin") in (None, "secrets", "hokma_internal")   # never real_money
    assert box.bash(f"ls {box.data}/runtime/a/venv/bin") != "real_money"
    assert box.bash("ls", cwd=box.data / "runtime" / "a" / "venv" / "bin") != "real_money"


def test_a_script_of_the_student_that_does_not_import_them_runs(box):
    (box.work / "ok.py").write_text("import json\nfrom schwab import auth\nprint('connections', 'broker')\n")
    (box.work / "ok.sh").write_text("#!/bin/sh\n.venv/bin/python ok.py\n")
    assert box.bash("python ok.py") is None
    assert box.bash("bash ok.sh") is None


@pytest.mark.parametrize("target, content", [
    ("ok.py", "from schwab import auth\nprint('broker')\n"), ("notes.md", "see hokma.broker in the docs\n"),
    ("data.txt", "from hokma import broker\n"), ("ok.py", "import hokma_extras\nprint(hokma_extras.__name__)\n")])
def test_legitimate_files_are_written(box, target, content):
    assert box.hit("Write", file_path=str(box.work / target), content=content) is None

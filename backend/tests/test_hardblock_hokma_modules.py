"""The modules of Hokma that touch real money, keys or the server are `real_money` (SPEC-015 18.7, bug 1221; narrowed in 1.2.9, task 3431): no command, script or file an agent writes may import
or name `hokma.broker`, `hokma.schwab`, `hokma.connections`, `hokma.exec`... The interpreter of `<data>/runtime/<x>/venv` may run python (tests, rule_check, a backtest) but never pip, venv or
its other files. A student's own venv, the third-party `schwab` library and the free modules of Hokma (factory, backtest, quant) stay free."""
import os

import pytest
from hardblock_support import box  # noqa: F401  (fixture)

from hokma.permissions.broker import BrokerCtx, check


@pytest.fixture
def bctx(box):
    return BrokerCtx(data=str(box.data), roots=[str(box.work)], protect=[str(box.data)], conversation_id="7")


def venv_python(box, name: str = "a") -> str:
    return f"{box.data}/runtime/{name}/venv/bin/python"


# ------------------------------------------------------------------------------------------- the modules

MODULE_COMMANDS = [
    'python -c "import hokma.broker"',
    'python3 -c "import hokma.schwab as s"',
    "python -c 'import hokma.connections'",
    "python -c 'from hokma import broker'",
    "python -c 'from hokma import schwab, connections'",
    "python -c 'from hokma import market, broker'",
    'python -c "from hokma import (market,\n    broker)"',
    "python -c 'from hokma.broker.service import place'",
    "python -c 'from hokma.broker import rules'",
    "python -c 'import hokma; hokma.broker.service'",
    "python -c 'import importlib; importlib.import_module(\"hokma.broker\")'",
    "python -c 'import importlib; importlib.import_module(\"hokma.\" + \"broker\")'",
    "python -c '__import__(\"hokma.schwab\")'",
    "python -c '__import__(\"hokma.\" + \"connections\")'",
    "python -c 'import importlib.util; importlib.util.find_spec(\"hokma.broker\")'",
    "python -c 'import runpy; runpy.run_module(\"hokma.broker.service\")'",
    "python -c 'import os; os.path.join(\"hokma\", \"broker\")'",
    "python -c 'from pathlib import Path; Path(\"hokma\") / \"schwab\"'",
    "python -c 'import HOKMA.BROKER'", "python -c 'import hokma . broker'", "python -c 'import hokma.exec'", "python -c 'import hokma.db'",
    "python -m hokma.connections",
    "python3 -m hokma.broker.service --help",
    "python -I -c 'import hokma.broker'",
    "python -W ignore -c 'import hokma.broker'",
    "python -Sc 'import hokma.broker'",
    "cat backend/hokma/broker/rules.py",
    "ls hokma/schwab",
    "grep -r place hokma/connections",
    "cat hokma\\broker\\rules.py",
    'type hokma\\schwab\\orders.py',
    "sudo python -c 'import hokma.broker'",
    "env FOO=1 python -c 'import hokma.broker'",
    "bash -c \"python -c 'import hokma.broker'\"",
    "echo 'import hokma.broker' > x.py",
    "echo 'import hokma.broker' | python",
    "echo 'import hokma.broker' | python -",
    "cat > x.py <<'EOF'\nimport hokma.broker\nEOF",
    "python <<'EOF'\nfrom hokma import broker\nEOF",
    "python - <<EOF\nimport importlib\nimportlib.import_module('hokma.' + 'schwab')\nEOF",
    "printf 'import hokma.connections' >> run.py",
    "node -e \"require('child_process').execSync('python -c \\\"import hokma.broker\\\"')\"",
]


@pytest.mark.parametrize("command", MODULE_COMMANDS)
def test_a_command_that_names_a_broker_module_is_real_money(box, command):
    assert box.bash(command) == "real_money", command


@pytest.mark.parametrize("command", [
    'python -c "import hokma.broker"', "python -c 'from hokma import broker'", "type hokma\\schwab\\orders.py",
    "cat backend/hokma/connections/api.py"])
def test_the_same_in_powershell(box, command):
    assert box.ps(command) == "real_money", command


@pytest.mark.parametrize("body", [
    "import hokma.broker\n", "from hokma import broker\n", "from hokma.schwab import orders\n",
    "import importlib\nm = importlib.import_module('hokma.connections')\n", "m = __import__('hokma.broker')\n",
    "import hokma\nprint(hokma.schwab)\n", "import os\nsrc = os.path.join('hokma', 'broker', 'rules.py')\n",
    "import subprocess\nsubprocess.run(['python', '-c', 'import hokma.broker'])\n"])
@pytest.mark.parametrize("name", ["job.py", "sub/job.py"])
def test_a_script_of_the_working_folder_that_imports_them_is_refused_when_run(box, body, name):
    path = box.work / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(body)
    assert box.bash(f"python {name}") == "real_money"
    assert box.bash(f"python3 ./{name}") == "real_money"
    assert box.bash(f"python < {name}") == "real_money"


def test_a_script_run_directly_or_through_a_shell_script_is_refused(box):
    (box.work / "tool.py").write_text("#!/usr/bin/env python3\nimport hokma.broker\n")
    (box.work / "tool.sh").write_text("#!/bin/sh\npython -c 'from hokma import broker'\n")
    (box.work / "nosuffix").write_text("#!/usr/bin/env python3\nfrom hokma import schwab\n")
    for command in ("./tool.py", "bash tool.sh", "sh ./tool.sh", "source tool.sh", "./nosuffix"):
        assert box.bash(command) == "real_money", command


@pytest.mark.parametrize("tool, key", [("Write", "content"), ("Edit", "new_string"), ("NotebookEdit", "new_source")])
@pytest.mark.parametrize("target", ["x.py", "deep/x.py", "run.sh", "x.js", "x", "n.ipynb"])
def test_writing_a_script_that_imports_them_is_refused(box, tool, key, target):
    path = "notebook_path" if tool == "NotebookEdit" else "file_path"
    assert box.hit(tool, **{path: str(box.work / target), key: "from hokma import broker\n"}) == "real_money"
    assert box.hit(tool, **{path: str(box.work / target), key: "import hokma.schwab\n"}) == "real_money"


def test_multiedit_new_text_is_read_too(box):
    edits = [{"old_string": "a", "new_string": "b"}, {"old_string": "c", "new_string": "import hokma.connections"}]
    assert box.hit("MultiEdit", file_path=str(box.work / "x.py"), edits=edits) == "real_money"
    assert box.hit("MultiEdit", file_path=str(box.work / "x.py"), edits=edits[:1]) is None


# ------------------------------------------------------------------------------------------- the interpreter

def test_the_runtime_interpreter_runs_python_and_nothing_else_of_its_folder(box):
    for name in ("a", "b", "x-1"):
        assert box.bash(f"{venv_python(box, name)} -c 'print(1)'") is None
        assert box.bash(f"{venv_python(box, name)}3.12 x.py") is None
        assert box.bash(f"{venv_python(box, name)} -m pytest -q") is None
        assert box.bash(f"{venv_python(box, name)} -m hokma.factory.rule_check x") is None
        assert box.bash(f"{box.data}/runtime/{name}/venv/Scripts/python.exe -c 1") is None
        assert box.bash(f"{box.data}/runtime/{name}/venv/bin/pip install x") == "real_money"
        assert box.bash(f"{venv_python(box, name)} -m pip install x") == "real_money"
        assert box.bash(f"{venv_python(box, name)} -m ensurepip") == "real_money"
        assert box.bash(f"source {box.data}/runtime/{name}/venv/bin/activate && python x.py") == "real_money"
        assert box.bash(f". {box.data}/runtime/{name}/venv/bin/activate") == "real_money"


@pytest.mark.parametrize("form", ["$HOKMA_DATA", "${HOKMA_DATA}", "$HOKMA_DATA_DIR", "${HOKMA_DATA_DIR}", "$env:HOKMA_DATA"])
def test_the_runtime_interpreter_by_a_variable_is_the_same_as_by_path(box, form):
    assert box.bash(f"{form}/runtime/a/venv/bin/python -c 1") is None
    assert box.bash(f"{form}/runtime/a/venv/bin/pip install x") == "real_money"
    assert box.bash(f"{form}/runtime/a/venv/bin/python -c 'import hokma.broker'") == "real_money"


def test_the_runtime_interpreter_through_a_link_or_a_wrapper_keeps_the_same_rule(box):
    base = box.data / "runtime" / "a" / "venv" / "bin"
    base.mkdir(parents=True)
    (base / "python").write_text("")
    for wrapper in ("sudo", "env X=1", "nohup", "timeout 5", "time", "exec"):
        assert box.bash(f"{wrapper} {venv_python(box)} -c 1") is None, wrapper
        assert box.bash(f"{wrapper} {venv_python(box)} -c 'from hokma import broker'") == "real_money", wrapper
    assert box.bash(f"bash -c '{venv_python(box)} -m pip list'") == "real_money"


def test_a_script_that_runs_the_runtime_interpreter_for_a_module_of_money_is_refused(box):
    (box.work / "go.py").write_text(f"import subprocess\nsubprocess.run(['{venv_python(box)}', '-c', 'import hokma.broker'])\n")
    (box.work / "go.sh").write_text(f"#!/bin/sh\n{venv_python(box)} -m pip install x\n")
    assert box.bash("python go.py") == "real_money"
    assert box.bash("bash go.sh") == "real_money"


def test_the_hook_as_a_process_and_the_permission_broker_say_real_money(box, bctx):
    for command in ('python -c "import hokma.broker"', f"{venv_python(box)} -m pip install x"):
        done = box.run_hook({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(box.work)})
        assert done.returncode == 2 and "HOKMA_BLOCKED real_money" in done.stderr, command
        verdict = check("Bash", {"command": command}, cwd=str(box.work), ctx=bctx)
        assert verdict.allowed is False and verdict.rule in ("real_money", "hokma_internal"), command
    verdict = check("Write", {"file_path": str(box.work / "x.py"), "content": "from hokma import broker\n"},
                    cwd=str(box.work), ctx=bctx)
    assert (verdict.allowed, verdict.rule) == (False, "real_money")
    for tool, arguments in (("web_fetch", {"url": "https://example.com/?m=hokma.broker"}),
                            ("mcp__other__run", {"command": "python -c 'import hokma.schwab'"}),
                            ("mcp__other__run", {"command": f"{venv_python(box)} -m pip install x"})):
        verdict = check(tool, arguments, cwd=str(box.work), ctx=bctx)
        assert (verdict.allowed, verdict.rule) == (False, "real_money"), (tool, arguments)


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

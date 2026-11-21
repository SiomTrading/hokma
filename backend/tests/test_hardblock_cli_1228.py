"""The command line of Hokma and the two server entry points are `real_money` for an agent (SPEC-015, bug 1228): an agent
that runs `printf 'token\\n' | hokma engine --port N &` gets a server of its own, with the broker, the orders and the owner.
Only `hokma doctor`, `hokma version`, `hokma ext request(s)` and a lone `--help` stay free. Also here: the `partial`
and `marshal` forms of the python escapes."""
import pytest
from hardblock_support import box  # noqa: F401  (fixture)

from hokma.permissions.broker import BrokerCtx, check


@pytest.fixture
def bctx(box):
    return BrokerCtx(data=str(box.data), roots=[str(box.work)], protect=[str(box.data)], conversation_id="7")


BLOCKED = [
    "hokma engine", "hokma engine --port 8765", "printf 'token\\n' | hokma engine --port 8765 &",
    "printf 'token\\n' | hokma engine --port 8765", "nohup hokma engine --port 9000 &", "setsid hokma engine &",
    "hokma start", "hokma restart", "hokma stop", "hokma db migrate", "hokma update", "hokma kit-sync", "hokma post-update",
    "hokma init-config", "hokma owner", "hokma shortcuts", "hokma prepare-claude", "hokma install-tools",
    "hokma ext list", "hokma ext add-marketplace x", "hokma ext install a@b", "hokma ext", "hokma status", "hokma --foo doctor",
    "hokma --version engine", "hokma doctor --fix", "uv run hokma engine", "uv run --with x hokma start", "uvx hokma engine",
    "poetry run hokma db migrate", "uv run python -m hokma_cli engine", "pipx run hokma engine", "hokma engine --help", "HOKMA ENGINE",
    "/usr/local/bin/hokma engine", "./hokma engine", "../bin/hokma start", "~/.local/bin/hokma engine",
    "C:\\Users\\a\\hokma.exe engine", "hokma.exe start", "hokma.cmd start", "bin/hokma.bat restart",
    "python hokma engine", "python /x/bin/hokma engine", "python3 ./hokma start", "py hokma.exe start",
    "python -m hokma_cli engine", "python -m hokma_cli.main engine", "python -m hokma_cli.main status", "python3 -m hokma_cli start",
    "python -Im hokma_cli engine", "python -I -m hokma_cli engine", "python -Imhokma_cli engine", 'python -m"hokma_cli" engine',
    "python -m hokma_cli.main db migrate", "python -m hokma_cli.x", "python -Bm hokma_cli update", "py -3 -m hokma_cli engine",
    "sudo hokma engine", "sudo -u root hokma start", "env X=1 hokma engine", "env -i hokma engine", "timeout 5 hokma engine",
    "nohup python -m hokma_cli engine &", "setsid python -m hokma_cli.main engine", "xargs hokma engine", "echo x | xargs hokma start",
    "bash -c 'hokma engine'", "sh -c \"printf 'a\\n' | hokma engine --port 1 &\"", "bash -c \"nohup python -m hokma_cli engine &\"",
    "env X=1 sudo nohup hokma engine &", "cd /tmp && hokma engine", "true; hokma engine", "false || hokma start",
    "(hokma engine &)", "{ hokma engine; }", "exec hokma engine", "time hokma engine", "command hokma engine",
    "python /x/hokma/core/engine_main.py", "python engine_main.py", "python ./core/engine_main.py --port 1",
    "python -m hokma.core.engine_main", "python -m hokma.kit.trash_mcp", "python3 /x/hokma/kit/trash_mcp.py",
    "python trash_mcp.py", "python -m core.engine_main", "python -m trash_mcp", "./engine_main.py", "/x/kit/trash_mcp.py",
    "python -c 'import hokma_cli.main as m; m.main([\"engine\"])'", "python -c 'from hokma_cli.main import main'",
    "python -c 'import hokma_cli'", "python -c 'import engine_main'",
    "python -c 'import subprocess; subprocess.run([\"hokma\", \"engine\"])'",
    "python -c 'import subprocess; subprocess.Popen([\"hokma\", \"start\"])'",
    "python -c 'import os; os.system(\"hokma engine --port 1\")'",
    "python -c 'import subprocess; subprocess.run([\"hokma\", cmd])'",
    "python -c 'import subprocess; subprocess.run([\"hokma\", \"ext\", \"list\"])'",
    "node -e 'require(\"child_process\").execSync(\"hokma engine\")'",
    "echo hi | hokma engine", "hokma engine & disown", "cat x | nohup hokma restart &",
]


@pytest.mark.parametrize("command", BLOCKED)
def test_the_command_line_and_the_server_entries_are_real_money(box, command):
    assert box.bash(command) == "real_money"


@pytest.mark.parametrize("command", [
    "hokma engine", "hokma.exe start", "& hokma engine", "& 'C:\\x\\hokma.exe' start", "python -m hokma_cli engine",
    "Start-Process hokma -ArgumentList engine", "cmd /c hokma engine", "powershell -Command \"hokma engine\"",
    "hokma db migrate", "py -m hokma_cli update"])
def test_the_same_in_powershell(box, command):
    assert box.ps(command) == "real_money"


@pytest.mark.parametrize("command", [
    "hokma doctor", "hokma version", "hokma ext request plugin foo@bar", "hokma ext request mcp x", "hokma ext requests",
    "hokma --help", "hokma -h", "hokma --version", "hokma -V", "hokma", "python -m hokma_cli.main --version", "uv run hokma doctor",
    "uv run pytest -q", "HOKMA DOCTOR", "hokma.exe doctor", "/usr/local/bin/hokma doctor", "./hokma version",
    "python -m hokma_cli doctor", "python -m hokma_cli.main version", "python -m hokma_cli ext request plugin a@b",
    "python -m hokma doctor", "sudo hokma doctor", "env X=1 hokma version", "timeout 5 hokma doctor",
    "bash -c 'hokma doctor'", "hokma doctor | head -5", "hokma version && echo ok", "python /x/hokma doctor",
    "python -c 'import subprocess; subprocess.run([\"hokma\", \"doctor\"])'",
    "python -c 'import subprocess; subprocess.run([\"hokma\", \"ext\", \"requests\"])'",
    "python -c 'import os; os.system(\"hokma version\")'"])
def test_the_read_only_commands_stay_free(box, command):
    assert box.bash(command) is None


@pytest.mark.parametrize("command", [
    "./hokma_notes.sh", "bash hokma_notes.sh", "cat hokma_notes.md", "echo hokma engine", "printf 'hokma engine\\n'",
    "git status", "git commit -m 'run hokma engine later'", "node app.js", "python script.py", "python3 -m pytest",
    "python -c 'print(1)'", "ls", "grep -r engine .", "python engine.py", "python main.py engine", "python my_engine_main.py",
    "python -m http.server 8000", "echo trash_mcp is a word", "python hokma_tool.py engine", "ls engine_main.py.bak",
    "python -c 'import subprocess; subprocess.run([\"git\", \"status\"])'", "mkdir hokma_notes", "pip install hokma-extras"])
def test_things_that_are_not_the_cli_stay_free(box, command):
    (box.work / "hokma_notes.sh").write_text("echo notes\n")
    (box.work / "script.py").write_text("print(1)\n")
    assert box.bash(command) is None


def test_a_script_that_runs_the_cli_is_read_before_it_runs(box):
    (box.work / "go.sh").write_text("#!/bin/sh\nprintf 'tok\\n' | hokma engine --port 9 &\n")
    (box.work / "go.py").write_text("import subprocess\nsubprocess.Popen(['hokma', 'engine'])\n")
    (box.work / "ok.py").write_text("import subprocess\nsubprocess.run(['hokma', 'doctor'])\n")
    assert box.bash("sh go.sh") == "real_money" and box.bash("./go.sh") == "real_money"
    assert box.bash("python go.py") == "real_money"
    assert box.bash("python ok.py") is None


def test_writing_python_that_names_the_cli_is_real_money(box):
    assert box.hit("Write", file_path=str(box.work / "a.sh"), content="hokma engine --port 1 &\n") is None   # a shell file is judged when it runs
    assert box.hit("Write", file_path=str(box.work / "a.py"), content="import hokma_cli.main\n") == "real_money"
    assert box.hit("Write", file_path=str(box.work / "a.sh"), content="hokma doctor\n") is None
    assert box.hit("Write", file_path=str(box.work / "a.sh"), content="echo hokma_notes\n") is None


def test_the_broker_applies_the_same_rule(box, bctx):
    for command in ("hokma engine --port 1", "printf 'x\\n' | hokma engine &", "python -m hokma_cli engine",
                    "python engine_main.py", "hokma db migrate"):
        verdict = check("Bash", {"command": command}, cwd=str(box.work), ctx=bctx)
        assert (verdict.allowed, verdict.rule) == (False, "real_money"), command
    for command in ("hokma doctor", "hokma ext requests", "git status"):
        assert check("Bash", {"command": command}, cwd=str(box.work), ctx=bctx).allowed, command


def test_the_hook_process_blocks_the_engine_command(box):
    done = box.run_hook({"tool_name": "Bash", "tool_input": {"command": "hokma engine"}, "cwd": str(box.work)})
    assert done.returncode == 2
    assert box.run_hook({"tool_name": "Bash", "tool_input": {"command": "hokma doctor"}, "cwd": str(box.work)}).returncode == 0


# ------------------------------------------------------------------------------------- partial(getattr ...) and marshal

PARTIAL_BLOCKED = [
    "import functools\ng = functools.partial(getattr, obj)\n",
    "import functools\ng = functools.partial(getattr, obj, name_var)\n",
    "from functools import partial\ng = partial(getattr, obj)\n",
    "from functools import partial\ng = partial(getattr, obj, name_var)\n",
    "from functools import partial\ng = partial(getattr, obj, 'x', name_var, 1)\n",
    "from functools import partial\ng = partial(getattr, obj, 'import_module')\n",
    "import functools\ng = functools.partialmethod(getattr, 'x')\n",
    "from functools import partialmethod\ng = partialmethod(getattr, obj)\n",
    "from functools import partial as p\ng = p(getattr, obj)\n",
    "from functools import partial as p, reduce\ng = p(getattr, obj, n)\n",
    "import functools as ft\ng = ft.partial(getattr, obj)\n",
    "import functools\nmk = functools.partial\ng = mk(getattr, obj)\n",
    "from functools import partial\nmk = partial\ng = mk(getattr, obj, n)\n",
    "from functools import partial\ng = partial(\n    getattr,\n    obj)\n",
    "from functools import partial\nga = getattr\ng = partial(ga, obj, n)\n",
    "from functools import partial\nimport builtins\ng = partial(builtins.getattr, obj, n)\n",
    "from functools import partial\ng = partial(__builtins__.getattr, obj)\n",
    "from functools import partial\nimport builtins\nev = builtins.eval\ng = partial(ev, s)\n",
    "from functools import partial\nfrom builtins import getattr as ga\ng = partial(ga, obj, n)\n",
    "from functools import partial\nfrom importlib import import_module as im\ng = partial(im, n)\n",
    "from functools import partial\ng = partial(importlib.import_module, 'x')\n",
    "from functools import partial\ng = partial(*args)\n",
    "from functools import partial\ng = partial(func=getattr)\n",
    "from functools import partial\ng = partial(eval, s)\n", "from functools import partial\ng = partial(exec, s)\n",
]


@pytest.mark.parametrize("body", PARTIAL_BLOCKED)
def test_partial_of_getattr_on_built_arguments_is_real_money(box, body):
    assert box.hit("Write", file_path=str(box.work / "a.py"), content=body) == "real_money"
    (box.work / "b.py").write_text(body)
    assert box.bash("python b.py") == "real_money"
    assert box.bash("python -c " + __import__("shlex").quote(body)) == "real_money"
    assert box.bash("python <<'EOF'\n" + body + "EOF") == "real_money"


MARSHAL_BLOCKED = [
    "import marshal\ncode = marshal.loads(raw)\n", "import marshal\ncode = marshal.load(open('x.bin', 'rb'))\n",
    "from marshal import loads\ncode = loads(raw)\n", "from marshal import load as ld\nx = ld(f)\n",
    "import marshal as m\nx = m.loads(raw)\n", "import os, marshal\nx = marshal.loads(raw)\n",
    "x = __import__('marshal').loads(raw)\n", "import importlib\nm = importlib.import_module('marshal')\n",
    "import marshal\nexec(marshal.loads(open('c.bin','rb').read()))\n",
]


@pytest.mark.parametrize("body", MARSHAL_BLOCKED)
def test_marshal_is_always_real_money(box, body):
    assert box.hit("Write", file_path=str(box.work / "a.py"), content=body) == "real_money"
    (box.work / "b.py").write_text(body)
    assert box.bash("python b.py") == "real_money"
    assert box.bash("python -c " + __import__("shlex").quote(body)) == "real_money"
    assert box.bash("python <<'EOF'\n" + body + "EOF") == "real_money"


def test_partial_and_marshal_reach_the_broker(box, bctx):
    for code in ("from functools import partial\ng = partial(getattr, obj)", "import marshal\nmarshal.loads(b)",
                 "from marshal import loads"):
        verdict = check("mcp__other__run", {"code": code}, cwd=str(box.work), ctx=bctx)
        assert (verdict.allowed, verdict.rule) == (False, "real_money"), code
    for command in ("python -c 'from functools import partial; partial(getattr, o)'", "python -c 'import marshal'"):
        verdict = check("Bash", {"command": command}, cwd=str(box.work), ctx=bctx)
        assert (verdict.allowed, verdict.rule) == (False, "real_money"), command


@pytest.mark.parametrize("body", [
    "import pickle\nmodel = pickle.load(open('model.pkl', 'rb'))\n", "import joblib\nmodel = joblib.load('m.joblib')\n",
    "from functools import partial\ng = partial(getattr, obj, 'x')\n", "from functools import partial\ng = partial(print, 'a')\n",
    "from functools import partial\ng = partial(int, base=2)\n", "import functools\nf = functools.partial(max, key=len)\n",
    "from functools import partial as p\ng = p(round, ndigits=2)\n", "print('marshaling the data')\n",
    "# marshal is not used here\nx = 1\n", "import json\nx = json.loads(s)\n",
    "def partial(x):\n    return x\n", "partial_sum = sum(xs)\n"])
def test_the_students_own_models_and_plain_partials_stay_free(box, body):
    assert box.hit("Write", file_path=str(box.work / "a.py"), content=body) is None
    (box.work / "b.py").write_text(body)
    assert box.bash("python b.py") is None

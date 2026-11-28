"""Bug 1225 (SPEC-015 18.7), reduced to what is free in 1.3.1 (task 3436): the lock of `real_money` no longer reads the code an agent runs. Importing Hokma (any module), changing the
import path, building a name or code at run time and opening the base of Hokma are free; the cases that were refused in 1.3.0 are gone with their rules."""
import pytest
from hardblock_support import box  # noqa: F401  (fixture)


# ----------------------------------------------------------------------------------- 1: any import of hokma

FREE_IMPORTS = [
    "import hokma", "import hokma.market", "import hokma.factory.strategy", "from hokma.factory import orders", "from hokma import market", "import os, hokma", "import hokma as h",
    "from  hokma  import x", "import  hokma", "import HOKMA", "import hokma; hokma.run()", "from hokma.backtest import spec", "import hokma.quant.metrics",
]


def wrap_python(body: str) -> list[str]:
    quoted = body.replace("'", "'\"'\"'")
    return [f"python -c '{quoted}'", f"python3 -c '{quoted}'", f"py -c '{quoted}'", f"sudo python -c '{quoted}'",
            f"env X=1 python -c '{quoted}'", f"bash -c \"python -c '{body}'\"" if "'" not in body else f"python <<'EOF'\n{body}\nEOF",
            f"python <<'EOF'\n{body}\nEOF", f"python - <<EOF\n{body}\nEOF", f"echo '{body}' | python" if "'" not in body else
            f"python <<'EOF'\n{body}\nEOF"]


@pytest.mark.parametrize("body", FREE_IMPORTS)
def test_the_free_modules_of_hokma_can_be_imported_and_run(box, body):
    for command in wrap_python(body):
        assert box.bash(command) is None, command
    (box.work / "free.py").write_text(body + "\n")
    assert box.bash("python free.py") is None
    assert box.hit("Write", file_path=str(box.work / "free2.py"), content=body + "\n") is None


def test_a_strategy_can_run_its_tests_and_the_rule_check_with_the_runtime_interpreter(box):
    py = f"{box.data}/runtime/a/venv/bin/python"
    for command in ("python -m pytest -q versions/v1/test_strategy.py", "python -m hokma.factory.rule_check service/strategies/STR-001-x/versions/v1",
                    f"{py} -m pytest -q", f"{py} -m hokma.factory.rule_check x", "pytest -q", "python -m hokma.backtest.run x"):
        assert box.bash(command) is None, command


# ----------------------------------------------------------------------------------- 2: the import path


@pytest.mark.parametrize("command", [
    "PYTHONPATH=src python app.py", "PYTHONPATH=. pytest", "PYTHONPATH=$PWD/src pytest -q", "export PYTHONPATH=lib",
    "python -c \"import sys; sys.path.insert(0, 'src'); import app\"", "python -c \"import sys, os; sys.path.insert(0, os.getcwd())\"",
    "python -c \"import sys; sys.path.append('..')\"", "python -c \"import torch; torch.save(1, 'model.pth')\"",
    "ls venv", "cat requirements.txt", "git status", "node app.js", "python -m pytest tests", "python -m http.server 8000",
    "python manage.py runserver", "pip install -r requirements.txt",
])
def test_an_import_path_of_the_student_project_is_free(box, command):
    assert box.bash(command) is None, command


# ----------------------------------------------------------------------------------- 3: names built by parts


@pytest.mark.parametrize("body", [
    "import importlib\nm = importlib.import_module('json')", "import importlib\nimportlib.import_module(\"os.path\")",
    "m = __import__('json')", "import runpy\nrunpy.run_module('http.server')", "import importlib.util\nimportlib.util.find_spec('numpy')",
    "from importlib import import_module\nimport_module('json')", "import importlib.util\n"
    "s = importlib.util.spec_from_file_location('m', 'plugins/x.py')", "import re\np = re.compile('a+')", "import ast\nast.literal_eval('[1]')",
    "model.eval()", "import subprocess\nsubprocess.run(['python', '-c', 'print(1)'])", "import sys, subprocess\n"
    "subprocess.run([sys.executable, '-m', 'pytest', '-q'])", "cur.execute('select 1')", "print('exec and eval are words')\n",
    "def evaluate(x):\n    return x", "import importlib\nimportlib.reload(mod)", "x = 'importlib'", "exec('print(1)')", "eval('1 + 1')",
])
def test_plain_python_with_a_literal_import_or_no_dynamic_use_is_free(box, body):
    quoted = body.replace("'", "'\"'\"'")
    assert box.bash(f"python -c '{quoted}'") is None, body
    (box.work / "ok.py").write_text(body + "\n")
    assert box.bash("python ok.py") is None, body
    assert box.hit("Write", file_path=str(box.work / "ok2.py"), content=body) is None, body


# ----------------------------------------------------------------------------------- 4: the database of hokma


@pytest.mark.parametrize("command", [
    "sqlite3 app.db 'select 1'", "sqlite3 data/students.sqlite .tables", "python -c \"import sqlite3; sqlite3.connect('app.db')\"",
    "python -c \"import sqlite3; sqlite3.connect(':memory:')\"", "python -c \"open('notes.txt').read()\"",
    "psql -h localhost -d mydb -c 'select 1'", "pg_dump mydb > d.sql", "python -c \"open('hokma_notes.db')\"" if False else "ls *.db",
    "cat hokma.txt", "ls hokma-notes", "python -c \"import sqlite3; sqlite3.connect('local.db').execute('select 1')\"",
])
def test_a_database_of_the_student_is_free(box, command):
    assert box.bash(command) is None, command


# ----------------------------------------------------------------------------------- the hook, the broker and the tools


# ----------------------------------------------------------------------------------- what stays free

FREE = [
    "ls", "ls -la", "cat notes.md", "cat docs/readme.md", "git status", "git log --oneline", "git commit -m 'fix the loader'",
    "node app.js", "node -e 'console.log(1)'", "npm test", "pytest tests -q", "python -m pytest -q", "python -m pip list",
    "python app.py", "python3 -c 'print(1)'", "python -c 'import json, os, sys; print(os.getcwd())'", "python -c 'import schwab'",
    "python -c 'from schwab import auth'", "python -c 'import hokma_extras'", "python -c 'import brokerage_tools'",
    "python -m venv .venv", ".venv/bin/python app.py", "pip install requests", "grep -rn import_module docs",
    "grep -rn exec notes", "ls hokma-notes", "cat hokma.txt", "python -c \"import importlib; importlib.import_module('json')\"",
    "python -c \"import importlib; importlib.import_module('numpy')\"", "python -c \"print('hokma')\"",
    "echo hokma", "cat README.md", "mkdir -p out && ls out", "python -c \"import re; print(re.compile('a'))\"",
]


@pytest.mark.parametrize("command", FREE)
def test_everyday_commands_stay_free(box, command):
    assert box.bash(command) is None, command


def test_a_student_script_that_does_not_touch_hokma_runs(box):
    (box.work / "app.py").write_text("import json\nimport importlib\nmod = importlib.import_module('json')\n"
                                     "import sqlite3\nconn = sqlite3.connect('app.db')\nprint(mod, conn)\n")
    assert box.bash("python app.py") is None

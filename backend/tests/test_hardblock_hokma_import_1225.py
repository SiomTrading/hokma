"""Bug 1225 (SPEC-015 18.7, round 3), narrowed in 1.2.9 (task 3431): an agent never imports the modules of Hokma that touch real money, keys, the guard or the server (broker, schwab,
connections, exec, db, desks, kit, core...), never builds the name of a module or a piece of code at run time, never puts the runtime with its libraries on its import path and never
opens Hokma's database. All of it is `real_money`, in the hook and in the permission broker. The rest of the package (factory, backtest, quant, rule_check) and what does not touch Hokma stay free."""
import os
from pathlib import Path

import pytest
from hardblock_support import box  # noqa: F401  (fixture)

from hokma.kit import hardblock
from hokma.permissions.broker import BrokerCtx, check

PKG = Path(hardblock.__file__).resolve().parents[2]        # the folder that holds the package `hokma`


@pytest.fixture
def bctx(box):
    return BrokerCtx(data=str(box.data), roots=[str(box.work)], protect=[str(box.data)], conversation_id="7")


def sp(box) -> str:
    return f"{box.data}/runtime/a/venv/lib/python3.12/site-packages"


# ----------------------------------------------------------------------------------- 1: any import of hokma

FREE_IMPORTS = [
    "import hokma", "import hokma.market", "import hokma.factory.strategy", "from hokma.factory import orders", "from hokma import market", "import os, hokma", "import hokma as h",
    "from  hokma  import x", "import  hokma", "import HOKMA", "import hokma; hokma.run()", "from hokma.backtest import spec", "import hokma.quant.metrics",
]
IMPORTS = [
    "import hokma.core.config", "from hokma.db import paths", "import hokma.broker", "from hokma import exec", "from hokma.schwab import client",
    "from hokma import (market,\n    desks)", "import os, sys, hokma.db as d", "from hokma.kit import hardblock as hb", "import hokma.connections",
    "__import__('hokma')", "__import__(\"hokma.db\")", "importlib.import_module('hokma')", "import_module('hokma.market')",
    "importlib.import_module('hokma.' + 'market')", "runpy.run_module('hokma')", "importlib.util.find_spec('hokma')",
]


def wrap_python(body: str) -> list[str]:
    quoted = body.replace("'", "'\"'\"'")
    return [f"python -c '{quoted}'", f"python3 -c '{quoted}'", f"py -c '{quoted}'", f"sudo python -c '{quoted}'",
            f"env X=1 python -c '{quoted}'", f"bash -c \"python -c '{body}'\"" if "'" not in body else f"python <<'EOF'\n{body}\nEOF",
            f"python <<'EOF'\n{body}\nEOF", f"python - <<EOF\n{body}\nEOF", f"echo '{body}' | python" if "'" not in body else
            f"python <<'EOF'\n{body}\nEOF"]


@pytest.mark.parametrize("body", IMPORTS)
def test_any_import_of_hokma_is_real_money(box, body):
    for command in wrap_python(body):
        assert box.bash(command) == "real_money", command


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


def test_the_blocks_of_the_money_modules_have_their_own_text(box):
    for command in ("python -c 'import hokma.broker'", "python -m hokma.exec", f"{box.data}/runtime/a/venv/bin/python -c 'from hokma.schwab import x'"):
        done = box.run_hook({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(box.work)})
        assert done.returncode == 2 and "HOKMA_BLOCKED real_money" in done.stderr and "real money, keys or the server" in done.stderr, command


@pytest.mark.parametrize("body", IMPORTS)
def test_a_script_file_with_any_import_of_hokma_is_refused(box, body):
    (box.work / "tool.py").write_text(body + "\n")
    assert box.bash("python tool.py") == "real_money"
    assert box.bash("python3 ./tool.py") == "real_money"
    assert box.bash("python < tool.py") == "real_money"
    assert box.bash(f"bash -c 'python tool.py'") == "real_money"
    assert box.hit("Write", file_path=str(box.work / "again.py"), content=body + "\n") == "real_money"
    assert box.hit("Edit", file_path=str(box.work / "again.py"), new_string=body + "\n", old_string="x") == "real_money"


def test_the_owner_doctor_command_stays_free_and_nothing_else_of_hokma_is(box):
    assert box.bash("python -m hokma doctor") is None
    assert box.bash("python3 -m hokma doctor") is None
    assert box.bash("python -m hokma.market") is None
    for command in ("python -m hokma", "python -m hokma serve", "python -m hokma.db", "python -m hokma.core.config",
                    "python -m runpy hokma", "python -m pydoc hokma", "python -m hokma.broker"):
        assert box.bash(command) == "real_money", command


def test_the_runtime_interpreter_runs_what_is_free_and_refuses_what_is_not(box):
    assert box.bash(f"{box.data}/runtime/a/venv/bin/python -m hokma doctor") is None
    assert box.bash(f"{box.data}/runtime/a/venv/bin/python -m hokma serve") == "real_money"


# ----------------------------------------------------------------------------------- 2: the import path

INJECTIONS = [
    "import sys; sys.path.insert(0, '{sp}')", "import sys; sys.path.append('{sp}')", "import sys; sys.path.extend(['{sp}'])",
    "import sys; sys.path = ['{sp}'] + sys.path", "import site; site.addsitedir('{sp}')",
    "import sys; sys.path.insert(0, '{data}/runtime/a')",
    "import sys; p = '{sp}'; sys.path.insert(0, p)", "import sys, os; sys.path.insert(0, os.path.join('{data}', 'runtime', 'a', 'venv'))",
    "import sys; sys.path.insert(0, '{sp}'[:-1] + 's')",
]


@pytest.mark.parametrize("body", INJECTIONS)
def test_putting_the_site_packages_or_the_package_folder_on_the_path_is_real_money(box, body):
    body = body.format(sp=sp(box), pkg=PKG, data=box.data)
    quoted = body.replace("'", "'\"'\"'")
    for command in (f"python -c '{quoted}'", f"python3 -c '{quoted}'", f"python <<'EOF'\n{body}\nEOF"):
        assert box.bash(command) == "real_money", command
    (box.work / "p.py").write_text(body + "\nimport helper\n")
    assert box.bash("python p.py") == "real_money"
    assert box.hit("Write", file_path=str(box.work / "q.py"), content=body + "\n") == "real_money"


@pytest.mark.parametrize("command", [
    "PYTHONPATH={sp} python -c 'print(1)'", "PYTHONPATH={sp}:. python3 x.py", "export PYTHONPATH={sp}",
    "env PYTHONPATH={sp} python x.py", "export PYTHONPATH=\"{data}/runtime/a\"", "bash -c 'PYTHONPATH={sp} python x.py'",
    "PYTHONPATH={sp} pytest", "cd {pkg} && python -c 'print(1)'", "cd {pkg} && python3 x.py",
    "cd {data}/runtime/a && python x.py", "cd {sp} && python x.py",
    "python {pkg}/hokma/__main__.py", "python -c \"import os; os.system('PYTHONPATH={sp} python x.py')\"",
    "echo {sp} > $(python -c 'import site;print(site.getsitepackages()[0])')/h.pth",
    "echo {sp} > x.pth", "echo '{pkg}' >> {sp}/extra.pth"])
def test_pythonpath_and_python_from_the_package_folder_are_real_money(box, command):
    command = command.format(sp=sp(box), pkg=PKG, data=box.data)
    assert box.bash(command) == "real_money", command


def test_python_with_the_package_folder_as_the_current_folder_is_real_money(box):
    assert box.bash("python -c 'print(1)'", cwd=PKG) == "real_money"
    assert box.bash("python3 x.py", cwd=PKG / "hokma") == "real_money"
    assert box.bash("python -m json.tool x.json", cwd=PKG / "hokma" / "kit") == "real_money"
    assert box.bash("ls", cwd=PKG) != "real_money"
    runtime = box.data / "runtime" / "a"
    runtime.mkdir(parents=True)
    assert box.bash("python x.py", cwd=runtime) == "real_money"


def test_the_package_folder_through_a_relative_path_or_a_link_is_real_money(box):
    link = box.work / "pk"
    os.symlink(PKG, link)
    assert box.bash("cd pk && python x.py") == "real_money"
    assert box.bash("python pk/hokma/__main__.py") == "real_money"


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

BUILT = [
    "importlib.import_module('ho' + 'kma.broker')", "importlib.import_module('hok' + 'ma')", "importlib.import_module(''.join(['ho', 'kma']))",
    "importlib.import_module('{}kma'.format('ho'))", "importlib.import_module('%skma' % 'ho')", "importlib.import_module(f'ho{x}')",
    "importlib.import_module(chr(104) + chr(111) + 'kma')", "importlib.import_module(bytes([104, 111]).decode() + 'kma')",
    "importlib.import_module(bytearray(b'hokma').decode())", "importlib.import_module(base64.b64decode('aG9rbWE=').decode())",
    "importlib.import_module(codecs.decode('ubxzn', 'rot13'))", "importlib.import_module(binascii.unhexlify('686f6b6d61').decode())",
    "importlib.import_module(name)", "importlib.import_module(sys.argv[1])", "importlib.import_module('.broker', package=pkg)",
    "importlib.import_module('.broker', 'hokma')", "__import__('ho' + 'kma')", "__import__(''.join(parts))", "__import__(n)",
    "__import__('ho\\x6bma')", "import_module('hok' 'ma')", "import_module(\"\"\"hokma\"\"\")", "runpy.run_module(m)",
    "runpy.run_module('ho' + 'kma')", "runpy.run_path(p)", "importlib.util.find_spec('ho' + 'kma')", "importlib.util.find_spec(n)",
    "importlib.util.spec_from_file_location('m', p)", "importlib.util.spec_from_file_location('m', base + '/x.py')",
    "importlib.util.spec_from_file_location('m', '/x/site-packages/hokma/__init__.py')",
    "importlib.machinery.SourceFileLoader('m', p).load_module()", "imp.load_source('m', p)",
    "f = importlib.import_module; f(n)", "f = __import__; f('x')", "from importlib import import_module as im; im(n)",
    "from importlib import *", "getattr(importlib, 'import_' + 'module')('x')", "getattr(__builtins__, '__imp' + 'ort__')('x')",
    "__builtins__.__dict__['__import__']('x')", "builtins.__import__(n)", "getattr(builtins, n)(m)",
    "exec('im' + 'port hokma')", "exec(''.join(parts))", "exec(code)", "exec(base64.b64decode(s))", "exec(codecs.decode(s, 'rot13'))",
    "exec(bytes(b).decode())", "exec(f'import {x}')", "exec(compile(src, 'x', 'exec'))", "eval('__imp' + 'ort__(1)')", "eval(s)",
    "eval(chr(1) + chr(2))", "eval(binascii.unhexlify(h))", "compile(src, 'f', 'exec')", "compile(''.join(a), 'f', 'exec')",
    "subprocess.run([sys.executable, '-c', code])", "subprocess.run(['python', '-c', 'im' + 'port ho' + 'kma'])",
    "os.system('python -c ' + code)", "os.system(f'python3 -c \"{code}\"')", "subprocess.run([sys.executable, '-m', mod])",
]


@pytest.mark.parametrize("body", BUILT)
def test_a_name_or_code_built_at_run_time_is_real_money(box, body):
    code = "import importlib, runpy, sys, os, base64, codecs, binascii, subprocess\n" + body
    quoted = code.replace("'", "'\"'\"'")
    assert box.bash(f"python -c '{quoted}'") == "real_money", body
    assert box.bash(f"python3 <<'EOF'\n{code}\nEOF") == "real_money", body
    assert box.bash(f"echo '{quoted}' | python") == "real_money", body
    (box.work / "b.py").write_text(code + "\n")
    assert box.bash("python b.py") == "real_money", body
    assert box.bash("bash -c 'python b.py'") == "real_money", body
    assert box.hit("Write", file_path=str(box.work / "w.py"), content=code) == "real_money", body
    assert box.hit("Write", file_path=str(box.work / "w"), content=code) == "real_money", body


def test_a_script_that_writes_python_with_a_built_import_is_refused_when_written(box):
    assert box.bash("cat > x.py <<'EOF'\nimport importlib\nimportlib.import_module(n)\nEOF") == "real_money"
    assert box.bash("printf 'exec(s)' > y.py") == "real_money"
    assert box.bash("echo 'exec(s)' | tee z.py") == "real_money"


def test_python_code_built_by_the_shell_is_real_money(box):
    assert box.bash('python -c "$(echo aW1wb3J0IG9z | base64 -d)"') == "real_money"
    assert box.bash('python -c "$CODE"') == "real_money"
    assert box.bash("python -c \"\x60cat payload\x60\"") == "real_money"
    assert box.bash("echo aW1wb3J0IGhva21h | base64 -d | python") is not None


def test_the_built_name_rule_in_a_notebook_cell(box):
    assert box.hit("NotebookEdit", notebook_path=str(box.work / "n.ipynb"), new_source="importlib.import_module(n)") == "real_money"
    assert box.hit("NotebookEdit", notebook_path=str(box.work / "n.ipynb"), new_source="print(1)") is None
    nb = '{"cells": [{"source": ["exec(s)\\n"]}]}'
    assert box.hit("Write", file_path=str(box.work / "n.ipynb"), content=nb) == "real_money"
    ok = '{"cells": [{"source": ["import importlib\\n", "importlib.import_module(\\"json\\")\\n"]}]}'
    assert box.hit("Write", file_path=str(box.work / "n.ipynb"), content=ok) is None


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


def test_the_same_rule_applies_in_powershell(box):
    assert box.ps("python -c \"import hokma.broker\"") == "real_money"
    assert box.ps("python -c \"import hokma.factory.strategy\"") is None
    assert box.ps("python -c \"import importlib; importlib.import_module(n)\"") == "real_money"
    assert box.ps("$env:PYTHONPATH = '" + sp(box) + "'; python x.py") == "real_money"
    assert box.ps("python -c \"print(1)\"") is None


# ----------------------------------------------------------------------------------- 4: the database of hokma

def db(box) -> str:
    return f"{box.data}/hokma.db"


DB_COMMANDS = [
    "sqlite3 {db} 'select 1'", "sqlite3 {db} \".dump\"", "sqlite3 -readonly {db} .tables", "sqlite3 {db}",
    "sudo sqlite3 {db} 'delete from desks'", "env X=1 sqlite3 {db} .schema", "bash -c \"sqlite3 {db} .tables\"",
    "python -c \"import sqlite3; sqlite3.connect('{db}')\"", "python3 -c \"import sqlite3; sqlite3.connect(r'{db}').execute('x')\"",
    "python -c \"open('{db}', 'rb').read()\"", "python -c \"open('{db}', 'wb').write(b'')\"", "python -c \"import pathlib; pathlib.Path('{db}').read_bytes()\"",
    "python -c \"import shutil; shutil.copy('{db}', 'x')\"", "python -c \"import sqlite3; sqlite3.connect('file:{db}?mode=ro', uri=True)\"",
    "python -c \"import sqlite3, os; sqlite3.connect(os.path.join(os.environ['HOKMA_DATA'], 'hokma.db'))\"",
    "python -c \"import sqlite3, os; sqlite3.connect(os.environ['HOKMA_DATA'] + '/hok' + 'ma.db')\"",
    "python -c \"import sqlite3, os; sqlite3.connect(os.path.expanduser('~/.local/share/x/' + n))\"",
    "python -c \"import os; open(os.path.join(os.environ['HOKMA_DATA'], 'hokma' + '.db'), 'rb')\"",
    "cp {db} copy.db", "cp x.db {db}", "mv x.db {db}", "echo x > {db}", "echo x >> {db}", "dd if=/dev/zero of={db}", "truncate -s 0 {db}",
    "sed -i s/a/b/ {db}", "tee {db}", "ln -s {db} d", "rsync {db} out/", "litecli {db}", "duckdb {db}",
    "psql -d hokma -c 'select 1'", "psql postgresql://u@h/hokma", "pg_dump hokma > d.sql", "psql $(cat {data}/database.json)",
    "sqlite3 {data}/other.db .tables", "python -c \"import sqlite3; sqlite3.connect('{data}/x.db')\"",
    "sqlite3 {db}-wal .tables", "cp {db}-shm out",
]


@pytest.mark.parametrize("command", DB_COMMANDS)
def test_opening_or_writing_the_database_of_hokma_is_real_money(box, command):
    command = command.format(db=db(box), data=box.data)
    assert box.bash(command) == "real_money", command


def test_the_database_by_a_script_or_a_tool_call_is_real_money(box):
    body = f"import sqlite3\nc = sqlite3.connect('{db(box)}')\nc.execute('delete from desks')\n"
    (box.work / "d.py").write_text(body)
    assert box.bash("python d.py") == "real_money"
    assert box.hit("Write", file_path=str(box.work / "e.py"), content=body) == "real_money"
    assert box.bash(f"cat > e.py <<'EOF'\n{body}EOF") == "real_money"
    body2 = f"with open('{db(box)}', 'rb') as f:\n    data = f.read()\n"
    assert box.hit("Write", file_path=str(box.work / "e.py"), content=body2) == "real_money"


def test_the_database_of_hokma_through_the_broker_and_the_hook(box, bctx):
    for command in ("sqlite3 " + db(box) + " .tables", "python -c \"open('" + db(box) + "', 'rb')\""):
        done = box.run_hook({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(box.work)})
        assert done.returncode == 2 and "HOKMA_BLOCKED real_money" in done.stderr, command
        verdict = check("Bash", {"command": command}, cwd=str(box.work), ctx=bctx)
        assert (verdict.allowed, verdict.rule) == (False, "real_money"), command


@pytest.mark.parametrize("command", [
    "sqlite3 app.db 'select 1'", "sqlite3 data/students.sqlite .tables", "python -c \"import sqlite3; sqlite3.connect('app.db')\"",
    "python -c \"import sqlite3; sqlite3.connect(':memory:')\"", "python -c \"open('notes.txt').read()\"",
    "psql -h localhost -d mydb -c 'select 1'", "pg_dump mydb > d.sql", "python -c \"open('hokma_notes.db')\"" if False else "ls *.db",
    "cat hokma.txt", "ls hokma-notes", "python -c \"import sqlite3; sqlite3.connect('local.db').execute('select 1')\"",
])
def test_a_database_of_the_student_is_free(box, command):
    assert box.bash(command) is None, command


# ----------------------------------------------------------------------------------- the hook, the broker and the tools

CASES = [
    ["python", "-c", "import hokma"],
    "python -c \"import importlib; importlib.import_module('ho' + 'kma')\"",
    "PYTHONPATH={sp} python -c 'print(1)'",
    "sqlite3 {db} 'select 1'",
]


@pytest.mark.parametrize("command", CASES[1:])
def test_the_four_cases_are_real_money_in_the_hook_process_and_in_the_broker(box, bctx, command):
    command = command.format(sp=sp(box), db=db(box))
    done = box.run_hook({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(box.work)})
    assert done.returncode == 2 and "HOKMA_BLOCKED real_money" in done.stderr, command
    verdict = check("Bash", {"command": command}, cwd=str(box.work), ctx=bctx)
    assert (verdict.allowed, verdict.rule) == (False, "real_money"), command
    verdict = check("mcp__other__run", {"command": command}, cwd=str(box.work), ctx=bctx)
    assert (verdict.allowed, verdict.rule) == (False, "real_money"), command
    assert check("run_command", {"command": command}, cwd=str(box.work), ctx=bctx).rule == "real_money"


def test_a_plain_import_in_the_broker_and_the_tools(box, bctx):
    command = 'python -c "import hokma.broker"'
    done = box.run_hook({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(box.work)})
    assert done.returncode == 2 and "HOKMA_BLOCKED real_money" in done.stderr
    for tool, arguments in (("Bash", {"command": command}), ("Write", {"file_path": str(box.work / "x.py"), "content": "import hokma.broker\n"}),
                            ("write_file", {"file_path": str(box.work / "x.py"), "content": "from hokma import db\n"}),
                            ("mcp__other__run", {"code": "importlib.import_module(n)"}),
                            ("mcp__other__run", {"script": "from hokma import exec"}),
                            ("mcp__other__run", {"command": f"sqlite3 {db(box)} .tables"}),
                            ("web_fetch", {"url": "https://example.com/?c=import hokma.broker"}),
                            ("web_fetch", {"url": "https://example.com/?m=hokma.broker"})):
        verdict = check(tool, arguments, cwd=str(box.work), ctx=bctx)
        assert (verdict.allowed, verdict.rule) == (False, "real_money"), (tool, arguments)
    for tool, arguments in (("Bash", {"command": "python -m hokma doctor"}), ("Bash", {"command": "git status"}),
                            ("Write", {"file_path": str(box.work / "x.py"), "content": "import json\nprint(1)\n"}),
                            ("mcp__other__run", {"command": "node app.js"}), ("web_fetch", {"url": "https://example.com/docs"})):
        verdict = check(tool, arguments, cwd=str(box.work), ctx=bctx)
        assert verdict.allowed, (tool, arguments)


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

"""Task 3436 (Hokma 1.3.1): what the lock refused by doubt and is free now. A normal day of an agent in Hokma: looking for the C# compiler in the system folders,
importing the code of Hokma, reading its base, loading a file by its path. It fails with the lock of 1.3.0 and passes with the one of 1.3.1."""
import pytest
from hardblock_support import box  # noqa: F401  (fixture)

SYSTEM_SEARCHES = [
    "ls C:\\Windows\\Microsoft.NET\\Framework64\\",
    "grep -r csc.exe 'C:\\Windows\\Microsoft.NET\\Framework64'",
    "grep -rl csc /usr/lib/dotnet",
    "grep -r csc $WINDIR/Microsoft.NET/Framework64",
    "grep -r csc \"$SYSTEMROOT/Microsoft.NET\" /usr/share/dotnet",
    "ls /usr/lib/dotnet/sdk/{a,b}",
    "cat $DOTNET_ROOT/sdk/version.txt",
    "ls ~root/Microsoft.NET",
    "find /usr/share -name 'csc*' -exec ls {} ;",
    "ls 'C:\\Program Files\\dotnet\\sdk'",
]
SYSTEM_SEARCHES_PS = [
    "Get-ChildItem C:\\Windows\\Microsoft.NET\\Framework64\\ -Recurse -Filter csc.exe",
    "Get-ChildItem $env:SystemRoot\\Microsoft.NET\\Framework64 -Recurse -Filter csc.exe",
    "Select-String -Path \"$env:windir\\Microsoft.NET\\Framework64\\*\\*.targets\" -Pattern csc -Recurse",
    "Get-ChildItem \"${env:ProgramFiles}\\dotnet\" -Recurse -Filter dotnet.exe",
    "Test-Path (Join-Path $env:windir 'Microsoft.NET')",
]


@pytest.mark.parametrize("command", SYSTEM_SEARCHES)
def test_the_system_folders_and_the_paths_built_at_run_time_are_free(box, command):
    assert box.bash(command) is None, command


@pytest.mark.parametrize("command", SYSTEM_SEARCHES_PS)
def test_the_same_in_powershell(box, command):
    assert box.ps(command) is None, command


def test_the_search_of_the_compiler_from_inside_the_data_folder_is_free(box):
    for command in ("ls $WINDIR/Microsoft.NET/Framework64", "grep -r csc /usr/lib/dotnet", "cat $DOTNET_ROOT/x ~root/y /usr/share/{a,b}"):
        assert box.bash(command, cwd=box.data) is None, command
    assert box.ps("Get-ChildItem $env:SystemRoot\\Microsoft.NET -Recurse -Filter csc.exe", cwd=box.data) is None


def test_the_tools_of_reading_over_system_folders_are_free(box):
    assert box.hit("Read", file_path="/usr/lib/os-release") is None
    assert box.hit("Glob", pattern="/usr/lib/dotnet/sdk/*/csc.dll") is None
    assert box.hit("Grep", pattern="csc", path="/usr/lib/dotnet") is None


IMPORT_PATH = [
    "python -c \"import sys; sys.path.insert(0, '/x/runtime/py312/venv/lib/python3.12/site-packages'); import json\"",
    "python -c \"import sys; sys.path.append(r'C:\\\\Users\\\\a\\\\AppData\\\\Local\\\\hokma\\\\runtime\\\\py\\\\venv\\\\Lib\\\\site-packages')\"",
    "PYTHONPATH=/home/a/hokma/backend python tool.py",
    "python -c \"import site; site.addsitedir('/x/venv/lib/site-packages')\"",
    "python -c \"import os, sys; sys.path.insert(0, os.path.join(os.getcwd(), 'src'))\"",
]
HOKMA_DATABASE = [
    "sqlite3 {data}/hokma.db 'select * from strategies'",
    "python -c \"import sqlite3; print(sqlite3.connect('{data}/hokma.db').execute('select 1').fetchall())\"",
    "python -c \"import sqlite3, os; sqlite3.connect(os.path.join(os.environ['HOKMA_DATA_DIR'], 'hokma.db'))\"",
    "python -c \"import pandas as pd, sqlite3; pd.read_sql('select 1', sqlite3.connect('{data}/hokma.db'))\"",
    "cat {data}/hokma.db",
]
IMPORTS_OF_HOKMA = [
    "import hokma", "import hokma.factory", "from hokma import backtest", "from hokma.quant import metrics", "import hokma.db", "from hokma.db import get_database",
    "import hokma.executor_notes", "from hokma import core", "from hokma.core import app", "import hokma.engines",
]
BUILT_CODE = [
    "import importlib.util as u, os; s = u.spec_from_file_location('m', os.path.join(os.getcwd(), 'x.py')); m = u.module_from_spec(s); s.loader.exec_module(m)",
    "import importlib.util; s = importlib.util.spec_from_file_location('m', 'x.py')",
    "import runpy; runpy.run_path('tool.py')",
    "import importlib; importlib.import_module(name)",
    "import importlib; importlib.import_module('json')",
    "name = 'js' + 'on'; __import__(name)",
    "exec(open('tool.py').read())",
    "import os; os.chdir(some_folder)",
    "import pickle; pickle.load(open('model.pkl', 'rb'))",
    "from functools import partial; f = partial(getattr, obj)",
]


@pytest.mark.parametrize("command", IMPORT_PATH)
def test_changing_the_import_path_is_free(box, command):
    assert box.bash(command) is None, command


@pytest.mark.parametrize("command", HOKMA_DATABASE)
def test_the_base_of_hokma_is_free_to_read(box, command):
    assert box.bash(command.format(data=box.data)) is None, command


@pytest.mark.parametrize("body", IMPORTS_OF_HOKMA)
def test_importing_the_code_of_hokma_is_free(box, body):
    quoted = body.replace("'", "'\"'\"'")
    assert box.bash(f"python -c '{quoted}'") is None
    assert box.bash(f"python <<'EOF'\n{body}\nEOF") is None
    (box.work / "m.py").write_text(body + "\n")
    assert box.bash("python m.py") is None
    assert box.hit("Write", file_path=str(box.work / "w.py"), content=body + "\n") is None


@pytest.mark.parametrize("body", BUILT_CODE)
def test_code_that_loads_a_file_by_its_path_or_builds_a_name_is_free(box, body):
    quoted = body.replace("'", "'\"'\"'")
    assert box.bash(f"python -c '{quoted}'") is None, body
    assert box.hit("Write", file_path=str(box.work / "w.py"), content=body + "\n") is None


def test_the_runtime_of_hokma_and_its_command_line_are_free(box):
    runtime = box.data / "runtime" / "py312" / "venv" / "bin"
    for command in (f"{runtime}/python -m pytest tests", f"{runtime}/python script.py", "python -m hokma doctor", "python -m hokma.factory", "hokma doctor"):
        assert box.bash(command) is None, command


def test_writing_outside_the_working_folder_is_free(box):
    for command in ("echo x > /etc/hosts-x", "touch /opt/x", "mkdir /var/x", "cp f /usr/local/x", f"echo x > {box.data}/plugins-x"):
        assert box.bash(command) is None, command
    assert box.hit("Write", file_path="/opt/x/file.txt", content="x") is None

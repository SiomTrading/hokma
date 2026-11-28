"""Task 3436 (Hokma 1.3.1): what the lock refused by doubt and is free now. A normal day of an agent in Hokma: looking for the C# compiler in the system folders,
importing the code of Hokma, reading its base, loading a file by its path. It fails with the lock of 1.3.0 and passes with the one of 1.3.1."""
import ntpath
import os
import types

import pytest
from hardblock_support import box  # noqa: F401  (fixture)

from hokma.kit import guard, hardblock

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


# ---- task 3437 (Hokma 1.3.2): /dev/null and the other null devices are free destinations on every system, Windows included.
# On Windows `normpath('/dev/null')` is `\dev\null` (rooted, no drive): comparing it with a protected root that has a drive raised ValueError and the comparator
# failed closed, so `cmd > /dev/null` was refused as hokma_internal. The tests run the same code with the path rules of Windows.

class _WinPath(types.SimpleNamespace):
    pass


def _windows(monkeypatch):
    """The modules of the hook see Windows: `os.name`, ntpath, and a realpath that puts the current drive (C:) on a rooted path without one."""
    def realpath(p, **kw):
        p = ntpath.normpath(p)
        return p if ntpath.splitdrive(p)[0] else "C:" + p

    path = _WinPath(**{k: getattr(ntpath, k) for k in dir(ntpath) if not k.startswith("__")})
    path.realpath = realpath
    path.exists = lambda p: False
    fake = types.SimpleNamespace(**{k: getattr(os, k) for k in dir(os) if not k.startswith("__")})
    fake.name, fake.path, fake.sep = "nt", path, "\\"
    fake.stat = lambda p, *a, **k: (_ for _ in ()).throw(OSError("no file"))
    monkeypatch.setattr(hardblock, "os", fake)
    monkeypatch.setattr(guard, "os", fake)
    monkeypatch.setattr(guard, "CASE_INSENSITIVE", True)


DATA_WIN = "C:\\Users\\a\\AppData\\Local\\hokma"
WORK_WIN = "C:\\Users\\a\\Hokma"
PROTECT_WIN = [DATA_WIN + "\\runtime", DATA_WIN + "\\python", DATA_WIN + "\\bin", DATA_WIN + "\\tools", DATA_WIN + "\\guard",
               DATA_WIN + "\\claude-config\\settings.json", "C:\\Program Files\\hokma\\hokma"]


def _win_ctx(cwd=WORK_WIN):
    return hardblock.Ctx(DATA_WIN, [WORK_WIN], PROTECT_WIN, home="C:\\Users\\a", cwd=cwd, brokers={"hosts": [], "url_patterns": []})


def _win(command, mode="bash", tool="Bash", cwd=WORK_WIN):
    block = hardblock.evaluate({"tool_name": tool, "tool_input": {"command": command}, "cwd": cwd}, _win_ctx(cwd))
    return block.rule if block else None


NULL_BASH = [
    "ls missing >/dev/null", "ls missing > /dev/null", "ls missing 2>/dev/null", "ls missing 2> /dev/null", "ls missing &>/dev/null", "ls missing &> /dev/null",
    "ls missing >>/dev/null", "ls missing >/dev/null 2>&1", "ls missing 1>/dev/null 2>/dev/null", "ls missing >&/dev/null", "echo x | tee /dev/null",
    "echo x > /dev/stdout", "echo x >/dev/stderr", "echo x 1>&2 2>/dev/fd/2", "echo x > /dev/fd/1", "echo x >NUL", "echo x > NUL", "echo x 2>NUL", "echo x > nul",
    "echo x > 'NUL'", "echo x > \\\\.\\NUL", "echo x > '\\\\.\\NUL'", "echo x > //./nul", "dir >nul 2>&1", "git status 2>/dev/null && echo ok", "cat f > /dev/null",
    "python script.py > /dev/null 2>&1 &", "cmd /c 'dir > NUL'",
]
NULL_PS = [
    "Get-ChildItem | Out-Null", "Get-ChildItem > $null", "Get-ChildItem >$null", "Get-ChildItem 2>$null", "Get-ChildItem *> $null", "Get-ChildItem 2>&1 > $null",
    "Get-ChildItem | Out-File $null", "Get-ChildItem | Out-File -FilePath NUL", "Get-ChildItem | Out-File NUL", "Get-ChildItem > NUL", "git status 2>NUL",
    "Get-ChildItem > \\\\.\\NUL", "[void](Get-ChildItem)", "$null = Get-ChildItem", "Get-ChildItem | Set-Content $null", "Get-ChildItem | Out-File /dev/null",
]


@pytest.mark.parametrize("command", NULL_BASH)
def test_the_null_devices_are_free_in_a_redirection(box, command):
    assert box.bash(command) is None, command


@pytest.mark.parametrize("command", NULL_PS)
def test_the_null_devices_are_free_in_powershell(box, command):
    assert box.ps(command) is None, command


@pytest.mark.parametrize("command", NULL_BASH)
def test_the_null_devices_are_free_on_windows(monkeypatch, command):
    _windows(monkeypatch)
    assert _win(command) is None, command


@pytest.mark.parametrize("command", NULL_PS)
def test_the_null_devices_are_free_in_powershell_on_windows(monkeypatch, command):
    _windows(monkeypatch)
    assert _win(command, tool="PowerShell") is None, command


@pytest.mark.parametrize("cwd", [DATA_WIN, DATA_WIN + "\\bin", "/c/Users/a/AppData/Local/hokma", "C:\\", "\\\\server\\share", "/c/Users/a/Hokma"])
def test_the_null_devices_are_free_from_any_folder_on_windows(monkeypatch, cwd):
    _windows(monkeypatch)
    for command in ("ls x >/dev/null", "ls x 2>/dev/null", "ls x &>/dev/null", "echo x >NUL"):
        assert _win(command, cwd=cwd) is None, (command, cwd)
    assert _win("Get-ChildItem > $null", tool="PowerShell", cwd=cwd) is None


def test_the_null_device_is_free_for_the_file_tools_on_windows(monkeypatch):
    _windows(monkeypatch)
    assert not guard.under_any("/dev/null", PROTECT_WIN)
    assert not guard.under_any("\\\\.\\NUL", PROTECT_WIN)
    assert not guard.under_any("/tmp/x", PROTECT_WIN)


@pytest.mark.parametrize("command", ["echo x > out.txt", "echo x > /tmp/out.txt", "echo x > ./sub/out.txt", "cp a b", "echo x > /c/Users/a/Hokma/out.txt", "ls > informes.txt"])
@pytest.mark.parametrize("cwd", [WORK_WIN, "/c/Users/a/Hokma", DATA_WIN, "\\Users\\a\\Hokma"])
def test_a_normal_file_in_the_working_folder_is_free_on_windows_whatever_the_cwd_looks_like(monkeypatch, command, cwd):
    _windows(monkeypatch)
    assert _win(command, cwd=cwd) is None, (command, cwd)


@pytest.mark.parametrize("command", [
    "echo x > C:\\Users\\a\\AppData\\Local\\hokma\\guard\\x", "echo x > /c/Users/a/AppData/Local/hokma/guard/x", "echo x > /cygdrive/c/Users/a/AppData/Local/hokma/bin/x",
    "echo x > 'C:\\Program Files\\hokma\\hokma\\kit\\guard.py'", "echo x > /c/Program\\ Files/hokma/hokma/kit/guard.py", "tee /c/Users/a/AppData/Local/hokma/tools/x",
])
def test_the_protected_files_stay_protected_on_windows_with_paths_the_posix_way(monkeypatch, command):
    _windows(monkeypatch)
    assert _win(command) == "hokma_internal", command


def test_the_protected_files_stay_protected_from_a_folder_spelled_the_posix_way(monkeypatch):
    _windows(monkeypatch)
    assert _win("echo x > x", cwd="/c/Users/a/AppData/Local/hokma/guard") == "hokma_internal"
    assert _win("echo x > guard.py", cwd="C:\\Users\\a\\AppData\\Local\\hokma\\bin") == "hokma_internal"


# ---- task 3437: the code folder of NinjaTrader read from Git Bash (`/c/Users/...`, spaces in the name) is no store of keys.
NT_FOLDER = "/c/Users/crist/Documents/NinjaTrader 8/bin/Custom/Indicators/SiomTradingIndicators"
NT_READS = [
    f"ls '{NT_FOLDER}'", f"ls \"{NT_FOLDER}\"", f"cat '{NT_FOLDER}/STISupplyandDemandX.cs'", f"grep -n Supply '{NT_FOLDER}/STISupplyandDemandX.cs'",
    f"grep -rn Supply '{NT_FOLDER}'", f"head -50 {NT_FOLDER.replace(' ', chr(92) + ' ')}/STISupplyandDemandX.cs", f"cd '{NT_FOLDER}' && ls", f"find '{NT_FOLDER}' -name '*.cs'",
    "ls 'C:\\Users\\crist\\Documents\\NinjaTrader 8\\bin\\Custom\\Indicators\\SiomTradingIndicators'",
]


@pytest.mark.parametrize("command", NT_READS)
@pytest.mark.parametrize("cwd", [WORK_WIN, DATA_WIN, "/c/Users/crist/Hokma"])
def test_the_code_folder_of_ninjatrader_is_free_to_read_on_windows(monkeypatch, command, cwd):
    _windows(monkeypatch)
    assert _win(command, cwd=cwd) is None, (command, cwd)


def test_the_code_folder_of_ninjatrader_is_free_to_read_with_the_tools_on_windows(monkeypatch):
    _windows(monkeypatch)
    for tool, args in (("Read", {"file_path": "C:\\Users\\crist\\Documents\\NinjaTrader 8\\bin\\Custom\\Indicators\\SiomTradingIndicators\\STISupplyandDemandX.cs"}),
                       ("Glob", {"pattern": "C:\\Users\\crist\\Documents\\NinjaTrader 8\\bin\\Custom\\Indicators\\*.cs"}),
                       ("Grep", {"pattern": "Supply", "path": "/c/Users/crist/Documents/NinjaTrader 8/bin/Custom/Indicators/SiomTradingIndicators"})):
        block = hardblock.evaluate({"tool_name": tool, "tool_input": args, "cwd": WORK_WIN}, _win_ctx())
        assert block is None, (tool, block and block.rule)


@pytest.mark.parametrize("command", [
    "cat /c/Users/a/AppData/Local/hokma/database.json", "cat C:\\Users\\a\\AppData\\Local\\hokma\\claude-config\\.credentials.json",
    "cat /c/Users/a/.ssh/id_rsa", "cat /c/Users/crist/.aws/credentials", "grep -r x /c/Users/a/AppData/Local/hokma",
])
def test_the_real_keys_stay_blocked_on_windows_with_paths_the_posix_way(monkeypatch, command):
    _windows(monkeypatch)
    assert _win(command) == "secrets", command

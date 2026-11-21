"""Task 3299, SPEC-015, point 2 (runtime part): the sensitive packages load only in the Hokma server.

Every check that matters runs in a REAL child process, started the way Hokma starts its own children (the allow list
environment of the engines, no inherited memory) and with both interpreters: the one running pytest and the one of this
repository's virtual environment."""
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hokma.core import engine_main, processguard
from hokma.core.proc import spawn_kwargs
from hokma.engines.envfilter import filtered_env
from hokma.permissions import modes

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
VENV_PYTHON = REPO / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
INTERPRETERS = sorted({sys.executable, *([str(VENV_PYTHON)] if VENV_PYTHON.exists() else [])})
CLEAR = "can only be loaded by the Hokma server"

SENSITIVE = ["hokma.broker", "hokma.schwab", "hokma.connections", "hokma.desks", "hokma.db", "hokma.broker.service",
             "hokma.broker.rules", "hokma.schwab.orders", "hokma.connections.store", "hokma.desks.service",
             "hokma.db.repos.desks", "hokma.kit.hokma_tools", "hokma.market.options", "hokma.scheduler.service",
             "hokma.permissions.checkpoints", "hokma.engine.dbstore"]
# What the hooks, the key helper, the supervisor and the CLI need before any mark exists: it must keep loading.
FREE = ["hokma.i18n", "hokma.core.processguard", "hokma.kit.hardblock", "hokma.kit.guard", "hokma.kit.engine_key",
        "hokma.permissions.broker", "hokma.permissions.modes", "hokma.core.supervisor", "hokma.core.launcher",
        "hokma.core.engine_main", "hokma.core.nativeprobe", "hokma_cli.main"]


def server_env(**extra: str) -> dict[str, str]:
    """The environment Hokma gives its own children: the same helpers the engines and the terminals use."""
    env = filtered_env()
    env.update(modes.work_env("/tmp", "7", "9", "agent"))
    env.update(extra)
    return env


def run(python: str, args: list[str], env: dict[str, str] | None = None, stdin: str = "", timeout: int = 90):
    return subprocess.run([python, *args], input=stdin, capture_output=True, text=True, timeout=timeout, cwd=str(BACKEND),
                          env=server_env() if env is None else env, **spawn_kwargs())


# ----------------------------------------------------------------------------------------------- the unit

def test_require_refuses_without_the_mark_and_says_why(monkeypatch):
    monkeypatch.setattr(processguard, "_role", None)
    with pytest.raises(ImportError) as caught:
        processguard.require("hokma.broker")
    assert isinstance(caught.value, processguard.SensitiveModuleError)
    assert CLEAR in str(caught.value) and "hokma.broker" in str(caught.value)
    assert not processguard.marked()


def test_the_test_process_is_marked_like_the_server():
    assert processguard.marked() and processguard.role() == processguard.SERVER
    import hokma.broker, hokma.db, hokma.desks, hokma.schwab  # noqa: F401,E401  (they load in the server)
    from hokma.connections import store  # noqa: F401


def test_allow_is_scoped_and_only_for_the_entry_points(monkeypatch):
    monkeypatch.setattr(processguard, "_role", None)
    with pytest.raises(processguard.SensitiveModuleError, match="may not mark"):
        with processguard.allow(processguard.SERVER):                   # this test is not an entry point
            pass
    assert not processguard.marked()


def test_the_engine_entry_point_marks_itself_for_the_time_it_serves(monkeypatch):
    monkeypatch.setattr(processguard, "_role", None)
    seen = []
    monkeypatch.setattr(engine_main, "_serve", lambda port: seen.append(processguard.role()) or 0)
    monkeypatch.setattr(engine_main.session, "adopt", lambda *a, **k: None)
    for name in ("read_token", "read_owner_hash", "read_owner_secret_hash", "read_channel_key"):
        monkeypatch.setattr(engine_main, name, lambda stream: "")
    monkeypatch.setattr(engine_main, "read_launch_proof", lambda stream: "")
    monkeypatch.setattr(engine_main.launchproof, "verify", lambda proof: True)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert engine_main.main(1234) == 0
    assert seen == [processguard.SERVER] and not processguard.marked()


# --------------------------------------------------------------------------------- children never have it

@pytest.mark.parametrize("python", INTERPRETERS)
@pytest.mark.parametrize("module", SENSITIVE)
def test_a_child_cannot_import_a_sensitive_module(python, module):
    done = run(python, ["-I", "-c", f"import {module}"])
    assert done.returncode != 0
    assert "SensitiveModuleError" in done.stderr and CLEAR in done.stderr, done.stderr[-500:]


@pytest.mark.parametrize("python", INTERPRETERS)
@pytest.mark.parametrize("form", ["import hokma.{m}", "from hokma import {m}", "import importlib; importlib.import_module('hokma.' + '{m}')",
                                  "__import__('hokma.{m}')"])
def test_every_way_to_import_is_refused(python, form):
    done = run(python, ["-I", "-c", form.format(m="broker")])
    assert done.returncode != 0 and CLEAR in done.stderr


@pytest.mark.parametrize("python", INTERPRETERS)
@pytest.mark.parametrize("module", FREE)
def test_what_the_hooks_and_the_supervisor_need_still_loads_without_the_mark(python, module):
    done = run(python, ["-I", "-c", f"import {module}"])
    assert done.returncode == 0, done.stderr[-500:]


def test_a_child_started_by_the_server_has_no_mark_and_no_way_to_get_it():
    """This pytest process stands for the server: it is marked. What it starts is not, with the environment
    helpers Hokma uses for terminals, engines and MCP servers, and with a copy of its whole environment."""
    assert processguard.marked()
    code = "from hokma.core import processguard as p; print(p.marked())"
    for env in (server_env(), dict(os.environ)):
        done = run(sys.executable, ["-I", "-c", code], env=env)
        assert done.returncode == 0 and done.stdout.strip() == "False", done.stderr
        done = run(sys.executable, ["-I", "-c", "import hokma.db"], env=env)
        assert done.returncode != 0 and CLEAR in done.stderr
    assert not [k for k in os.environ if "PROCESSGUARD" in k.upper()] and processguard.role() == "server"


def test_the_entry_points_of_the_marks_cannot_be_reached_by_an_import():
    for python in INTERPRETERS:
        for code in ("from hokma.core import processguard as p\nwith p.allow('server'):\n    import hokma.db",
                     "from hokma.core import processguard as p\nwith p.allow('owner_cli'):\n    import hokma.db",
                     "from hokma.core import processguard as p\np._role = None\nimport hokma.broker"):
            done = run(python, ["-I", "-c", code])
            if "_role = None" in code:
                assert done.returncode != 0 and CLEAR in done.stderr
            else:
                assert done.returncode != 0 and "may not mark" in done.stderr, done.stderr[-300:]


def test_running_a_command_of_the_cli_from_a_script_leaves_no_mark_behind():
    code = ("from hokma_cli.main import main\nassert main(['version']) == 0\n"
            "import hokma.db\n")
    for python in INTERPRETERS:
        done = run(python, ["-I", "-c", code])
        assert done.returncode != 0 and CLEAR in done.stderr, done.stderr[-300:]


# ------------------------------------------------------------- what must keep working in a new process

@pytest.fixture
def owner_env(tmp_path):
    data, home = tmp_path / "data", tmp_path / "home"
    data.mkdir()
    home.mkdir()
    return server_env(HOKMA_DATA_DIR=str(data), HOKMA_HOME=str(home))


@pytest.mark.parametrize("python", INTERPRETERS)
def test_the_owner_commands_still_use_the_database(python, owner_env):
    migrate = run(python, ["-m", "hokma_cli.main", "db", "migrate"], env=owner_env)
    assert migrate.returncode == 0 and "SensitiveModuleError" not in migrate.stderr, migrate.stderr[-500:]
    doctor = run(python, ["-m", "hokma_cli.main", "doctor"], env=owner_env)
    assert doctor.returncode in (0, 1) and "SensitiveModuleError" not in doctor.stderr and doctor.stdout.strip()


@pytest.mark.parametrize("python", INTERPRETERS)
def test_the_native_probe_child_still_loads_the_database_code(python):
    done = run(python, ["-I", "-m", "hokma.core.nativeprobe", "sqlite_vec"])
    assert "SensitiveModuleError" not in done.stderr, done.stderr[-500:]
    assert done.returncode == 0 or "vec" in done.stderr.lower()


def mcp_session(python: str, env: dict[str, str], data: Path) -> list[dict]:
    script = BACKEND / "hokma" / "kit" / "trash_mcp.py"
    requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    done = run(python, ["-I", str(script), "--data", str(data)], env=env,
               stdin="".join(json.dumps(r) + "\n" for r in requests))
    assert done.returncode == 0, done.stderr[-500:]
    return [json.loads(line) for line in done.stdout.splitlines() if line.strip()]


@pytest.mark.parametrize("python", INTERPRETERS)
def test_the_mcp_server_marks_itself_and_keeps_every_tool_including_the_order_tools(python, owner_env):
    from hokma.kit import hokma_tools

    answers = mcp_session(python, owner_env, Path(owner_env["HOKMA_DATA_DIR"]))
    names = [tool["name"] for tool in answers[1]["result"]["tools"]]
    expected = [d["name"] for d in hokma_tools.definitions()]
    assert names == ["trash_move", *expected]
    assert {"order_preview", "order_place", "order_replace", "order_cancel", "order_status", "order_list", "position_list",
            "order_log"} <= set(names)


@pytest.mark.parametrize("python", INTERPRETERS)
def test_importing_the_mcp_module_without_running_it_opens_nothing(python):
    done = run(python, ["-I", "-c", "import hokma.kit.trash_mcp\nimport hokma.broker"])
    assert done.returncode != 0 and CLEAR in done.stderr
    done = run(python, ["-I", "-c", "import hokma.kit.trash_mcp as m\nm.main(['--data'])\nimport hokma.broker"])
    assert done.returncode != 0 and CLEAR in done.stderr

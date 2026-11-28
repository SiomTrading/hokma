"""Task 3431 (Hokma 1.2.9), narrowed by task 3436 (1.3.1): the lock of real money and keys. What stays `real_money` is calling a broker outside the order tools of Hokma
(hosts, URL shapes, order tools of other servers) and what stays `secrets` are the stores of keys. Importing the code of Hokma, running its command line and reading or
reading its base are free since 1.3.1: those cases of 1.2.9 now assert that they pass. Round 2 of Vera (task 3436) closed three doors again, with their own cases
in test_hardblock_1310_guard.py: the modules that talk to the broker (broker, schwab, connections, desks, exec, core.processguard), the writing of the lock, the package and
the runtime, and the writing of the file of the base. The rest of hokma is imported free, and the base is read free."""
import pytest
from hardblock_support import box  # noqa: F401  (fixture)

from hokma.permissions.broker import BrokerCtx, check


@pytest.fixture
def bctx(box):
    return BrokerCtx(data=str(box.data), roots=[str(box.work)], protect=[str(box.data)], conversation_id="7")


def wrap(body: str) -> list[str]:
    quoted = body.replace("'", "'\"'\"'")
    return [f"python -c '{quoted}'", f"python3 -c '{quoted}'", f"python <<'EOF'\n{body}\nEOF", f"sudo python -c '{quoted}'", f"bash -c \"python -c '{body}'\"" if "'" not in body else f"python -c '{quoted}'"]


MONEY_IMPORTS = [
    "from hokma.db import get_database", "import hokma.db", "import hokma.permissions", "import hokma.kit.hardblock", "from hokma import db",
    "importlib.import_module('hokma.' + 'broker')", "importlib.import_module(name)",
]


@pytest.mark.parametrize("body", MONEY_IMPORTS)
def test_importing_the_code_of_hokma_is_free(box, body):
    for command in wrap(body):
        assert box.bash(command) is None, command
    (box.work / "m.py").write_text(body + "\n")
    assert box.bash("python m.py") is None
    assert box.hit("Write", file_path=str(box.work / "w.py"), content=body + "\n") is None


@pytest.mark.parametrize("command", ["python -m hokma.db", "python -m hokma", "python -m hokma serve", "hokma engine", "hokma start", "python -m hokma_cli.main engine"])
def test_running_the_modules_or_the_command_line_of_hokma_is_free(box, command):
    assert box.bash(command) is None, command


@pytest.mark.parametrize("command", [
    "sqlite3 {db} 'select * from orders'", "sqlite3 {db} .dump", "python -c \"import sqlite3; sqlite3.connect('{db}').execute('select 1')\"", "python -c \"open('{db}', 'rb').read()\"",
    "cp {db} x.db", "psql -d hokma -c 'update orders set qty = 1'", "pg_dump hokma > d.sql",
])
def test_reading_the_base_of_hokma_is_free(box, command):
    command = command.format(db=f"{box.data}/hokma.db")
    assert box.bash(command) is None, command


@pytest.mark.parametrize("command", [
    "cat {data}/identity.key", "cat {data}/database.json", "cat {data}/control.sock", "cat {data}/engine.lock", "cat ~/.ssh/id_rsa",
])
def test_reading_the_keys_and_secrets_of_hokma_is_refused(box, command):
    assert box.bash(command.format(data=box.data)) in ("secrets", "hokma_internal", "real_money"), command


def test_the_order_tools_and_the_broker_hosts_are_still_blocked_for_a_shell(box, bctx):
    for command in ("curl https://api.schwabapi.com/trader/v1/accounts", "python -c \"import requests; requests.post('https://api.schwabapi.com/x')\""):
        assert box.bash(command) == "real_money", command
    for tool in ("mcp__Hokma__order_place",):
        verdict = check(tool, {"order": {}}, cwd=str(box.work), ctx=bctx)
        assert verdict.allowed is False and verdict.rule == "real_money", tool


def test_the_hook_and_the_broker_of_permissions_say_the_same(box, bctx):
    for command, rule in (("curl https://api.schwabapi.com/trader/v1/accounts", "real_money"), (f"cat {box.data}/identity.key", "secrets")):
        done = box.run_hook({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(box.work)})
        assert done.returncode == 2 and f"HOKMA_BLOCKED {rule}" in done.stderr, command
        verdict = check("Bash", {"command": command}, cwd=str(box.work), ctx=bctx)
        assert (verdict.allowed, verdict.rule) == (False, rule), command
    for command in ("sqlite3 " + f"{box.data}/hokma.db" + " 'select 1'", 'python -c "import hokma.db"'):
        assert box.run_hook({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(box.work)}).returncode == 0, command
        assert check("Bash", {"command": command}, cwd=str(box.work), ctx=bctx).allowed, command
    for command, rule in (('python -c "import hokma.broker"', "real_money"), ("sqlite3 " + f"{box.data}/hokma.db" + " 'delete from t'", "hokma_internal")):
        done = box.run_hook({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(box.work)})
        assert done.returncode == 2 and f"HOKMA_BLOCKED {rule}" in done.stderr, command
        verdict = check("Bash", {"command": command}, cwd=str(box.work), ctx=bctx)
        assert (verdict.allowed, verdict.rule) == (False, rule), command

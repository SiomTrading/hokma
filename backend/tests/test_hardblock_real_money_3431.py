"""Task 3431 (Hokma 1.2.9): the lock of real money, keys and the base does not move when the lock of the package `hokma` is narrowed. This file passes the same before and after that change.
Every case here is a way for an agent to reach the broker, the orders, the accounts, the keys or the tables of money. They are all `real_money` or `secrets`."""
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
    "import hokma.broker", "from hokma import broker", "from hokma.broker.service import place", "import hokma.schwab", "from hokma.schwab import orders", "from hokma import schwab, connections",
    "import hokma.connections", "import hokma.exec", "from hokma.exec import submit", "from hokma import exec", "import hokma.desks", "from hokma.db import get_database", "import hokma.db",
    "import hokma.permissions", "import hokma.kit.hardblock", "import hokma.core.processguard",
    "__import__('hokma.broker')", "importlib.import_module('hokma.schwab')", "importlib.import_module('hokma.' + 'broker')", "importlib.import_module(name)",
]


@pytest.mark.parametrize("body", MONEY_IMPORTS)
def test_importing_a_module_of_money_keys_or_the_base_is_real_money(box, body):
    for command in wrap(body):
        assert box.bash(command) == "real_money", command
    (box.work / "m.py").write_text(body + "\n")
    assert box.bash("python m.py") == "real_money"
    assert box.hit("Write", file_path=str(box.work / "w.py"), content=body + "\n") == "real_money"


@pytest.mark.parametrize("command", ["python -m hokma.broker", "python -m hokma.schwab", "python -m hokma.connections", "python -m hokma.exec", "python -m hokma.db", "python -m hokma", "python -m hokma serve",
                                     "python -Im hokma.broker.service", "hokma engine", "hokma start", "python -m hokma_cli.main engine"])
def test_running_the_money_modules_or_the_server_is_real_money(box, command):
    assert box.bash(command) == "real_money", command


@pytest.mark.parametrize("command", [
    "sqlite3 {db} 'update orders set qty = 1'", "sqlite3 {db} 'delete from broker_accounts'", "sqlite3 {db} .dump", "sqlite3 {db} 'update desks set capital = 9999999'",
    "python -c \"import sqlite3; sqlite3.connect('{db}').execute('update orders set qty = 1')\"", "python -c \"open('{db}', 'rb').read()\"", "cp x.db {db}", "echo x > {db}",
    "psql -d hokma -c 'update orders set qty = 1'", "pg_dump hokma > d.sql",
])
def test_writing_or_reading_the_tables_of_money_is_refused(box, command):
    command = command.format(db=f"{box.data}/hokma.db")
    assert box.bash(command) in ("real_money", "secrets"), command


@pytest.mark.parametrize("command", [
    "cat {data}/hokma.db", "cat {data}/database.json", "cat {data}/control.sock", "cat {data}/engine.lock", "cat ~/.ssh/id_rsa",
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
    for command in ('python -c "import hokma.broker"', "sqlite3 " + f"{box.data}/hokma.db" + " 'update orders set qty = 1'"):
        done = box.run_hook({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(box.work)})
        assert done.returncode == 2 and "HOKMA_BLOCKED real_money" in done.stderr, command
        verdict = check("Bash", {"command": command}, cwd=str(box.work), ctx=bctx)
        assert (verdict.allowed, verdict.rule) == (False, "real_money"), command

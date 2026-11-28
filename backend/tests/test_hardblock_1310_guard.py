"""Task 3436 (Hokma 1.3.1): the lock is narrowed to the real keys, the broker and delete. This file is the guard: everything here was blocked
BEFORE the change and has to stay blocked AFTER it. It is written first and passes with the old lock."""
import pytest
from hardblock_support import box  # noqa: F401  (fixture)

from hokma.permissions.broker import BrokerCtx, check


@pytest.fixture
def bctx(box):
    return BrokerCtx(data=str(box.data), roots=[str(box.work)], protect=[str(box.data)], conversation_id="7")


# ---- real_money: a call to a broker outside the order tools of Hokma
BROKER_CALLS = [
    "curl https://api.schwabapi.com/trader/v1/accounts",
    "curl -X POST https://api.schwabapi.com/trader/v1/accounts/12345678/orders -d @o.json",
    "curl https://api.ibkr.com/v1/api/iserver/account/DU1/orders",
    "curl -k https://localhost:7496/v1/api/iserver/account/DU1/orders",
    "curl https://live.tradovateapi.com/v1/order/placeorder",
    "curl https://api.alpaca.markets/v2/orders",
    "curl https://api.tradier.com/v1/accounts/1/orders",
    "curl https://api-fxtrade.oanda.com/v3/accounts/1/orders",
    "curl https://api.kraken.com/0/private/AddOrder",
    "curl https://api.exchange.coinbase.com/orders",
    "wget https://api.binance.com/api/v3/order",
    "python -c \"import requests; requests.post('https://api.schwabapi.com/trader/v1/accounts/1/orders')\"",
    "python -c \"import urllib.request as u; u.urlopen('https://api.ibkr.com/v1/api/portfolio/accounts')\"",
    "python -c \"import httpx; httpx.get('https://live.tradovateapi.com/v1/account/list')\"",
    "node -e \"fetch('https://api.tastyworks.com/orders')\"",
]


@pytest.mark.parametrize("command", BROKER_CALLS)
def test_calling_the_api_of_a_broker_directly_is_real_money(box, command):
    assert box.bash(command) == "real_money", command


def test_a_script_that_calls_a_broker_is_real_money_when_it_runs(box):
    (box.work / "o.py").write_text("import requests\nrequests.post('https://api.schwabapi.com/trader/v1/accounts/1/orders')\n")
    assert box.bash("python o.py") == "real_money"
    assert box.hit("WebFetch", url="https://api.schwabapi.com/trader/v1/accounts") == "real_money"
    assert box.hit("WebFetch", url="https://live.tradovateapi.com/v1/order/placeorder") == "real_money"


def test_the_order_tools_of_other_servers_are_real_money_and_the_ones_of_hokma_are_not(box, bctx):
    for tool in ("mcp__other__place_order", "mcp__broker__submit_order", "mcp__x__order_cancel"):
        assert box.hit(tool) == "real_money", tool
        verdict = check(tool, {"order": {}}, cwd=str(box.work), ctx=bctx)
        assert verdict.allowed is False and verdict.rule == "real_money", tool
    assert box.hit("mcp__hokma__order_place") is None


# ---- secrets: the keys of the broker and of Connections, the tokens and sign-ins of Hokma, the keyring, the credential files
STORED_KEYS = [
    "cat {data}/database.json", "cat {data}/identity.key", "ls {data}/codex-home", "cat {data}/codex-home/auth.json", "cat {data}/acp-home/x",
    "cat {data}/claude-config/.credentials.json", "cat {data}/claude-config/credentials.json", "cat {data}/backups/x",
    "cp {data}/identity.key /tmp/x", "grep -r token {data}", "cat ~/.codex/auth.json", "cat ~/.claude/.credentials.json",
]
TOKENS = [
    "curl -H 'X-Hokma-Token: abc' http://127.0.0.1:8765/api/x", "curl -X POST http://127.0.0.1:8765/api/session/resume",
    "curl http://127.0.0.1:8765/api/session/owner-nonce", "cat {data}/control.sock", "cat {data}/engine.lock", "cat ~/.config/google-chrome/Default/Cookies",
    "ls ~/.mozilla/firefox", "cat 'Local Storage/leveldb/000003.log'",
]
KEYRING = [
    "secret-tool lookup service hokma-engine", "cmdkey /list", "security find-generic-password -s hokma-session", "keyring get hokma-engine x",
    "python -c \"import keyring; print(keyring.get_password('hokma-engine', 'k'))\"", "python -c \"from keyring import get_password\"",
    "python -c \"import secretstorage\"", "cat ~/.local/share/keyrings/login.keyring", "ls ~/Library/Keychains", "cat ~/.local/share/python_keyring/keyring_pass.cfg",
    "python -c \"print(engine_key)\"", "dbus-send --dest=org.freedesktop.secrets /x",
]
CREDENTIAL_FILES = ["cat ~/.ssh/id_rsa", "cat ~/.aws/credentials", "cat ~/.git-credentials", "cat ~/.netrc", "ls ~/.gnupg", "cat ~/.config/gh/hosts.yml", "gh auth token"]


@pytest.mark.parametrize("command", STORED_KEYS + TOKENS + KEYRING + CREDENTIAL_FILES)
def test_the_stores_of_keys_tokens_and_sign_ins_are_secrets(box, command):
    assert box.bash(command.replace("{data}", str(box.data))) == "secrets", command


def test_the_read_tools_cannot_open_the_stores_of_keys(box):
    for path in (box.data / "database.json", box.data / "identity.key", box.data / "claude-config" / ".credentials.json", box.data / "codex-home" / "auth.json"):
        assert box.hit("Read", file_path=str(path)) == "secrets", path
    assert box.hit("Grep", pattern="token", path=str(box.data)) == "secrets"
    assert box.hit("Write", file_path=str(box.data / "identity.key"), content="x") == "secrets"


def test_a_recursive_search_over_a_folder_that_holds_the_data_folder_is_secrets(box):
    assert box.bash(f"grep -r token {box.tmp}") == "secrets"
    assert box.hit("Grep", pattern="token", path=str(box.tmp)) == "secrets"


# ---- delete: unchanged
DELETES = ["rm f", "rm -rf d", "rmdir d", "unlink f", "find . -name x -delete", "mv f /dev/null", "shred f", "python -c \"import os; os.remove('f')\"",
           "python -c \"import shutil; shutil.rmtree('d')\"", "node -e \"require('fs').unlinkSync('f')\"", "bash -c 'rm f'", "eval 'rm f'", "echo cm0gZg== | base64 -d | sh",
           "xargs rm < list.txt", "find . -exec rm {} ;", "$x f", "rm /tmp/anything", "rm ~/Documents/x"]


@pytest.mark.parametrize("command", DELETES)
def test_deleting_files_is_still_blocked(box, command):
    assert box.bash(command) == "delete", command


def test_deleting_with_powershell_is_still_blocked(box):
    for command in ("Remove-Item f", "Remove-Item -Recurse -Force d", "del f", "ri f", "[System.IO.File]::Delete('f')"):
        assert box.ps(command) == "delete", command


# ===================================================================== round 2 of Vera (task 3436): four doors, each one narrow
# Nothing here brings back what 1.3.1 freed: the base is read, Hokma is imported, system folders and built paths are free, and a normal folder is written.
VERA_STILL_BLOCKED = {
    "curl https://API.SCHWABAPI.COM/trader/v1/accounts": "real_money", "cu''rl https://api.schwabapi.com/x": "real_money",
    "bash -c 'curl https://api.schwabapi.com/x'": "real_money", "wget -qO- https://live.tradovateapi.com/v1/order/placeorder": "real_money",
    "nc api.schwabapi.com 443": "real_money", "cat {data}/'database.json'": "secrets", "cat {data}/ident*.key": "secrets",
    "python -c \"print(open('{data}/identity.key').read())\"": "secrets", "tar cf x.tar {data}": "secrets", "bash -c 'cat {data}/identity.key'": "secrets",
    "head -c 100 {data}/database.json": "secrets", "echo {1..100} ; cat {data}/identity.key": "secrets",
    "echo {a,b}{a,b}{a,b}{a,b}{a,b}{a,b}{a,b} {data}/identity.key": "secrets", "cat {a,b}{a,b}{a,b}{a,b}{a,b}{a,b}{a,b} {data}/identity.key": "secrets",
}


@pytest.mark.parametrize("command", VERA_STILL_BLOCKED)
def test_what_vera_confirmed_blocked_stays_blocked(box, command):
    assert box.bash(command.replace("{data}", str(box.data))) == VERA_STILL_BLOCKED[command], command


def test_a_command_name_or_a_payload_built_at_run_time_is_still_blocked(box):
    assert box.bash("c=curl; $c https://api.schwabapi.com/trader/v1/accounts") is not None
    assert box.bash("echo Y3VybCBodHRwczovL2FwaS5zY2h3YWJhcGkuY29tL3g= | base64 -d | sh") is not None


# ---- A1. a command or variable in the path that can reach a file of keys of the data folder
DYNAMIC_TO_KEYS = [
    "cat {data}/../$(basename {data})/identity.key", "d={data}; cat $d/identity.key", "d={data}; cat \"$d/database.json\"", "cat $(pwd)/identity.key",
    "cat {data}/$(echo identity.key)", "cat {data}/$@{name}", "cat $(echo {data})/codex-home/auth.json", "cat BACKTICKecho {data}BACKTICK/acp-home/x", "d={data}; cat $d/ident*.key",
    "cat {data}/claude-config/$f", "cat {data}/$(echo database).json", "d={data}; cat < $d/identity.key", "d={data}; head -c 9 $d/claude-config/.credentials.json",
    "cp {data}/../$(basename {data})/identity.key /tmp/x",
]
DYNAMIC_FREE = [
    "cat $HOME/notes.md", "cat $(pwd)/informes/viejo.md", "d=informes; cat $d/viejo.md", "d=x; ls $d/backups", "d=x; ls $d/*.json", "cat $(pwd)/../work/notes.md",
    "cat {data}/plugins/$name", "ls $(dirname {data})", "cat $DOTNET_ROOT/sdk/version.txt", "d=x; cat $d/credentials-notes.md",
]


@pytest.mark.parametrize("command", DYNAMIC_TO_KEYS)
def test_a_path_with_a_command_or_a_variable_that_can_reach_the_keys_is_secrets(box, command):
    assert box.bash(command.replace("BACKTICK", chr(96)).replace("$@{", "$" + "{").replace("{data}", str(box.data))) == "secrets", command


@pytest.mark.parametrize("command", DYNAMIC_FREE)
def test_a_dynamic_path_that_does_not_go_near_the_keys_is_free(box, command):
    assert box.bash(command.replace("{data}", str(box.data))) is None, command


# ---- A2. the braces past the limit: closed only when the text touches the data folder or names a key
def test_braces_past_the_limit_fail_closed_only_near_the_keys(box):
    big = "{a,b,c,d,e,f,g,h}{1,2,3,4,5,6,7,8,9}"
    assert box.bash(f"cat {box.data}/{big}/x") == "secrets"
    assert box.bash(f"cat {box.data}/{big}/identity.key") == "secrets"
    assert box.bash(f"cat /srv/{big}/identity.key") == "secrets"
    assert box.bash(f"cat {big}/database.json") == "secrets"
    assert box.bash(f"grep -r x {box.data}/{big}") == "secrets"
    assert box.bash(f"ls /tmp/{big}") is None
    assert box.bash(f"cat {big}.txt") is None
    assert box.bash(f"cat {box.work}/{big}/notes.md") is None
    assert box.bash(f"cat {box.data}/plugins") is None


# ---- B. the modules that talk to the broker or put orders are not imported or loaded by an agent; the rest of hokma is
SENSITIVE_CODE = [
    "import hokma.broker", "import hokma.broker as b; b.place_order()", "from hokma.broker import service", "from hokma import broker", "from hokma import schwab",
    "from hokma import db, desks", "from hokma import (db,\n    broker)", "from hokma.connections import list_connections", "import hokma.connections.store",
    "from hokma.schwab import orders", "import hokma.exec.live", "from hokma.exec import submit", "import hokma.desks.service", "from hokma.desks.trading import exits",
    "import hokma.core.processguard", "from hokma.core.processguard import allow", "import hokma . broker",
    "import importlib; importlib.import_module('hokma.broker')", "import importlib; importlib.import_module(\"hokma.schwab.orders\")", "__import__('hokma.connections')",
    "import importlib; importlib.import_module('.broker', 'hokma')", "import hokma as h; h.broker.service", "import hokma; getattr(hokma, 'schwab')",
    "import runpy; runpy.run_module('hokma.exec.live')", "import importlib.util as u; u.spec_from_file_location('m', '/x/hokma/broker/__init__.py')",
    "import importlib.util as u; u.spec_from_file_location('m', 'C:\\\\x\\\\hokma\\\\schwab\\\\orders.py')",
]
HOKMA_FREE_CODE = [
    "import hokma", "import hokma.db", "from hokma.db import get_database", "import hokma.factory", "from hokma import backtest", "from hokma.quant import metrics",
    "import hokma.core.app", "from hokma.core import app", "import hokma.engines", "import hokma.executor_notes", "from hokma import db", "import hokma.connectionsx",
    "import importlib; importlib.import_module('hokma.backtest')", "name = 'bro' + 'ker'; __import__('hokma.' + name)", "import hokma.broker_notes",
    "print('a hokma strategy places no order by itself')",
]


@pytest.mark.parametrize("body", SENSITIVE_CODE)
def test_the_modules_that_talk_to_the_broker_are_real_money_from_every_way_to_run_code(box, body):
    quoted = body.replace("'", "'\"'\"'")
    assert box.bash(f"python -c '{quoted}'") == "real_money", body
    assert box.bash(f"python3 <<'EOF'\n{body}\nEOF") == "real_money", body
    (box.work / "m.py").write_text(body + "\n")
    assert box.bash("python m.py") == "real_money", body
    assert box.bash("python ./m.py") == "real_money", body
    assert box.bash("cat m.py | python") == "real_money", body
    assert box.hit("Write", file_path=str(box.work / "w.py"), content=body + "\n") == "real_money", body
    assert box.hit("Edit", file_path=str(box.work / "m.py"), old_string="x", new_string=body) == "real_money", body
    assert box.hit("MultiEdit", file_path=str(box.work / "m.py"), edits=[{"old_string": "x", "new_string": body}]) == "real_money", body


@pytest.mark.parametrize("body", HOKMA_FREE_CODE)
def test_the_rest_of_hokma_is_imported_free(box, body):
    quoted = body.replace("'", "'\"'\"'")
    assert box.bash(f"python -c '{quoted}'") is None, body
    (box.work / "m.py").write_text(body + "\n")
    assert box.bash("python m.py") is None, body
    assert box.hit("Write", file_path=str(box.work / "w.py"), content=body + "\n") is None, body


def test_a_python_file_written_with_a_shell_and_the_module_run_by_name_are_real_money(box):
    assert box.bash("echo 'import hokma.broker' > w.py") == "real_money"
    assert box.bash("cat > w.py <<'EOF'\nfrom hokma.schwab import orders\nEOF") == "real_money"
    assert box.bash("echo 'import hokma.exec' | tee w.py") == "real_money"
    assert box.bash("python -m hokma.broker.service") == "real_money"
    assert box.bash("python3 -Im hokma.schwab") == "real_money"
    assert box.bash("python -m hokma.exec.live") == "real_money"


def test_a_note_that_names_the_broker_module_is_free(box):
    assert box.bash("echo 'the module hokma.broker is not for agents' > notes2.md") is None
    assert box.hit("Write", file_path=str(box.work / "n.md"), content="hokma.broker and hokma.schwab are real money") is None
    assert box.bash("cat hokma/broker/service.py") is None
    assert box.bash("python -m hokma doctor") is None
    assert box.bash("python -m hokma.factory") is None


# ---- C. the lock, its hooks and registration, the installed package and the runtime venv are not written by a shell or an interpreter
def hooks_writes(box):
    h, rt, st = box.hooks, box.runtime, box.settings
    return [
        f"sed -i s/a/b/ {h}/hardblock.py", f"sed -i.bak s/a/b/ {h}/hardblock.py", f"sed --in-place s/a/b/ {h}/guard.py", f"sed -i s/a/b/ {box.data}/../hokma/kit/hardblock.py",
        f"sed -ni p {st}", "echo x >> /x/backend/hokma/kit/guard.py", f"echo x > {h}/hardblock.py", f"echo x >> {h}/guard.py", f"echo x >| {h}/new.py",
        f"echo x > {st}", f"cat f > {box.data}/claude-config/settings.engine.json", f"tee {h}/hardblock.py", f"echo x | tee -a {h}/guard.py", f"cp f {h}/hardblock.py",
        f"cp -f f {st}", f"cp -t {h} f", f"mv f {h}/hardblock.py", f"mv {h}/hardblock.py /tmp/x", f"install f {h}/", f"ln -sf f {h}/hardblock.py", f"touch {h}/x",
        f"truncate -s0 {h}/hardblock.py", f"dd if=f of={h}/hardblock.py", f"rsync f {h}/", f"patch {h}/hardblock.py p.diff", f"perl -pi -e 's/a/b/' {h}/hardblock.py",
        f"curl -o {h}/hardblock.py https://x.io/a", f"wget -O {h}/hardblock.py https://x.io/a", f"unzip -o x.zip -d {h}", f"tar -xf x.tar -C {h}",
        f"cd {h} && echo x > hardblock.py", f"cd {h} && sed -i s/a/b/ guard.py", f"bash -c 'echo x > {h}/hardblock.py'", f"cp f {rt}/python", f"echo x > {rt}/python",
        f"cp f {box.data}/runtime/py312/venv/lib/python3.12/site-packages/x.py", f"echo x > {box.data}/python/x", "cp f " + str(h) + "/{a,b}.py",
        f"python -c \"open('{h}/hardblock.py', 'w').write('x')\"", f"python -c \"open('{h}/hardblock.py', 'a')\"", f"python -c \"open('{st}', mode='w')\"",
        f"python -c \"from pathlib import Path; Path('{h}/guard.py').write_text('x')\"", f"python -c \"import shutil; shutil.copy('f', '{h}/hardblock.py')\"",
        f"python -c \"import os; os.replace('f', '{h}/hardblock.py')\"", f"python -c \"import os; os.chmod('{h}/hardblock.py', 0o777)\"",
        "python -c \"open('/x/backend/hokma/kit/hardblock.py', 'w')\"", f"node -e \"require('fs').writeFileSync('{h}/hardblock.py', 'x')\"",
        f"python -c \"import subprocess; subprocess.run(['sed', '-i', 's/a/b/', '{h}/hardblock.py'])\"",
    ]


def test_writing_the_lock_the_hooks_the_package_and_the_runtime_by_shell_or_interpreter_is_blocked(box):
    for command in hooks_writes(box):
        assert box.bash(command) == "hokma_internal", command
    (box.work / "w.py").write_text(f"open('{box.hooks}/hardblock.py', 'w').write('x')\n")
    assert box.bash("python w.py") == "hokma_internal"
    assert box.bash(f"python <<'EOF'\nopen('{box.hooks}/guard.py', 'w').write('x')\nEOF") == "hokma_internal"


def test_pip_and_ensurepip_in_the_runtime_venv_are_blocked_and_listing_is_free(box):
    rt = box.runtime
    for command in (f"{rt}/python -m pip install x", f"{rt}/python -m ensurepip", f"{rt}/python -m pip uninstall -y x", f"{rt}/pip install x", f"{rt}/pip3 install x",
                    f"python -m pip install --target {box.data}/runtime/x y", f"python -m pip install --target={box.data}/runtime/x y", f"pip install --prefix {box.data}/runtime y",
                    f"pip install --target {box.hooks} y", f"uv pip install --python {rt}/python x", f"source {rt}/activate && pip install x",
                    f"{rt}/python -c \"import pip; pip.main(['install', 'x'])\"", f"{rt}/python -c \"import ensurepip; ensurepip.bootstrap()\"",
                    f"{rt}/python -m ensurepip --upgrade"):
        assert box.bash(command) == "hokma_internal", command
    for command in (f"{rt}/python -m pip list", f"{rt}/pip freeze", f"{rt}/python -m pip show x", "pip install x", "python -m pip install x", "pip install -r requirements.txt",
                    "pip install --target ./libs x", "python -m venv .venv", "uv pip install x", "uv run script.py"):
        assert box.bash(command) is None, command


def test_reading_the_hooks_the_package_and_the_runtime_stays_free(box):
    h, rt = box.hooks, box.runtime
    for command in (f"cat {h}/hardblock.py", f"grep -n def {h}/hardblock.py", f"cp {h}/hardblock.py /tmp/copy.py", f"ls {h}", f"ls {rt}", f"cat {box.settings}",
                    f"head -5 {h}/guard.py", f"diff {h}/hardblock.py f", f"{rt}/python -m pytest tests", f"{rt}/python script.py", f"python -c \"print(open('{h}/hardblock.py').read())\"",
                    f"python -c \"import shutil; shutil.copy('{h}/hardblock.py', '/tmp/x.py')\"", f"python -c \"open('{h}/hardblock.py', 'rb').read()\"",
                    "echo x > notes3.md", "sed -i s/a/b/ notes.md", "cp f d/f2", "mv f d/f3", "tee d/log.txt", "touch d/new.txt", f"echo x > {box.data}/plugins-x",
                    "python -c \"open('notes.md', 'w').write('x')\"", "perl -pi -e 's/a/b/' notes.md", "curl -o d/x.html https://x.io/a"):
        assert box.bash(command) is None, command


# ---- D. the base: reading hokma.db is free, writing the file directly (sqlite3, an engine, open, a copy) is not; the writes go through the tools of Hokma
DB_WRITES = [
    "sqlite3 {data}/hokma.db \"update settings set orders_enabled=1\"", "sqlite3 {data}/hokma.db \"UPDATE settings SET a=1 WHERE b=2\"",
    "sqlite3 {data}/hokma.db 'insert into t values (1)'", "sqlite3 {data}/hokma.db 'delete from t'", "sqlite3 {data}/hokma.db 'drop table t'",
    "sqlite3 {data}/hokma.db 'create table t (a)'", "sqlite3 {data}/hokma.db 'alter table t add column b'", "sqlite3 {data}/hokma.db 'replace into t values (1)'",
    "sqlite3 {data}/hokma.db 'pragma writable_schema=1'", "sqlite3 {data}/hokma.db 'vacuum'", "sqlite3 {data}/hokma.db '.import f.csv t'", "sqlite3 {data}/hokma.db '.read w.sql'",
    "sqlite3 {data}/hokma.db '.restore b.db'", "sqlite3 {data}/hokma.db '.shell id'", "sqlite3 -init w.sql {data}/hokma.db 'select 1'",
    "sqlite3 :memory: \"attach '{data}/hokma.db' as h; update h.settings set a=1\"", "sqlite3 {data}/hokma.db <<'EOF'\ninsert into t values (1);\nEOF",
    "sqlite3 {data}/hokma.db <<< 'update t set a=1'", "echo 'delete from t' | sqlite3 {data}/hokma.db", "cd {data} && sqlite3 hokma.db 'update t set a=1'",
    "python -c \"import sqlite3; c = sqlite3.connect('{data}/hokma.db'); c.execute('update t set a=1'); c.commit()\"",
    "python -c \"import sqlite3; sqlite3.connect('{data}/hokma.db').executescript('delete from t')\"",
    "python -c \"import sqlite3, os; c = sqlite3.connect(os.path.join(os.environ['HOKMA_DATA_DIR'], 'hokma.db')); c.execute('insert into t values (1)')\"",
    "python -c \"import sqlalchemy as sa; e = sa.create_engine('sqlite:///{data}/hokma.db'); e.execute('delete from t')\"",
    "python -c \"import pandas as pd, sqlite3; pd.DataFrame({'a': [1]}).to_sql('t', sqlite3.connect('{data}/hokma.db'))\"",
    "python -c \"open('{data}/hokma.db', 'wb').write(b'')\"", "python -c \"open('{data}/hokma.db-wal', 'ab').write(b'x')\"",
    "python -c \"import shutil; shutil.copy('f', '{data}/hokma.db')\"", "python -c \"import os; os.replace('f', '{data}/hokma.db')\"",
    "python -c \"from pathlib import Path; Path('{data}/hokma.db-journal').write_bytes(b'')\"", "node -e \"require('fs').writeFileSync('{data}/hokma.db', 'x')\"",
    "cp f {data}/hokma.db", "mv f {data}/hokma.db", "mv f {data}/hokma.db-wal", "echo x > {data}/hokma.db", "echo x >> {data}/hokma.db-shm", "tee {data}/hokma.db-journal",
    "truncate -s0 {data}/hokma.db", "dd if=f of={data}/hokma.db", "sed -i s/a/b/ {data}/hokma.db", "rsync f {data}/hokma.db", "cat f > {data}/hokma.db",
]
DB_READS = [
    "sqlite3 {data}/hokma.db 'select * from strategies'", "sqlite3 {data}/hokma.db 'SELECT count(*) FROM orders WHERE status = 1'", "sqlite3 -readonly {data}/hokma.db 'select 1'",
    "sqlite3 {data}/hokma.db '.tables'", "sqlite3 {data}/hokma.db '.schema'", "sqlite3 {data}/hokma.db '.dump'", "sqlite3 -header -csv {data}/hokma.db 'select * from t' > out.csv",
    "sqlite3 {data}/hokma.db 'pragma table_info(strategies)'", "sqlite3 {data}/hokma.db <<'EOF'\nselect * from strategies;\nEOF", "sqlite3 {data}/hokma.db <<< 'select 1'",
    "echo 'select 1' | sqlite3 {data}/hokma.db", "cat {data}/hokma.db", "cat {data}/hokma.db-wal", "cp {data}/hokma.db /tmp/hokma-copy.db", "cp {data}/hokma.db copy.db",
    "grep -c x {data}/hokma.db", "ls -l {data}/hokma.db", "sqlite3 copy.db 'update t set a=1'", "sqlite3 {data}/other.db 'insert into t values (1)'",
    "python -c \"import sqlite3; print(sqlite3.connect('{data}/hokma.db').execute('select 1').fetchall())\"",
    "python -c \"import sqlite3, os; sqlite3.connect(os.path.join(os.environ['HOKMA_DATA_DIR'], 'hokma.db'))\"",
    "python -c \"import pandas as pd, sqlite3; pd.read_sql('select 1', sqlite3.connect('{data}/hokma.db'))\"",
    "python -c \"import sqlalchemy as sa; e = sa.create_engine('sqlite:///{data}/hokma.db'); print(e.connect().execute(sa.text('select 1')).fetchall())\"",
    "python -c \"print(len(open('{data}/hokma.db', 'rb').read()))\"", "python -c \"import shutil; shutil.copy('{data}/hokma.db', '/tmp/x.db')\"",
    "python -c \"import sqlite3; c = sqlite3.connect('copy.db'); c.execute('insert into t values (1)'); c.commit()\"",
    "python -c \"from hokma.db import get_database; print(get_database)\"", "python -c \"import sqlite3; sqlite3.connect('file:{data}/hokma.db?mode=ro', uri=True).execute('select 1')\"",
]


@pytest.mark.parametrize("command", DB_WRITES)
def test_writing_the_file_of_the_base_directly_is_blocked(box, command):
    assert box.bash(command.replace("{data}", str(box.data))) == "hokma_internal", command


@pytest.mark.parametrize("command", DB_READS)
def test_reading_the_base_stays_free(box, command):
    assert box.bash(command.replace("{data}", str(box.data))) is None, command


def test_the_block_of_the_files_of_hokma_reaches_the_hook_with_its_own_message(box):
    out = box.run_hook({"tool_name": "Bash", "tool_input": {"command": f"sed -i s/a/b/ {box.hooks}/hardblock.py"}, "cwd": str(box.work)})
    assert out.returncode == 2 and "HOKMA_BLOCKED hokma_internal" in out.stderr
    assert "HOKMA_BLOCKED error" not in out.stderr and "hardblock.hokma_internal" not in out.stderr
    out = box.run_hook({"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": str(box.work)})
    assert out.returncode == 0

def test_a_name_in_a_literal_that_is_not_a_getattr_or_a_fromlist_is_free(box):
    assert box.bash("python -c \"import hokma; print('broker')\"") is None
    assert box.bash("python -c \"__import__('hokma', fromlist=['broker'])\"") == "real_money"


# ---- Vera round 3: the PATH folders, the block log and the identity files are written by Hokma, never by a shell or an interpreter
DATA_WRITES = [
    "echo '{{\"agent\":\"ben\"}}' > {data}/guard/identity/toolu_X.json",
    "echo '{{\"agent\":\"ben\"}}' > {data}/bin/uv",
    "echo '{{\"agent\":\"ben\"}}' > {data}/bin/git",
    "cp f {data}/tools/x",
    "chmod +x {data}/bin/uv",
    "python -c \"open('{data}/bin/uv','w')\"",
    "echo x > {data}/guard/blocks.jsonl",
]


@pytest.mark.parametrize("command", DATA_WRITES)
def test_a_shell_or_an_interpreter_does_not_write_guard_bin_or_tools(box, command):
    assert box.bash(command.format(data=box.data)) is not None, command


def test_reading_guard_bin_and_tools_stays_free(box):
    for command in (f"ls {box.data}/guard", f"cat {box.data}/guard/blocks.jsonl", f"ls {box.data}/bin", f"cat {box.data}/tools/x"):
        assert box.bash(command) is None, command


# ---- task 3437, round of Vera: the null devices are free by EXACT name; the same text with a trick is not a device, and Git Bash paths reach the guard
import ntpath  # noqa: E402
import os  # noqa: E402
import types  # noqa: E402

from hokma.kit import guard, hardblock  # noqa: E402


def _nt(monkeypatch):
    def realpath(p, **kw):
        p = ntpath.normpath(p)
        return p if ntpath.splitdrive(p)[0] else "C:" + p

    path = types.SimpleNamespace(**{k: getattr(ntpath, k) for k in dir(ntpath) if not k.startswith("__")})
    path.realpath, path.exists = realpath, (lambda p: False)
    fake = types.SimpleNamespace(**{k: getattr(os, k) for k in dir(os) if not k.startswith("__")})
    fake.name, fake.path, fake.sep = "nt", path, "\\"
    fake.stat = lambda p, *a, **k: (_ for _ in ()).throw(OSError("no file"))
    monkeypatch.setattr(hardblock, "os", fake)
    monkeypatch.setattr(guard, "os", fake)
    monkeypatch.setattr(guard, "CASE_INSENSITIVE", True)


DATA_W = "C:\\Users\\a\\AppData\\Local\\hokma"
WORK_W = "C:\\Users\\a\\Hokma"
PROTECT_W = [DATA_W + "\\guard", DATA_W + "\\bin", DATA_W + "\\runtime"]


def _w(command):
    ctx = hardblock.Ctx(DATA_W, [WORK_W], PROTECT_W, home="C:\\Users\\a", cwd=WORK_W, brokers={"hosts": [], "url_patterns": []})
    block = hardblock.evaluate({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": WORK_W}, ctx)
    return block.rule if block else None


@pytest.mark.parametrize("p", ["/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/fd/2", "/proc/self/fd/1", "/dev/./null"])
def test_the_exact_devices_are_devices(p):
    assert hardblock.is_device(p), p


@pytest.mark.parametrize("p", ["/dev/fd/../../x/hokma.db", "/dev/fd/1/../x", "/dev/null/x", "/dev/nullx", "/dev/fd/", "/dev/fd/a", "/proc/self/fd/../x", "/dev/stdout/guard/x"])
def test_a_device_prefix_with_a_trick_is_no_device(p):
    assert not hardblock.is_device(p), p


def test_a_device_prefix_that_climbs_to_the_base_or_the_guard_is_blocked(box):
    db = str(box.data / "hokma.db")
    assert box.bash(f"echo x > /dev/fd/../../..{db}") == "hokma_internal"
    assert box.bash(f"echo x > /dev/fd/../../..{box.data}/guard/x") == "hokma_internal"
    assert box.bash(f"echo x > /proc/self/fd/../../..{db}") == "hokma_internal"


def test_the_names_of_windows_are_devices_only_on_windows(monkeypatch):
    assert not hardblock.is_device("NUL") and not hardblock.is_device("C:\\x\\NUL")
    _nt(monkeypatch)
    for p in ("NUL", "nul", "CON", "\\\\.\\NUL", "C:\\work\\NUL", DATA_W + "\\guard\\NUL"):
        assert hardblock.is_device(p), p
    for p in ("\\\\?\\C:\\Users\\a\\AppData\\Local\\hokma\\guard\\NUL", "//?/C:/x/NUL", "\\\\.\\C:\\x\\NUL"):
        assert not hardblock.is_device(p), p


def test_a_raw_path_to_a_file_called_nul_in_the_guard_is_blocked_on_windows(monkeypatch):
    _nt(monkeypatch)
    assert _w("echo x > '\\\\?\\C:\\Users\\a\\AppData\\Local\\hokma\\guard\\NUL'") in ("hokma_internal", "secrets")
    assert _w("echo x >NUL") is None
    assert _w("echo x > " + "'" + DATA_W + "\\guard\\NUL'") is None


@pytest.mark.parametrize("p", ["/c/Users/a/AppData/Local/hokma/guard/x", "/cygdrive/c/Users/a/AppData/Local/hokma/bin/x", "/C/Users/a/AppData/Local/hokma/runtime/y",
                               "C:\\Users\\a\\AppData\\Local\\hokma\\guard\\x"])
def test_the_guard_sees_the_git_bash_spelling_of_a_protected_path(monkeypatch, p):
    _nt(monkeypatch)
    assert guard.under_any(p, PROTECT_W), p


@pytest.mark.parametrize("p", ["/dev/null", "/tmp/x", "/c/Users/a/Hokma/x", "/d/Users/a/AppData/Local/hokma/guard/x", "/cygdrive/d/x"])
def test_the_guard_leaves_free_what_is_not_protected(monkeypatch, p):
    _nt(monkeypatch)
    assert not guard.under_any(p, PROTECT_W), p

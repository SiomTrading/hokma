"""M9 and the key rules (P13, C3, F6 where it applies): a saved key exists only in the system credential store. It is
not on disk under the data folder, not in the environment of the `claude` process, not in a WebSocket message, an
HTTP answer or a log line, and the always-on protections stop an agent from reading it."""
import json
import logging
import os

import pytest

from hokma.core.config import hokma_data_dir
from hokma.engine.session import SECRET_ENV
from hardblock_support import Box, box  # noqa: F401  (fixture)
from live_fixtures import AUTH, Collector, live  # noqa: F401  (fixture)
from live_support import FakeDatabento, FakeWs, alpaca_script, finnhub_script, massive_script
from market_support import RECORDED_AT
from test_live_market import KEY_ID, SECRET, iso, subscribe

FINNHUB = "finnhub-key-value-13579"
MASSIVE = "massive-key-24680"
DATABENTO = "db-" + "q" * 29
SECRETS = (KEY_ID, SECRET, FINNHUB, MASSIVE, DATABENTO)


class Recorder:
    """Every HTTP answer the test client receives, to search them all afterwards."""

    def __init__(self, client) -> None:
        self.client, self.texts = client, []

    def __getattr__(self, name):
        call = getattr(self.client, name)

        def wrapped(*args, **kwargs):
            response = call(*args, **kwargs)
            self.texts.append(response.text + json.dumps(dict(response.headers)))
            return response
        return wrapped


def test_m9_a_saved_key_is_nowhere_but_the_system_store(live, caplog, capfd):
    caplog.set_level(logging.DEBUG)
    logging.getLogger().setLevel(logging.DEBUG)
    alpaca = FakeWs(alpaca_script(KEY_ID, SECRET)).start()
    finnhub = FakeWs(finnhub_script, reject_token=FINNHUB).start()
    massive = FakeWs(massive_script(MASSIVE)).start()
    gateway = FakeDatabento(DATABENTO).start()
    try:
        live.use({"alpaca": alpaca.url, "finnhub": finnhub.url, "massive": massive.url,
                  "databento": f"tcp://127.0.0.1:{gateway.port}"})
        with live.app() as client, live.connect(client) as ws:
            http = Recorder(client)
            c = Collector(ws)
            http.put("/api/connections/alpaca/key", json={"key_id": KEY_ID, "secret": SECRET}, headers=AUTH)
            http.put("/api/connections/finnhub/key", json={"key": FINNHUB}, headers=AUTH)
            http.put("/api/connections/massive/key", json={"key": MASSIVE}, headers=AUTH)
            http.put("/api/connections/databento/key", json={"key": DATABENTO}, headers=AUTH)
            assert subscribe(c, "a", "AAPL", "5m")["source"] == "Alpaca IEX"
            alpaca.send_all([{"T": "t", "S": "AAPL", "p": 351.0, "s": 5, "t": iso(int(RECORDED_AT * 1000) + 1000)}])
            c.find(lambda m: m["type"] == "bar")
            for provider in ("alpaca", "finnhub", "massive", "databento"):
                http.post(f"/api/connections/{provider}/test", headers=AUTH)
            http.get("/api/connections", headers=AUTH)
            http.put("/api/connections/finnhub/key", json={"key": "bad key"}, headers=AUTH)
            http.put("/api/connections/alpaca/enabled", json={"enabled": True}, headers=AUTH)
            http.put("/api/connections/priority", headers=AUTH,
                     json={"asset_class": "equity", "order": ["finnhub", "alpaca", "databento"]})
            c.drain(0.5)
            socket_text = json.dumps([{k: v for k, v in m.items() if k != "_at"} for m in c.all])
            everything = {"websocket": socket_text, "http": "\n".join(http.texts), "logs": caplog.text,
                          "output": "".join(capfd.readouterr())}
            for where, text in everything.items():
                for secret in SECRETS:
                    assert secret not in text, f"{secret!r} found in {where}"
            for path in hokma_data_dir().rglob("*"):
                if path.is_file():
                    body = path.read_bytes()
                    for secret in SECRETS:
                        assert secret.encode() not in body, f"{secret!r} found in {path}"
            for value in os.environ.values():
                for secret in SECRETS:
                    assert secret not in value
            # what the engine hands to the `claude` process: the environment minus the session secrets
            claude_env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
            assert not [v for v in claude_env.values() if any(s in v for s in SECRETS)]
            state = json.loads((hokma_data_dir() / "connections.json").read_text())
            assert set(state) == {"version", "enabled", "priority"}
            assert all(isinstance(v, bool) for v in state["enabled"].values())
            assert all(isinstance(v, str) for order in state["priority"].values() for v in order)
        stored = {name: live.keyring.items[("hokma-market", name)] for name in ("alpaca", "finnhub", "massive", "databento")}
        assert json.loads(stored["alpaca"]) == {"key_id": KEY_ID, "secret": SECRET}
        assert stored["finnhub"] == FINNHUB and stored["massive"] == MASSIVE and stored["databento"] == DATABENTO
    finally:
        alpaca.stop()
        finnhub.stop()
        massive.stop()
        gateway.stop()


def test_f6_the_key_is_not_in_the_url_of_any_provider_that_takes_it_in_a_message(live):
    alpaca = FakeWs(alpaca_script(KEY_ID, SECRET)).start()
    massive = FakeWs(massive_script(MASSIVE)).start()
    try:
        live.use({"alpaca": alpaca.url, "massive": massive.url})
        with live.app() as client:
            client.put("/api/connections/alpaca/key", json={"key_id": KEY_ID, "secret": SECRET}, headers=AUTH)
            client.put("/api/connections/massive/key", json={"key": MASSIVE}, headers=AUTH)
            client.post("/api/connections/alpaca/test", headers=AUTH)
            client.post("/api/connections/massive/test", headers=AUTH)
            for fake in (alpaca, massive):
                assert fake.paths and not any(s in p for p in fake.paths for s in SECRETS)
                assert not any("?" in p for p in fake.paths)
    finally:
        alpaca.stop()
        massive.stop()


def test_the_hub_and_service_never_put_a_key_in_the_process_environment(live):
    before = dict(os.environ)
    finnhub = FakeWs(finnhub_script, reject_token=FINNHUB).start()
    try:
        live.use({"finnhub": finnhub.url})
        with live.app() as client:
            client.put("/api/connections/finnhub/key", json={"key": FINNHUB}, headers=AUTH)
            client.post("/api/connections/finnhub/test", headers=AUTH)
        assert dict(os.environ) == before
    finally:
        finnhub.stop()


# ------------------------------------------------------------------ P13: the always-on protections


READERS = [
    "python -c \"import keyring; print(keyring.get_password('hokma-market','alpaca'))\"",
    "python3 -m keyring get hokma-market alpaca", "keyring get hokma-market finnhub",
    "uv run python -c 'import keyring as k; print(k.get_password(\"hokma-market\", \"alpaca\"))'",
    "secret-tool lookup service hokma-market", "secret-tool search --all service hokma-market",
    "gdbus call --session --dest org.freedesktop.secrets --object-path /org/freedesktop/secrets "
    "--method org.freedesktop.Secret.Service.SearchItems '{}'",
    "dbus-send --session --dest=org.freedesktop.secrets --print-reply /org/freedesktop/secrets "
    "org.freedesktop.Secret.Service.GetSecrets",
    "busctl --user call org.freedesktop.secrets /org/freedesktop/secrets org.freedesktop.Secret.Service SearchItems a{ss} 0",
    "cat ~/.local/share/keyrings/login.keyring", "ls ~/.local/share/keyrings",
    "cat ~/.local/share/kwalletd/kdewallet.kwl", "kwallet-query -r hokma-market kdewallet",
    "security find-generic-password -s hokma-market -w", "security dump-keychain -d login.keychain",
    "cat ~/Library/Keychains/login.keychain-db", "cmdkey /list", "cmdkey /list:hokma-market", "vaultcmd /list",
    "grep -r hokma-market ~/.local", "pass show hokma-market/alpaca",
    "node -e \"require('keytar').getPassword('hokma-market','alpaca')\"",
    "python -c \"import dbus; dbus.SessionBus().get_object('org.freedesktop.secrets','/org/freedesktop/secrets')\"",
]


@pytest.mark.parametrize("command", READERS)
def test_p13_no_command_can_read_the_connections_keys(box, command):
    assert box.bash(command) == "secrets"


@pytest.mark.parametrize("command", [
    "cmdkey /list", "Get-StoredCredential -Target hokma-market", "vaultcmd /listcreds:x",
    "(New-Object Windows.Security.Credentials.PasswordVault).Retrieve('hokma-market','alpaca')",
    "Get-Content $env:APPDATA\\Microsoft\\Credentials\\x", "Get-ChildItem $env:LOCALAPPDATA\\Microsoft\\Vault"])
def test_p13_the_windows_readers_are_stopped_in_powershell(box, command):
    assert box.ps(command) == "secrets"


def test_p13_a_script_in_the_working_folder_that_reads_the_store_is_stopped(box):
    (box.work / "peek.py").write_text("import keyring\nprint(keyring.get_password('hokma-market', 'alpaca'))\n")
    assert box.bash("python peek.py") == "secrets"
    assert box.hit("Read", file_path=str(box.data / "connections.json")) == "hokma_internal"
    assert box.hit("Read", file_path="/home/u/.local/share/keyrings/login.keyring") == "secrets"
    assert box.hit("Read", file_path="/home/u/Library/Keychains/login.keychain-db") == "secrets"


@pytest.mark.parametrize("command", [
    "ls", "git status", "pip install requests", "python -m pytest", "cat notes.md",
    "echo microsoft credentials report > notes.md", "python -c \"print('secret santa list')\"",
    "npm install keyboard"])
def test_p13_normal_work_is_not_stopped_by_the_new_markers(box, command):
    assert box.bash(command) is None

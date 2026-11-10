"""Connections: saving, the real test turn, region diagnosis, doctor rows, per-conversation engine (CA-01, CA-17, CA-17b)."""
import asyncio
import json

import keyring
import pytest
from keyring.backends import fail

from dbsupport import env  # noqa: F401  (fixture)
from hokma.db import get_database, migrate
from hokma.doctor import checks
from hokma.engine.dbstore import DbChatStore
from hokma.engines import secrets, service
from hokma.engines.service import ServiceError
from test_engine_direct import Server, oa_text  # noqa: F401


class MemoryKeyring(keyring.backend.KeyringBackend):
    priority = 5

    def __init__(self):
        self.data = {}

    def get_password(self, service_name, username):
        return self.data.get((service_name, username))

    def set_password(self, service_name, username, password):
        self.data[(service_name, username)] = password

    def delete_password(self, service_name, username):
        self.data.pop((service_name, username), None)


@pytest.fixture
def ready(env):
    ring = MemoryKeyring()
    keyring.set_keyring(ring)
    migrate()
    yield ring
    keyring.set_keyring(fail.Keyring())


def test_a_key_goes_to_the_secure_store_and_never_to_the_view(ready):
    view = service.add_api("custom", "sk-secret-123", label="Mine", base_url="http://127.0.0.1:9/v1")
    assert view["has_secret"] is True and "sk-secret-123" not in json.dumps(view)
    assert ready.data[("hokma-engine", str(view["id"]))] == "sk-secret-123"
    assert "sk-secret-123" not in json.dumps(service.list_connections())
    assert view["is_default"] is True                               # the first connection is the default
    service.remove(view["id"])
    assert ("hokma-engine", str(view["id"])) not in ready.data
    assert service.list_connections() == []


def test_without_a_secure_store_nothing_is_saved(env):
    keyring.set_keyring(fail.Keyring())
    migrate()
    with pytest.raises(ServiceError) as info:
        service.add_api("custom", "k", base_url="http://127.0.0.1:9/v1")
    assert info.value.code == "engines.store_unavailable" and service.list_connections() == []


def test_validation(ready):
    for args in (("nope", "k"), ("custom", "")):
        with pytest.raises(ServiceError):
            service.add_api(*args, base_url="http://127.0.0.1:9/v1")
    with pytest.raises(ServiceError):
        service.add_api("custom", "k", base_url="ftp://x")


def test_ping_ok_marks_connected(ready, env):
    srv = Server([oa_text("OK")])
    try:
        view = service.add_api("custom", "k", base_url=srv.url, model="m1")
        result = asyncio.run(service.ping(view["id"]))
    finally:
        srv.stop()
    assert result["ok"] and result["status"] == "connected"
    assert service.list_connections()[0]["status"] == "connected"


def test_a_region_refusal_is_saved_as_region(ready, env):
    srv = Server([], status=403, error_body="Country, region, or territory not supported")
    try:
        view = service.add_api("custom", "k", base_url=srv.url, model="m1")
        result = asyncio.run(service.ping(view["id"]))
    finally:
        srv.stop()
    assert (result["ok"], result["status"], result["code"]) == (False, "error", "region")
    row = service.list_connections()[0]
    assert row["status_detail"] == "region"
    rows = checks.check_engines()
    assert len(rows) == 1 and rows[0].status == checks.WARN and "Settings" in rows[0].detail


def test_a_bad_key_is_no_session_and_quota_is_error(ready, env):
    for status, body, expected in ((401, "invalid api key", ("no_session", "auth")), (429, "slow", ("error", "quota"))):
        srv = Server([], status=status, error_body=body)
        try:
            view = service.add_api("custom", "k", label=f"c{status}", base_url=srv.url, model="m1")
            result = asyncio.run(service.ping(view["id"]))
        finally:
            srv.stop()
        assert (result["status"], result["code"]) == expected


def test_no_connection_is_a_warning_not_a_failure(ready):
    rows = checks.check_engines()
    assert [r.status for r in rows] == [checks.WARN]


def test_new_conversations_use_the_default_connection_and_can_switch(ready):
    one = service.add_api("custom", "k", label="One", base_url="http://127.0.0.1:9/v1")
    two = service.add_api("custom", "k", label="Two", base_url="http://127.0.0.1:9/v1")
    store = DbChatStore()
    conv = store.create_conversation("t", "sonnet", None, None)
    assert store.connection_for(conv.id)["id"] == one["id"]
    store.set_connection(conv.id, two["id"])
    assert store.connection_for(conv.id)["id"] == two["id"]
    assert store.get_conversation(conv.id).engine_session is None


def test_a_removed_connection_stops_the_conversation_with_a_clear_error(ready):
    from hokma.engine.engine import ChatEngine
    from hokma.engine.session import TurnError
    one = service.add_api("custom", "k", base_url="http://127.0.0.1:9/v1")
    store = DbChatStore()
    conv = store.create_conversation("t", "sonnet", None, None)
    service.remove(one["id"])
    engine = ChatEngine(store=store)
    with pytest.raises(TurnError) as info:
        engine.resolve_engine(store.get_conversation(conv.id))
    assert info.value.code == "connection_removed"


def test_the_claude_import_shows_names_and_host_never_values(ready, env, monkeypatch):
    home = env / "home" / ".claude"
    home.mkdir(parents=True)
    (home / "settings.json").write_text(json.dumps({"env": {
        "ANTHROPIC_BASE_URL": "https://gw.example.com/v1", "ANTHROPIC_AUTH_TOKEN": "tok-SECRET", "OTHER": "x"}}))
    preview = service.claude_import_preview()
    assert preview["found"] and preview["host"] == "gw.example.com"
    assert set(preview["variables"]) == {"ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"}
    assert "tok-SECRET" not in json.dumps(preview)
    view = service.claude_import_apply()
    assert "tok-SECRET" not in json.dumps(view)
    env_vars, helper = service.claude_env_for({"id": view["id"], "auth_kind": "gateway"})
    assert "ANTHROPIC_AUTH_TOKEN" not in env_vars and "OTHER" not in env_vars
    assert env_vars["ANTHROPIC_BASE_URL"] == "https://gw.example.com/v1"
    assert "tok-SECRET" not in json.dumps(env_vars) and "--var ANTHROPIC_AUTH_TOKEN" in helper
    assert not any("tok-SECRET" in p.read_text(errors="ignore") for p in (env / "data").rglob("*") if p.is_file())


def test_a_plain_claude_key_goes_by_api_key_helper_not_the_environment(ready):
    view = service.add_cli("claude_code", "api_key", "Claude key", key="sk-ant-abc")
    env_vars, helper = service.claude_env_for({"id": view["id"], "auth_kind": "api_key"})
    assert env_vars == {} and "engine_key.py" in helper and f"--conn {view['id']}" in helper
    assert secrets.get(view["id"]) == "sk-ant-abc"


def test_engine_key_helper_prints_the_secret_of_an_imported_setup(ready, env, capfd):
    from hokma.kit import engine_key
    view = service.add_cli("claude_code", "api_key", "K", key="sk-ant-abc")
    assert engine_key.main(["--conn", str(view["id"])]) == 0
    assert capfd.readouterr().out == "sk-ant-abc"
    ready.data[("hokma-engine", "77")] = json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "tok-1"}})
    assert engine_key.main(["--conn", "77", "--var", "ANTHROPIC_AUTH_TOKEN"]) == 0
    assert capfd.readouterr().out == "tok-1"
    assert engine_key.main(["--conn", "77", "--var", "NOPE"]) == 1

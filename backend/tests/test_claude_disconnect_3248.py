"""Task 3248: Disconnect on the Claude card is the student's durable intent (no `claude auth logout`, no migration).
The removed row IS the record; Connect revives that same row. Bug 1128: an unreadable CLI is 'unknown', not 'no_session'."""
import subprocess

import keyring
import pytest
from keyring.backends import fail

from dbsupport import env  # noqa: F401  (fixture)
from hokma.db import get_database, migrate
from hokma.db import database as dbmod
from hokma.engine.dbstore import DbChatStore
from hokma.engine.engine import ChatEngine
from hokma.engines import effective, service
from hokma.setup import claude as claude_setup


@pytest.fixture
def ready(env):
    previous = keyring.get_keyring()
    migrate()
    with get_database().write() as u:
        u.profile.save(owner_name="Ana", workdir=str(env / "work"))
        u.profile.set_claude_status("connected", None)
    yield
    keyring.set_keyring(previous)       # never leave a global backend behind for the next test


def programs(monkeypatch, *present):
    monkeypatch.setattr(effective, "installed", lambda kind, path=None: kind in present)


def restart():
    """A new process over the same file: the singleton is dropped and opened again."""
    dbmod._singleton = None
    dbmod._singleton_key = None
    get_database()


def gemini_connected():
    view = service.add_cli("gemini_cli", "subscription", "Gemini")
    with get_database().write() as u:
        u.engine_connections.set_status(view["id"], "connected", None)
    return view["id"]


def test_flag_is_off_with_no_claude_row_and_with_a_live_one(ready, monkeypatch):
    programs(monkeypatch, "claude_code")
    assert effective.claude_disabled_by_user() is False
    service.ensure_claude_connection()
    assert effective.claude_disabled_by_user() is False
    assert effective.claude_connected() is True


def test_disconnect_sets_the_flag_and_it_survives_a_restart(ready, monkeypatch):
    programs(monkeypatch, "claude_code")
    row = service.ensure_claude_connection()
    service.remove(row["id"])
    assert effective.claude_disabled_by_user() is True and effective.claude_connected() is False
    restart()
    assert effective.claude_disabled_by_user() is True and effective.claude_connected() is False


def test_flag_is_false_when_the_database_cannot_be_read(ready, monkeypatch):
    def boom():
        raise RuntimeError("no database")

    monkeypatch.setattr("hokma.db.get_database", boom)
    assert effective.claude_disabled_by_user() is False


def test_after_disconnect_the_chat_and_the_model_button_use_another_verified_engine(ready, monkeypatch):
    programs(monkeypatch, "claude_code", "gemini_cli")
    claude = service.ensure_claude_connection()
    service.set_default(claude["id"])
    gemini = gemini_connected()
    assert service.active_engine()["engine"] == "claude_code"
    service.remove(claude["id"])
    shown = service.active_engine()
    assert shown["engine"] == "gemini_cli" and shown["id"] == gemini
    assert [g["engine"] for g in service.model_menu()] == ["gemini_cli"]            # Claude is not listed
    store = DbChatStore()
    conv = store.create_conversation("t", "haiku", None, None)
    adapter, connection, _ = ChatEngine(store=store, claude_bin="/bin/true").resolve_engine(store.get_conversation(conv.id))
    assert adapter.kind == "gemini_cli" and connection["id"] == gemini               # even with the program given to the engine


def test_after_disconnect_with_nothing_else_the_screen_says_nothing_can_answer(ready, monkeypatch):
    programs(monkeypatch, "claude_code")
    service.remove(service.ensure_claude_connection()["id"])
    assert service.active_engine()["available"] is False
    assert service.model_menu() == []


def test_connect_revives_the_same_row_and_the_flag_falls(ready, monkeypatch):
    programs(monkeypatch, "claude_code")
    first = service.ensure_claude_connection()
    service.remove(first["id"])
    assert effective.claude_disabled_by_user() is True
    again = service.ensure_claude_connection()
    assert again["id"] == first["id"] and again["status"] == "connected"
    with get_database().read() as u:
        assert len(u.engine_connections.list(include_removed=True)) == 1               # no second row
    assert effective.claude_disabled_by_user() is False and effective.claude_connected() is True
    assert service.active_engine()["engine"] == "claude_code"
    restart()
    assert effective.claude_disabled_by_user() is False


def test_a_removed_codex_or_api_row_does_not_set_the_flag(ready, monkeypatch):
    programs(monkeypatch, "claude_code", "gemini_cli")
    service.ensure_claude_connection()
    service.remove(gemini_connected())
    assert effective.claude_disabled_by_user() is False


def test_the_listing_carries_the_flag(ready, monkeypatch):
    import asyncio

    from hokma.core import app as core_app  # noqa: F401
    from hokma.engines import api as engines_api

    programs(monkeypatch, "claude_code")
    monkeypatch.setattr(engines_api, "_guard", lambda *a, **k: None)
    monkeypatch.setattr(service, "detect", lambda: {})
    monkeypatch.setattr(engines_api.secrets, "available", lambda: True)
    body = asyncio.run(engines_api.listing("", "", None))
    assert body["claude_disabled_by_user"] is False
    service.remove(service.ensure_claude_connection()["id"])
    body = asyncio.run(engines_api.listing("", "", None))
    assert body["claude_disabled_by_user"] is True


# ------------------------------------------------------------------ bug 1128: unknown is not "no session"

@pytest.fixture
def cli(env, monkeypatch):
    monkeypatch.setattr(claude_setup, "claude_path", lambda: "/fake/claude")
    monkeypatch.setattr(claude_setup, "_version", lambda path: "1.0")
    return monkeypatch


def test_a_timeout_is_unknown_not_a_sign_out(cli):
    def run(*a, **k):
        raise subprocess.TimeoutExpired("claude", 15)

    cli.setattr(claude_setup.subprocess, "run", run)
    state = claude_setup.detect()
    assert state.state == "unknown" and state.installed is True


def test_an_oserror_is_unknown(cli):
    def run(*a, **k):
        raise OSError("cannot run")

    cli.setattr(claude_setup.subprocess, "run", run)
    assert claude_setup.detect().state == "unknown"


def test_output_that_is_not_json_is_unknown(cli):
    cli.setattr(claude_setup.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="oops", stderr=""))
    assert claude_setup.detect().state == "unknown"


def test_a_real_logged_out_answer_is_still_no_session(cli):
    cli.setattr(claude_setup.subprocess, "run",
                lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout='{"loggedIn": false}', stderr=""))
    assert claude_setup.detect().state == "no_session"


def test_unknown_still_counts_as_usable_for_the_chat(ready, monkeypatch):
    programs(monkeypatch, "claude_code")
    with get_database().write() as u:
        u.profile.set_claude_status("unknown", None)
    assert effective.claude_connected() is True


def test_after_disconnect_with_nothing_else_the_chat_does_not_launch_claude(ready, monkeypatch):
    from hokma.engine.session import TurnError

    programs(monkeypatch, "claude_code")
    service.remove(service.ensure_claude_connection()["id"])
    store = DbChatStore()
    conv = store.create_conversation("t", "haiku", None, None)
    with pytest.raises(TurnError) as raised:
        ChatEngine(store=store, claude_bin="/bin/true").resolve_engine(store.get_conversation(conv.id))
    assert raised.value.code == "claude_disabled"                                    # not the generic "no engine"
    service.ensure_claude_connection()                                               # Connect: Claude answers again
    adapter, _, _ = ChatEngine(store=store, claude_bin="/bin/true").resolve_engine(store.get_conversation(conv.id))
    assert adapter.kind == "claude_code"


# ------------------------------------------------------------------ bug 1159: conversations tied to the removed row

def attached_conversation(row_id):
    store = DbChatStore()
    conv = store.create_conversation("t", "sonnet", None, None)
    with get_database().write() as u:
        u.engine_connections.attach(int(conv.id), row_id)
    return store, conv


def connection_of(conv):
    with get_database().read() as u:
        return u.conversations.find(int(conv.id)).connection_id


def test_disconnect_unlinks_the_conversations_and_connect_makes_them_answer_with_claude_again(ready, monkeypatch):
    from hokma.engine.session import TurnError

    programs(monkeypatch, "claude_code")
    row = service.ensure_claude_connection()
    store, conv = attached_conversation(row["id"])
    service.remove(row["id"])
    assert connection_of(conv) is None
    engine = ChatEngine(store=store, claude_bin="/bin/true")
    with pytest.raises(TurnError) as raised:
        engine.resolve_engine(store.get_conversation(conv.id))
    assert raised.value.code == "claude_disabled"                 # never "connection_removed" for a Claude row
    service.ensure_claude_connection()
    adapter, _, _ = engine.resolve_engine(store.get_conversation(conv.id))
    assert adapter.kind == "claude_code"


def test_a_removed_claude_row_still_attached_counts_as_claude_without_a_row(ready, monkeypatch):
    programs(monkeypatch, "claude_code", "gemini_cli")
    row = service.ensure_claude_connection()
    gemini = gemini_connected()
    store, conv = attached_conversation(row["id"])
    with get_database().write() as u:
        u.engine_connections.remove(row["id"])                    # removed without the unlink (older data)
    shown = service.active_engine(str(conv.id))
    assert shown["engine"] == "gemini_cli" and shown["id"] == gemini
    assert [g["engine"] for g in service.model_menu()] == ["gemini_cli"]


def test_with_the_flag_and_nothing_else_the_button_says_so_for_a_linked_conversation(ready, monkeypatch):
    programs(monkeypatch, "claude_code")
    row = service.ensure_claude_connection()
    store, conv = attached_conversation(row["id"])
    service.remove(row["id"])
    assert service.active_engine(str(conv.id))["available"] is False
    assert service.model_menu() == []


def test_the_removed_error_has_no_raw_english_text(ready):
    from hokma.engine.session import TurnError

    store = DbChatStore()
    with get_database().write() as u:
        gone = u.engine_connections.add("codex", "Old", "subscription", auto_default=False)
        u.engine_connections.set_status(gone.id, "connected", None)
    conv = store.create_conversation("t", "haiku", None, None)
    with get_database().write() as u:
        u.engine_connections.attach(int(conv.id), gone.id)
        u.engine_connections.remove(gone.id)
    with pytest.raises(TurnError) as raised:
        ChatEngine(store=store, claude_bin=None).resolve_engine(store.get_conversation(conv.id))
    assert raised.value.code == "connection_removed" and "pick another" not in str(raised.value)


# ------------------------------------------------------------------ bug 1158: Gemini row is idempotent

def test_adding_gemini_twice_makes_one_row(ready, monkeypatch):
    programs(monkeypatch, "claude_code", "gemini_cli")
    first = service.add_cli("gemini_cli", "subscription", "Gemini")
    second = service.add_cli("gemini_cli", "subscription", "Gemini")
    assert first["id"] == second["id"]
    assert len([c for c in service.list_connections() if c["engine"] == "gemini_cli"]) == 1


# ------------------------------------------------------------------ bug 1162: empty label

def test_an_empty_label_is_refused_and_the_old_one_stays(ready, monkeypatch):
    programs(monkeypatch, "claude_code")
    row = service.ensure_claude_connection()
    with pytest.raises(service.ServiceError) as raised:
        service.rename(row["id"], "   ")
    assert raised.value.status == 422 and raised.value.code == "engines.bad_request"
    assert service.rename(row["id"], "Mi Claude")["label"] == "Mi Claude"


# ------------------------------------------------------------------ bug 1160: an expired session is not "Claude is not installed"

def test_a_claude_row_answering_for_claude_without_a_row_is_not_a_replacement():
    row = {"id": 7, "engine": "claude_code", "auth_kind": "subscription", "label": "Claude", "status": "connected"}
    chosen = effective.resolve(None, [row], is_installed=lambda kind: True, claude_ok=False)   # installed, session expired
    assert chosen.conn == row and chosen.fallback_from is None


def test_a_real_replacement_is_still_announced():
    gemini = {"id": 8, "engine": "gemini_cli", "auth_kind": "subscription", "label": "Gemini", "status": "connected"}
    chosen = effective.resolve(None, [gemini], is_installed=lambda kind: kind == "gemini_cli", claude_ok=False)
    assert chosen.conn == gemini and chosen.fallback_from is not None


# ------------------------------------------------------------------ bug 1162: login failures

def test_a_failed_login_start_is_a_502_and_a_finished_login_has_no_link(env, monkeypatch):
    class Dead:
        url = "https://claude.ai/oauth/old"
        proc = None

    monkeypatch.setattr(claude_setup, "_login", Dead())
    monkeypatch.setattr(claude_setup, "_alive", lambda login: False)
    monkeypatch.setattr(claude_setup, "_failure", lambda login: None)
    info = claude_setup.login_info()
    assert info["running"] is False and info["url"] is None
    monkeypatch.setattr(claude_setup, "_login", None)


def test_the_cli_failure_status_is_not_a_500():
    import inspect

    source = inspect.getsource(claude_setup.start_login)
    assert "SetupError(502, failure)" in source and "SetupError(500, failure)" not in source

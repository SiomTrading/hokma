"""Bug 1095 (reopened): Claude connected, Gemini saved, and every message went to Gemini ("the gemini command is not
installed"). The chat and the screen must use the engine that is really there, say so, and never lose the choice."""
import keyring
import pytest
from keyring.backends import fail

from dbsupport import env  # noqa: F401  (fixture)
from hokma.db import get_database, migrate
from hokma.engine.dbstore import DbChatStore
from hokma.engine.engine import ChatEngine
from hokma.engine.session import TurnError
from hokma.engines import effective, service
from hokma.setup.claude import ClaudeState


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
    keyring.set_keyring(MemoryKeyring())
    migrate()
    with get_database().write() as u:
        u.profile.save(owner_name="Ana", workdir=str(env / "work"))
    yield
    keyring.set_keyring(fail.Keyring())


def programs(monkeypatch, *present):
    """Only these programs are installed: 'claude_code', 'gemini_cli', 'codex'."""
    monkeypatch.setattr(effective, "installed", lambda kind, path=None: kind in present)


def claude_status(status):
    with get_database().write() as u:
        u.profile.set_claude_status(status, None)


def saved_gemini_default():
    """The state of the student: a Gemini row, the default, which every new conversation got attached to."""
    view = service.add_cli("gemini_cli", "subscription", "Gemini")
    service.set_default(view["id"])
    return view["id"]


def test_claude_connected_and_gemini_saved_the_chat_uses_claude_and_says_so(ready, monkeypatch):
    programs(monkeypatch, "claude_code")
    claude_status("connected")
    gemini = saved_gemini_default()
    store = DbChatStore()
    conv = store.create_conversation("t", "haiku", None, None)
    assert store.connection_for(conv.id)["id"] == gemini              # the conversation is attached to Gemini
    engine = ChatEngine(store=store)
    adapter, connection, _ = engine.resolve_engine(store.get_conversation(conv.id))
    assert adapter.kind == "claude_code" and connection is None       # it did NOT go to Gemini
    assert engine.fallbacks[conv.id] == {"from": "Gemini", "to": "Claude"}
    # the choice is untouched: it works again the moment Gemini is installed
    assert store.connection_for(conv.id)["id"] == gemini
    with get_database().read() as u:
        assert u.engine_connections.default().id == gemini


def test_a_restart_with_gemini_saved_resolves_the_same_way_and_the_screen_agrees(ready, monkeypatch):
    programs(monkeypatch, "claude_code")
    claude_status("connected")
    gemini = saved_gemini_default()
    first = DbChatStore().create_conversation("t", "haiku", None, None)
    # restart: new objects over the same database
    service.startup_check()
    store = DbChatStore()
    engine = ChatEngine(store=store)
    adapter, connection, _ = engine.resolve_engine(store.get_conversation(first.id))
    assert adapter.kind == "claude_code" and connection is None
    shown = service.active_engine(first.id)                           # what the pill shows
    assert shown["engine"] == "claude_code" and shown["replaced"] is True and shown["saved"] == "Gemini"
    assert shown["available"] is True
    assert service.active_engine()["engine"] == "claude_code"         # and for a new conversation
    assert store.connection_for(first.id)["id"] == gemini


def test_the_saved_engine_is_used_when_it_is_installed_and_connected(ready, monkeypatch):
    programs(monkeypatch, "claude_code", "gemini_cli")
    claude_status("connected")
    gemini = saved_gemini_default()
    with get_database().write() as u:
        u.engine_connections.set_status(gemini, "connected", None)
    store = DbChatStore()
    conv = store.create_conversation("t", "haiku", None, None)
    engine = ChatEngine(store=store)
    adapter, connection, _ = engine.resolve_engine(store.get_conversation(conv.id))
    assert adapter.kind == "gemini_cli" and connection["label"] == "Gemini" and not engine.fallbacks
    assert service.active_engine(conv.id)["replaced"] is False


def test_no_engine_connected_the_chat_says_it_plainly(ready, monkeypatch):
    programs(monkeypatch)                                              # nothing installed
    saved_gemini_default()
    store = DbChatStore()
    conv = store.create_conversation("t", "haiku", None, None)
    engine = ChatEngine(store=store)
    with pytest.raises(TurnError) as info:
        engine.resolve_engine(store.get_conversation(conv.id))
    assert info.value.code == "no_engine"
    shown = service.active_engine(conv.id)
    assert shown["available"] is False


def test_signed_out_claude_is_not_an_engine_that_is_there(ready, monkeypatch):
    programs(monkeypatch, "claude_code")
    claude_status("no_session")
    store = DbChatStore()
    conv = store.create_conversation("t", "haiku", None, None)
    with pytest.raises(TurnError) as info:
        ChatEngine(store=store).resolve_engine(store.get_conversation(conv.id))
    assert info.value.code == "no_engine"


def test_a_fake_claude_binary_given_to_the_engine_still_works(ready, monkeypatch):
    programs(monkeypatch)
    store = DbChatStore()
    conv = store.create_conversation("t", "haiku", None, None)
    adapter, connection, _ = ChatEngine(store=store, claude_bin="/bin/true").resolve_engine(store.get_conversation(conv.id))
    assert adapter.kind == "claude_code" and connection is None


def test_finishing_the_claude_sign_in_makes_it_answer_and_keeps_the_saved_choice(ready, monkeypatch):
    from hokma.setup.api import _remember_claude

    programs(monkeypatch, "claude_code")
    claude_status("no_session")
    gemini = saved_gemini_default()
    _remember_claude(ClaudeState("connected", True))
    with get_database().read() as u:
        assert u.engine_connections.default().id == gemini            # the student's choice is still saved
    shown = service.active_engine()
    assert shown["engine"] == "claude_code" and shown["replaced"] is True   # and Claude answers meanwhile
    programs(monkeypatch, "claude_code", "gemini_cli")                 # Gemini gets installed and tested later
    with get_database().write() as u:
        u.engine_connections.set_status(gemini, "connected", None)
    assert service.active_engine()["engine"] == "gemini_cli"           # the saved engine answers again


def test_the_sign_in_does_not_touch_a_saved_engine_that_works(ready, monkeypatch):
    from hokma.setup.api import _remember_claude

    programs(monkeypatch, "claude_code", "gemini_cli")
    gemini = saved_gemini_default()
    with get_database().write() as u:
        u.engine_connections.set_status(gemini, "connected", None)
    _remember_claude(ClaudeState("connected", True))
    with get_database().read() as u:
        assert u.engine_connections.default().id == gemini
    assert service.active_engine()["engine"] == "gemini_cli"


def test_a_new_connection_does_not_take_the_chat_from_a_signed_in_claude(ready, monkeypatch):
    programs(monkeypatch, "claude_code")
    claude_status("connected")
    view = service.add_cli("gemini_cli", "subscription", "Gemini")
    with get_database().read() as u:
        assert u.engine_connections.default() is None                  # was: the first row became the default
    assert service.active_engine()["engine"] == "claude_code"
    assert view["is_default"] is False


def test_a_new_connection_is_the_default_when_claude_cannot_be_used(ready, monkeypatch):
    programs(monkeypatch)
    view = service.add_cli("gemini_cli", "subscription", "Gemini")
    with get_database().read() as u:
        assert u.engine_connections.default().id == view["id"]


def test_with_several_valid_engines_the_last_choice_of_the_student_wins(ready, monkeypatch):
    programs(monkeypatch)                                              # Claude and Gemini are not there
    saved_gemini_default()
    first = service.add_api("custom", "k", label="First", base_url="http://127.0.0.1:9/v1")
    second = service.add_api("custom", "k", label="Second", base_url="http://127.0.0.1:9/v1")
    with get_database().write() as u:
        u.engine_connections.set_status(first["id"], "connected", None)
        u.engine_connections.set_status(second["id"], "connected", None)
        u.engine_connections.set_default(first["id"])                  # the last choice: First
        u.engine_connections.set_default(second["id"])
        u.engine_connections.set_default(first["id"])
        gemini_row = [c for c in u.engine_connections.list() if c.engine == "gemini_cli"][0]
        u.engine_connections.set_default(gemini_row.id)                # then Gemini, which cannot run
    store = DbChatStore()
    conv = store.create_conversation("t", "haiku", None, None)
    adapter, connection, _ = ChatEngine(store=store).resolve_engine(store.get_conversation(conv.id))
    assert adapter.kind == "api" and connection["label"] == "First"


# ------------------------------------------------------------------ the model button: every connected engine, with its models

def connected_gemini():
    view = service.add_cli("gemini_cli", "subscription", "Gemini")
    with get_database().write() as u:
        u.engine_connections.set_status(view["id"], "connected", None)
    return view["id"]


def test_only_gemini_connected_the_menu_has_no_claude(ready, monkeypatch):
    programs(monkeypatch, "gemini_cli")
    connected_gemini()
    groups = service.model_menu()
    assert [g["label"] for g in groups] == ["Gemini"]
    assert [m["name"] for m in groups[0]["models"]] == ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-2.5-pro"]   # the catalog
    assert [m["id"] for m in groups[0]["models"]] == ["haiku", "sonnet", "opus"]


def test_claude_and_chatgpt_connected_both_groups_are_listed_and_the_rest_are_not(ready, monkeypatch):
    programs(monkeypatch, "claude_code", "codex", "gemini_cli")
    claude_status("connected")
    codex = service.add_cli("codex", "subscription", "ChatGPT (Codex)")
    with get_database().write() as u:
        u.engine_connections.set_status(codex["id"], "connected", None)
    service.add_cli("gemini_cli", "subscription", "Gemini")                       # saved, never connected
    groups = service.model_menu()
    assert [g["label"] for g in groups] == ["Claude", "ChatGPT (Codex)"]
    assert groups[0]["connection_id"] is None and [m["name"] for m in groups[0]["models"]] == ["Haiku", "Sonnet", "Opus"]
    assert groups[1]["connection_id"] == codex["id"]


def test_an_engine_whose_program_is_gone_leaves_the_menu(ready, monkeypatch):
    programs(monkeypatch)
    connected_gemini()
    assert service.model_menu() == []


def test_the_list_follows_connecting_and_disconnecting_without_a_restart(ready, monkeypatch):
    programs(monkeypatch, "claude_code", "gemini_cli")
    claude_status("connected")
    assert [g["label"] for g in service.model_menu()] == ["Claude"]
    gemini = connected_gemini()
    assert [g["label"] for g in service.model_menu()] == ["Claude", "Gemini"]
    service.remove(gemini)
    assert [g["label"] for g in service.model_menu()] == ["Claude"]


def test_picking_a_model_of_another_engine_changes_the_engine_the_backend_uses(ready, monkeypatch):
    programs(monkeypatch, "claude_code", "gemini_cli")
    claude_status("connected")
    gemini = connected_gemini()
    store = DbChatStore()
    conv = store.create_conversation("t", "haiku", None, None)
    engine = ChatEngine(store=store)
    assert engine.resolve_engine(store.get_conversation(conv.id))[0].kind == "claude_code"
    # the button: Gemini's model, then Claude's again (Claude gets its saved connection the first time it is picked)
    store.set_connection(conv.id, gemini)
    adapter, connection, model = engine.resolve_engine(store.get_conversation(conv.id))
    assert adapter.kind == "gemini_cli" and connection["id"] == gemini and model == "gemini-2.5-flash-lite"
    claude = service.ensure_claude_connection()
    store.set_connection(conv.id, claude["id"])
    adapter, connection, _ = engine.resolve_engine(store.get_conversation(conv.id))
    assert adapter.kind == "claude_code" and connection["id"] == claude["id"]
    assert service.ensure_claude_connection()["id"] == claude["id"]                 # once, not one per click
    assert [g["label"] for g in service.model_menu()] == ["Claude", "Gemini"]      # and Claude is not listed twice


# ------------------------------------------------------------------ bug 1104: the button and the engine that answers are one rule

def test_claude_connected_gemini_installed_but_never_tested_the_menu_and_the_answer_agree(ready, monkeypatch):
    programs(monkeypatch, "claude_code", "gemini_cli")
    claude_status("connected")
    saved_gemini_default()                                            # installed, status unknown: never proved to work
    store = DbChatStore()
    conv = store.create_conversation("t", "haiku", None, None)
    engine = ChatEngine(store=store)
    adapter, connection, _ = engine.resolve_engine(store.get_conversation(conv.id))
    assert adapter.kind == "claude_code" and connection is None       # an untested engine does not take the chat from Claude
    shown = service.active_engine(conv.id)
    assert shown["label"] == "Claude" and shown["replaced"] is True and shown["saved"] == "Gemini"
    groups = service.model_menu()
    assert [g["label"] for g in groups] == ["Claude"]
    assert any(g["connection_id"] == shown["id"] for g in groups)     # the engine that answers is in the list


def test_the_engine_that_answers_is_always_in_the_menu(ready, monkeypatch):
    programs(monkeypatch, "gemini_cli")                               # no Claude; Gemini installed, never tested
    saved_gemini_default()
    shown = service.active_engine()
    assert shown["engine"] == "gemini_cli" and shown["available"] is True    # nothing better exists: it is tried
    assert [g["label"] for g in service.model_menu()] == ["Gemini"]


def test_nothing_at_all_the_screen_says_there_is_no_engine(ready, monkeypatch):
    programs(monkeypatch)
    saved_gemini_default()
    shown = service.active_engine()
    assert shown["available"] is False
    assert service.model_menu() == []


def test_the_replacement_is_announced_once_per_conversation_while_it_lasts(ready, monkeypatch):
    programs(monkeypatch, "claude_code")
    claude_status("connected")
    gemini = saved_gemini_default()
    store = DbChatStore()
    conv = store.create_conversation("t", "haiku", None, None)
    engine = ChatEngine(store=store)
    record = store.get_conversation(conv.id)
    engine.resolve_engine(record)
    assert engine.fallbacks.pop(conv.id) == {"from": "Gemini", "to": "Claude"}      # first message: announced
    engine.resolve_engine(record)
    engine.resolve_engine(record)
    assert conv.id not in engine.fallbacks                                           # the next ones: silence
    programs(monkeypatch, "claude_code", "gemini_cli")
    with get_database().write() as u:
        u.engine_connections.set_status(gemini, "connected", None)
    assert engine.resolve_engine(record)[0].kind == "gemini_cli" and conv.id not in engine.fallbacks
    programs(monkeypatch, "claude_code")                                             # Gemini breaks again
    engine.resolve_engine(record)
    assert engine.fallbacks.pop(conv.id) == {"from": "Gemini", "to": "Claude"}      # that is news again


def test_the_checks_and_the_adapters_look_for_programs_in_the_same_places():
    from hokma.engine import process
    from hokma.engines import acp, codex, registry

    assert acp.find_cli is process.find_cli and codex.find_cli is process.find_cli
    assert process.find_claude.__code__.co_names.count("find_cli") == 1
    import inspect

    assert "find_cli" in inspect.getsource(effective.installed) and "find_cli" in inspect.getsource(registry.installed_clis)


# ------------------------------------------------------------------ bug 1105: the last valid choice of the student, Claude one more of them

def connected_codex():
    view = service.add_cli("codex", "subscription", "ChatGPT (Codex)")
    with get_database().write() as u:
        u.engine_connections.set_status(view["id"], "connected", None)
    return view["id"]


def chosen(connection_id):
    with get_database().write() as u:
        u.engine_connections.mark_chosen(connection_id)


def answering(conv_id=None):
    store = DbChatStore()
    conv = store.get_conversation(conv_id) if conv_id else store.get_conversation(store.create_conversation("t", "haiku", None, None).id)
    adapter, connection, _ = ChatEngine(store=store).resolve_engine(conv)
    return adapter.kind, (connection or {}).get("label")


def test_claude_and_chatgpt_connected_a_broken_gemini_saved_the_student_chose_chatgpt_before_so_chatgpt_answers(ready, monkeypatch):
    programs(monkeypatch, "claude_code", "codex")
    claude_status("connected")
    codex = connected_codex()
    chosen(codex)                                                      # the student picked ChatGPT some day
    saved_gemini_default()                                             # and a Gemini that is not installed is saved
    assert answering() == ("codex", "ChatGPT (Codex)")                 # not Claude
    assert service.active_engine()["label"] == "ChatGPT (Codex)"
    assert [g["label"] for g in service.model_menu()] == ["Claude", "ChatGPT (Codex)"]


def test_the_student_never_chose_another_engine_so_claude_goes_first(ready, monkeypatch):
    programs(monkeypatch, "claude_code", "codex")
    claude_status("connected")
    connected_codex()                                                  # connected, never chosen by the student
    saved_gemini_default()
    assert answering() == ("claude_code", None)


def test_claude_chosen_after_chatgpt_wins_because_it_is_one_more_engine_in_the_order(ready, monkeypatch):
    programs(monkeypatch, "claude_code", "codex")
    claude_status("connected")
    codex = connected_codex()
    chosen(codex)
    claude = service.ensure_claude_connection()
    chosen(claude["id"])                                               # later: Claude
    saved_gemini_default()
    assert answering() == ("claude_code", "Claude")


def test_a_connection_test_or_a_status_change_does_not_count_as_a_choice(ready, monkeypatch):
    programs(monkeypatch, "claude_code", "codex")
    claude_status("connected")
    codex = connected_codex()
    other = service.add_cli("codex", "subscription", "Other ChatGPT")["id"]
    with get_database().write() as u:
        u.engine_connections.set_status(other, "connected", None)      # tested later than codex, never chosen
        u.engine_connections.mark_chosen(codex)
        u.engine_connections.set_status(other, "connected", None)      # tested again, still not a choice
        assert u.engine_connections.get(other).chosen_at is None
        assert u.engine_connections.get(other).updated_at >= u.engine_connections.get(codex).updated_at
    saved_gemini_default()
    assert answering() == ("codex", "ChatGPT (Codex)")                 # chosen_at decides, not updated_at


def test_a_default_that_hokma_picks_by_itself_is_not_a_choice(ready, monkeypatch):
    programs(monkeypatch)                                              # no Claude: the first connection becomes the default
    view = service.add_cli("gemini_cli", "subscription", "Gemini")
    with get_database().read() as u:
        assert u.engine_connections.default().id == view["id"]
        assert u.engine_connections.get(view["id"]).chosen_at is None
    service.set_default(view["id"])                                    # the student does it: now it is a choice
    with get_database().read() as u:
        assert u.engine_connections.get(view["id"]).chosen_at is not None


def test_attaching_an_engine_to_a_conversation_is_a_choice(ready, monkeypatch):
    programs(monkeypatch, "codex")
    codex = connected_codex()
    store = DbChatStore()
    conv = store.create_conversation("t", "haiku", None, None)
    with get_database().read() as u:
        assert u.engine_connections.get(codex).chosen_at is None
    store.set_connection(conv.id, codex)
    with get_database().read() as u:
        assert u.engine_connections.get(codex).chosen_at is not None


def test_the_migration_keeps_the_saved_default_as_the_last_choice_and_is_applied_once(env):
    import shutil

    from hokma.db.migrate import MIGRATIONS_DIR

    old = env / "migs-0008"
    old.mkdir()
    for path in MIGRATIONS_DIR.glob("*.sql"):
        if path.name < "0009":
            shutil.copy(path, old / path.name)
    assert migrate(migrations_dir=old).version == 8
    with get_database().write() as u:                                  # a student who already updated: two rows, one default
        u.session.execute("INSERT INTO engine_connections (engine, label, auth_kind, status, is_default) "
                          "VALUES ('gemini_cli', 'Gemini', 'subscription', 'unknown', 1)")
        u.session.execute("INSERT INTO engine_connections (engine, label, auth_kind, status, is_default) "
                          "VALUES ('codex', 'ChatGPT', 'subscription', 'connected', 0)")
    result = migrate()
    assert result.applied == [9, 10, 11, 12, 13] and result.version == 13
    with get_database().read() as u:
        rows = {c.label: c for c in u.engine_connections.list()}
        assert rows["Gemini"].chosen_at == rows["Gemini"].updated_at   # nothing lost: the default is their last choice
        assert rows["ChatGPT"].chosen_at is None
        assert rows["Gemini"].is_default is True and rows["ChatGPT"].status == "connected"
    again = migrate()
    assert again.applied == [] and again.version == 13                  # a second start changes nothing


def test_a_failing_environment_makes_an_engine_count_as_not_installed_and_never_breaks_the_screen(ready, monkeypatch, env):
    from hokma.engines import envfilter

    def broken(*args, **kwargs):
        raise OSError(448, "untrusted mount point")

    monkeypatch.setattr(envfilter, "filtered_env", broken)
    assert effective.installed("gemini_cli") is False                  # no exception
    saved_gemini_default()
    shown = service.active_engine()                                    # the endpoint answers
    assert shown["available"] is False
    assert service.model_menu() == []
    log = (env / "data" / "logs" / "engine.log").read_text(encoding="utf-8")
    assert "could not look for" in log and "448" in log

"""Hokma 0.0.36 Telegram hotfix (task 3248): bugs 1151 to 1157. Telegram is a fake (see test_telegram_glue)."""
import asyncio
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hokma.db import get_database
from hokma.telegram.api import create_router
from hokma.telegram.glue import TOKEN_ENTRY, TelegramGlue, api_base_override, build_application
from permissions_support import HEADERS
from test_telegram_glue import (TOKEN, FakeApp, FakeSession, MemoryKeys, aio, approvals, button,  # noqa: F401
                                env, linked, tap, text_update)
from test_telegram_topics import AVISOS, GROUP, group_update, grouped, say  # noqa: F401

OTHER = "987654321:" + "B" * 35


def client_for(glue):
    app = FastAPI()
    app.state.telegram = glue
    app.include_router(create_router())
    return TestClient(app)


def private_vincular_update(user_id=111, thread_id=None):
    chat = SimpleNamespace(id=int(GROUP), type="supergroup", is_forum=True, title="Hokma")
    msg = SimpleNamespace(text="/vincular", chat=chat, message_thread_id=thread_id, is_topic_message=bool(thread_id),
                          message_id=1)
    return SimpleNamespace(effective_message=msg, effective_chat=chat, message=msg,
                           effective_user=SimpleNamespace(id=user_id))


def lock_out(link, platform="telegram"):
    for _ in range(5):
        link._pairing.approve_code(platform, "WRONG000")


# ----- 1151: a bad token never deletes the one that works -----

def test_a_rejected_or_malformed_token_keeps_the_working_one(env):
    from telegram.error import InvalidToken

    calls = []

    async def factory(token):
        calls.append(token)
        if token == OTHER:
            raise InvalidToken("Unauthorized")
        return env.app

    env.glue.link._application_factory = factory
    with client_for(env.glue) as client:
        assert client.put("/api/telegram/token", json={"token": TOKEN}, headers=HEADERS).json() == {"connected": True}
        for bad in ("abc", "", "   "):
            r = client.put("/api/telegram/token", json={"token": bad}, headers=HEADERS)
            assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_token"
            assert env.glue.keys.items[TOKEN_ENTRY] == TOKEN and env.glue.link.connected
        r = client.put("/api/telegram/token", json={"token": OTHER}, headers=HEADERS)   # well formed, Telegram says no
        assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_token"
        assert env.glue.keys.items[TOKEN_ENTRY] == TOKEN
        assert calls[-1] == TOKEN and env.glue.link.connected              # and the bot is up again with it


# ----- 1152: two Connect at once leave one Application and one poller -----

def test_two_concurrent_starts_build_one_application_and_one_poller(env):
    built, polls = [], []

    class CountingApp(FakeApp):
        async def _poll(self, **kw):
            polls.append(1)
            await super()._poll(**kw)

        def __init__(self):
            super().__init__()
            self.updater.start_polling = self._poll

    async def factory(token):
        await asyncio.sleep(0)
        built.append(token)
        return CountingApp()

    async def run():
        env.glue.link._application_factory = factory
        await asyncio.gather(env.glue.link.start(TOKEN), env.glue.link.start(TOKEN))
        assert len(built) == 1 and len(polls) == 1
        env.glue.keys.set(TOKEN_ENTRY, TOKEN)
        await asyncio.gather(env.glue.start(), env.glue.start())
        assert len(built) == 1 and len(polls) == 1 and env.glue.link.connected
        await env.glue.link.start(OTHER)                                    # another token does restart
        assert len(built) == 2
        await env.glue.link.stop()

    asyncio.run(run())


# ----- 1153: one approvals row, with the channel it came from -----

def _real_session(env, cid):
    from hokma.engine.session import ChatSession

    async def broadcast(conversation_id, event):
        pass

    engine = SimpleNamespace(store=env.engine.store, permission_timeout=30, broadcast=broadcast)
    return ChatSession(engine, cid)


async def _permission(env, session, request_id):
    from hokma.engine.session import Pending

    class Eng:
        async def answer_permission(self, *a):
            pass

    pending = Pending(request_id, "u1", "Bash", {"command": "ls"}, {"kind": "run", "target": "ls"}, 1e12)
    session.pending[request_id] = pending
    waiter = asyncio.create_task(session._await_decision(Eng(), pending))
    return waiter


@aio
async def test_a_telegram_answer_writes_one_approval_with_the_telegram_channel(env):
    await linked(env)
    cid = env.glue._conversation(0, None)[0]
    session = env.engine.sessions[cid] = _real_session(env, cid)
    allow, deny, panel = [await _permission(env, session, r) for r in ("r1", "r2", "r3")]
    assert await env.glue.on_decision("r1", "once") == 1
    assert await env.glue.on_decision("r2", "deny") == 1
    assert await session.resolve("r3", "allow", "human") is True          # the panel's own answer
    await asyncio.gather(allow, deny, panel)
    rows = [(r["verdict"], r["channel"]) for r in approvals(env.db)]
    assert rows == [("approved", "telegram"), ("rejected", "telegram"), ("approved", "dashboard")]
    assert await env.glue.on_decision("r1", "once") == 0


# ----- 1154: lockout, a second /start and a code Telegram did not accept -----

@aio
async def test_a_lockout_says_how_long_in_the_bot_and_in_the_api(env):
    env.glue.keys.set(TOKEN_ENTRY, TOKEN)
    assert await env.glue.start()
    lock_out(env.glue.link)
    await env.glue.link._handle_text_message(text_update(222, "hola"))
    assert "Wait 60 min" in env.app.bot.sent[-1]["text"]
    with client_for(env.glue) as client:
        r = client.post("/api/telegram/approve", json={"code": "ABCDEFGH"}, headers=HEADERS)
        assert r.status_code == 429 and r.json()["detail"]["code"] == "locked" and "60 min" in r.json()["detail"]["message"]


@aio
async def test_a_second_start_is_answered_and_a_failed_send_does_not_burn_the_code(env):
    env.glue.keys.set(TOKEN_ENTRY, TOKEN)
    assert await env.glue.start()
    real_send, fail = env.app.bot.send_message, [True]

    async def flaky(**kw):
        if fail[0]:
            raise RuntimeError("network down")
        return await real_send(**kw)

    env.app.bot.send_message = flaky
    await env.glue.link._handle_text_message(text_update(222, "hola"))      # the send fails: nothing is spent
    assert env.glue.link._pairing.list_approved("telegram") == [] and env.app.bot.sent == []
    fail[0] = False
    await env.glue.link._handle_text_message(text_update(222, "hola"))
    assert "pairing code is" in env.app.bot.sent[-1]["text"]
    await env.glue.link._handle_text_message(text_update(222, "hola"))      # within 10 minutes: told, not ignored
    assert "already sent you a code" in env.app.bot.sent[-1]["text"] and len(env.app.bot.sent) == 2


# ----- 1155: the permission says where it comes from and goes there -----

@aio
async def test_permissions_go_to_their_topic_or_the_private_chat_and_name_it(env):
    await grouped(env)
    env.glue.link._topics.update({10: "Daniel", 11: "Joel"})
    env.engine.script = [{"type": "permission_request", "turn_id": "t1", "request_id": "r1", "tool": "Bash",
                          "input": {"command": "ls"}, "summary": {"kind": "run", "target": "ls"}}]
    await say(env, "uno", 10)
    first = env.app.bot.sent[-1]
    await say(env, "dos", 11)
    second = env.app.bot.sent[-1]
    await env.glue.link._handle_text_message(text_update(111, "tres"))
    third = env.app.bot.sent[-1]
    assert (first["message_thread_id"], "Topic Daniel" in first["text"]) == (10, True)
    assert (second["message_thread_id"], "Topic Joel" in second["text"]) == (11, True)
    assert "message_thread_id" not in third and third["chat_id"] == 111 and "Private chat" in third["text"]
    assert all("reply_markup" in m for m in (first, second, third))


# ----- 1156: engine errors in the user's language; a message sent mid-turn is queued -----

@aio
async def test_engine_errors_reach_the_phone_in_the_catalog_language_never_raw(env):
    await linked(env)
    env.engine.script = [{"type": "error", "code": "busy", "message": "a turn is already running"}]
    await env.glue.link._handle_text_message(text_update(111, "hola"))
    assert env.app.bot.sent[-1]["text"] == "Hokma is busy with another answer. Try again in a moment."
    env.engine.script = [{"type": "error", "code": "unknown_project", "message": "no such project"}]
    await env.glue.link._handle_text_message(text_update(111, "hola"))
    assert "project no longer exists" in env.app.bot.sent[-1]["text"]
    env.engine.script = [{"type": "error", "code": "engine_error", "message": "ValueError: boom"}]
    await env.glue.link._handle_text_message(text_update(111, "hola"))
    last = env.app.bot.sent[-1]["text"]
    assert "ValueError" not in last and "boom" not in last and "engine_error" in last


@aio
async def test_text_written_during_a_turn_is_queued_and_sent_when_it_ends(env):
    await linked(env)
    await env.glue.link._handle_text_message(text_update(111, "uno"))
    cid = env.engine.handled[0]["conversation_id"]
    env.engine.sessions[cid] = SimpleNamespace(state="running", pending={})
    await env.glue.link._handle_text_message(text_update(111, "dos"))
    await env.glue.link._handle_text_message(text_update(111, "tres"))
    assert len(env.engine.handled) == 1 and "queued" in env.app.bot.sent[-1]["text"]
    env.engine.sessions[cid].state = "idle"
    await env.glue._on_event(json.dumps({"type": "done", "conversation_id": cid, "turn_id": "t1"}), 0)
    for task in list(env.glue._tasks):
        await task
    assert env.engine.handled[-1]["text"] == "dos\n\ntres" and env.glue._queued == {}


# ----- 1157 -----

def test_catalog_texts_of_the_nits():
    from hokma import i18n

    es, en = i18n.catalog("es"), i18n.catalog("en")
    for key in ("pairing_code_message", "group_pairing_code_message"):
        assert "Ajustes, Telegram" in es[key] and "Configuraci\u00f3n" not in es[key]
    assert "Configuraci\u00f3n" not in es["telegram.err.no_token"]
    assert en["avisos_topic_name"] == "Notices" and "vinculado" in es["telegram.notify_off"]


@aio
async def test_silences_now_say_something(env):
    await grouped(env)
    # a second /vincular with a group already linked
    await env.glue.link._handle_vincular(private_vincular_update())
    assert "already linked" in env.app.bot.sent[-1]["text"]
    # text in General and in Avisos, /new in Avisos and in General
    await say(env, "hola", None)
    assert "do not chat here" in env.app.bot.sent[-1]["text"] and "message_thread_id" not in env.app.bot.sent[-1]
    await say(env, "hola", AVISOS)
    assert "notices only" in env.app.bot.sent[-1]["text"] and env.app.bot.sent[-1]["message_thread_id"] == AVISOS
    await env.glue.link._handle_reset(group_update("/new", AVISOS))
    assert "inside a conversation topic" in env.app.bot.sent[-1]["text"]
    await env.glue.link._handle_reset(group_update("/new", None))
    assert "inside a conversation topic" in env.app.bot.sent[-1]["text"]
    # voice and photos, from the owner and from a stranger
    voice = SimpleNamespace(text=None, chat=SimpleNamespace(id=111, type="private"), message_thread_id=None)
    update = SimpleNamespace(effective_message=voice, effective_chat=voice.chat, message=voice,
                             effective_user=SimpleNamespace(id=111))
    await env.glue.link._handle_unsupported(update)
    assert "only understand text" in env.app.bot.sent[-1]["text"]
    sent = len(env.app.bot.sent)
    stranger = SimpleNamespace(effective_message=voice, effective_chat=voice.chat, message=voice,
                               effective_user=SimpleNamespace(id=999))
    await env.glue.link._handle_unsupported(stranger)
    assert len(env.app.bot.sent) == sent and env.engine.handled == []


def test_oversized_token_and_code_are_a_code_and_a_message_not_a_422(env):
    with client_for(env.glue) as client:
        r = client.put("/api/telegram/token", json={"token": "1" * 101}, headers=HEADERS)
        assert r.status_code == 400 and set(r.json()["detail"]) == {"code", "message"}
        r = client.post("/api/telegram/approve", json={"code": "A" * 17}, headers=HEADERS)
        assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_code"
        r = client.post("/api/telegram/group/approve", json={"code": "A" * 17}, headers=HEADERS)
        assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_code"


@aio
async def test_avisos_can_be_tried_again_from_the_api(env):
    await linked(env)
    env.glue.store.save({"chat_id": "111", "user_name": "Ana", "group_id": GROUP, "avisos": "no_permission"})
    answers = [None, AVISOS]                      # refused first, then the right was granted

    async def create_forum_topic(**kw):
        thread = answers.pop(0)
        if thread is None:
            raise RuntimeError("not enough rights to create a topic")
        return SimpleNamespace(message_thread_id=thread)

    env.app.bot.create_forum_topic = create_forum_topic
    with client_for(env.glue) as client:
        refused = client.post("/api/telegram/group/avisos", headers=HEADERS).json()
        assert refused["avisos"] == "no_permission" and refused["avisos_detail"]["message"]
        assert client.post("/api/telegram/group/avisos", headers=HEADERS).json() == {"avisos": "ok", "avisos_detail": None}
        assert client.post("/api/telegram/group/avisos", headers={"X-Hokma-Token": HEADERS["X-Hokma-Token"]}
                           ).status_code == 403
        env.glue.link.unlink_group()
        assert client.post("/api/telegram/group/avisos", headers=HEADERS).status_code == 409


@aio
async def test_a_button_of_an_earlier_run_says_so_and_loses_its_keyboard(env):
    await linked(env)
    await env.glue.link.send_exec_approval("rm x", "r1", "Bash")
    env.glue.link._ui._approval_state.clear()                           # Hokma restarted: its memory is gone
    update, answers = tap(button(env.app.bot, 0), 111)
    await env.glue.link._ui.handle_callback_query(update)
    assert answers == ["That permission is no longer pending (Hokma restarted)."] and update.keyboard_edits
    await env.glue.link.send_exec_approval("rm y", "r2", "Bash")        # a double tap is still "already answered"
    cid = env.glue._conversation(0, None)[0]
    env.engine.sessions[cid] = FakeSession("Bash", "rm y", "r2")
    first, _ = tap(button(env.app.bot, 0), 111)
    again, answers = tap(button(env.app.bot, 0), 111)
    await env.glue.link._ui.handle_callback_query(first)
    await env.glue.link._ui.handle_callback_query(again)
    assert answers == ["Already answered"] and not again.keyboard_edits


@aio
async def test_changing_chat_forgets_the_sessions_of_the_previous_one(env):
    await linked(env)
    await env.glue.link._handle_text_message(text_update(111, "hola"))
    with env.db.read() as u:
        assert u.session.scalar("SELECT count(*) FROM telegram_session WHERE chat_id = '111'") == 1
    env.glue.store.save({"chat_id": "111", "user_name": "Ana", "group_id": GROUP})       # same chat: kept
    with env.db.read() as u:
        assert u.session.scalar("SELECT count(*) FROM telegram_session") == 1
    env.glue.store.save({"chat_id": "222", "user_name": "Bea"})
    with env.db.read() as u:
        assert u.session.scalar("SELECT count(*) FROM telegram_session") == 0


# ----- extra: the Bot API base is configurable for tests, loopback only -----

def test_the_api_base_variable_accepts_only_loopback_http(monkeypatch):
    monkeypatch.delenv("HOKMA_TELEGRAM_API_BASE", raising=False)
    assert api_base_override() is None
    for good in ("http://127.0.0.1:47202", "http://localhost:9/", "http://localhost"):
        monkeypatch.setenv("HOKMA_TELEGRAM_API_BASE", good)
        assert api_base_override() == good.rstrip("/")
    for bad in ("https://api.telegram.org", "http://example.com", "http://127.0.0.1.evil.com", "http://127.0.0.1:1/x",
                "https://127.0.0.1:47202", "http://127.0.0.1:notaport", "ftp://localhost"):
        monkeypatch.setenv("HOKMA_TELEGRAM_API_BASE", bad)
        assert api_base_override() is None, bad


def test_the_application_uses_the_local_base_only_when_asked(monkeypatch):
    monkeypatch.delenv("HOKMA_TELEGRAM_API_BASE", raising=False)
    app = asyncio.run(build_application(TOKEN))
    assert app.bot.base_url.startswith("https://api.telegram.org/bot")
    monkeypatch.setenv("HOKMA_TELEGRAM_API_BASE", "http://127.0.0.1:47202")
    app = asyncio.run(build_application(TOKEN))
    assert app.bot.base_url == f"http://127.0.0.1:47202/bot{TOKEN}"

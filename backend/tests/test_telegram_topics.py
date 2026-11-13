"""SPEC-012 P3: the linked group, one conversation per topic, the reset policy, Avisos for notices and permissions,
and the migration over a 0.0.31 database. Telegram is a fake (see test_telegram_glue)."""
import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hokma.db import get_database, migrate
from hokma.telegram.api import create_router
from hokma.telegram.glue import TelegramGlue
from permissions_support import HEADERS
from test_telegram_glue import FakeSession, aio, env, linked, text_update  # noqa: F401  (fixtures and helpers)

GROUP, AVISOS = "-100123", 77
NOON = datetime(2026, 11, 12, 12, 0)


def group_update(text, thread_id, user_id=111, chat_id=GROUP):
    chat = SimpleNamespace(id=int(chat_id), type="supergroup", is_forum=True, title="Hokma")
    msg = SimpleNamespace(text=text, chat=chat, message_thread_id=thread_id, is_topic_message=True, message_id=5,
                          reply_to_message=None)
    return SimpleNamespace(effective_message=msg, effective_chat=chat, message=msg,
                           effective_user=SimpleNamespace(id=user_id, first_name="Ana"))


async def grouped(env):
    await linked(env)
    env.glue.store.save({"chat_id": "111", "user_name": "Ana", "group_id": GROUP, "avisos_thread_id": AVISOS,
                         "avisos_group_id": GROUP, "avisos": "ok"})
    env.glue._now = lambda: NOON
    env.engine.script = [{"type": "delta", "turn_id": "t1", "text": "ok"},
                         {"type": "done", "turn_id": "t1", "stopped": False}]


def sessions(db):
    with db.read() as u:
        return u.session.query("SELECT thread_id, conversation_id, topic_name FROM telegram_session ORDER BY thread_id")


def titles(env):
    return [env.engine.store.get_conversation(r["conversation_id"]).title for r in sessions(env.db)]


async def say(env, text, thread_id, **kw):
    await env.glue.link._handle_text_message(group_update(text, thread_id, **kw))


@aio
async def test_each_topic_is_its_own_conversation_and_the_answer_goes_back_to_it(env):
    await grouped(env)
    env.glue.link._topics.update({10: "Daniel", 11: "Joel"})
    await say(env, "uno", 10)
    await say(env, "dos", 11)
    await say(env, "tres", 10)
    sent = env.engine.handled
    assert sent[0]["conversation_id"] == sent[2]["conversation_id"] != sent[1]["conversation_id"]
    assert titles(env) == ["Daniel 2026-11-12", "Joel 2026-11-12"]
    assert [m["message_thread_id"] for m in env.app.bot.sent] == [10, 11, 10]


@aio
async def test_a_new_command_opens_another_and_leaves_the_old_one_intact(env):
    await grouped(env)
    await say(env, "uno", 10)
    old = sessions(env.db)[0]["conversation_id"]
    env.glue.on_reset(10)
    await say(env, "dos", 10)
    new = sessions(env.db)[0]["conversation_id"]
    assert new != old and env.engine.store.get_conversation(old) is not None


@aio
async def test_idle_and_daily_close_the_session_but_a_running_turn_does_not(env):
    await grouped(env)
    await say(env, "uno", 10)
    first = sessions(env.db)[0]["conversation_id"]
    env.glue._now = lambda: NOON + timedelta(minutes=119)
    await say(env, "dos", 10)
    assert sessions(env.db)[0]["conversation_id"] == first                          # 119 min: same
    later = NOON + timedelta(minutes=119 + 121)
    env.glue._now = lambda: later
    env.engine.sessions[first] = FakeSession("Bash", "x", "r1")                      # a permission is waiting: busy
    await say(env, "tres", 10)
    assert sessions(env.db)[0]["conversation_id"] == first
    env.engine.sessions.clear()
    env.glue._now = lambda: later + timedelta(minutes=121)
    await say(env, "cuatro", 10)
    second = sessions(env.db)[0]["conversation_id"]
    assert second != first and "two hours" in env.app.bot.sent[-2]["text"]            # notice, then the answer
    env.glue._now = lambda: datetime(2026, 11, 13, 3, 0)                              # next night, idle again
    await say(env, "cinco", 10)
    env.glue._now = lambda: datetime(2026, 11, 13, 3, 59)
    await say(env, "seis", 10)
    third = sessions(env.db)[0]["conversation_id"]
    env.glue._now = lambda: datetime(2026, 11, 13, 4, 1)                              # 62 min later, but past 04:00
    await say(env, "siete", 10)
    assert sessions(env.db)[0]["conversation_id"] not in (second, third) and "daily" in env.app.bot.sent[-2]["text"]


@aio
async def test_notices_and_permissions_go_to_avisos_and_private_answers_stay_private(env):
    await grouped(env)
    env.engine.script = [{"type": "permission_request", "turn_id": "t1", "request_id": "r1", "tool": "Bash",
                          "input": {"command": "ls"}, "summary": {"kind": "run", "target": "ls"}}]
    await say(env, "lista", 10)
    assert env.app.bot.sent[-1]["message_thread_id"] == AVISOS and "reply_markup" in env.app.bot.sent[-1]
    cid = env.engine.handled[0]["conversation_id"]
    session = env.engine.sessions[cid] = FakeSession("Bash", "ls", "r1")
    assert await env.glue.on_decision("r1", "once") == 1 and session.resolved == [("r1", "allow", "human")]
    env.glue.notify_job(SimpleNamespace(name="Daily"), SimpleNamespace(status="done", detail="ok"))
    for task in list(env.glue._tasks):
        await task
    assert env.app.bot.sent[-1]["message_thread_id"] == AVISOS
    env.engine.script = [{"type": "delta", "turn_id": "t2", "text": "privado"}, {"type": "done", "turn_id": "t2"}]
    await env.glue.link._handle_text_message(text_update(111, "hola"))
    last = env.app.bot.sent[-1]
    assert last["text"] == "privado" and "message_thread_id" not in last and last["chat_id"] == 111


@aio
async def test_another_user_or_another_group_is_ignored(env):
    await grouped(env)
    await say(env, "orden", 10, user_id=999)
    await say(env, "orden", 10, chat_id="-100999")
    assert env.engine.handled == [] and sessions(env.db) == []


@aio
async def test_saving_the_group_keeps_the_queue_and_changing_the_chat_clears_it(env):
    await linked(env)
    env.app.bot.sent.clear()
    with env.db.write() as u:
        u.session.insert("telegram_outbox", {"text": "pending"})
    env.glue.store.save({"chat_id": "111", "user_name": "Ana", "group_id": GROUP})
    with env.db.read() as u:
        assert u.session.scalar("SELECT count(*) FROM telegram_outbox") == 1
    with env.db.write() as u:
        u.session.insert("telegram_outbox", {"text": "in a topic", "thread_id": 10})
    env.glue.store.save({"chat_id": "111", "user_name": "Ana", "group_id": "-100555"})      # another group
    with env.db.read() as u:
        assert [r["text"] for r in u.session.query("SELECT text FROM telegram_outbox")] == ["pending"]
    env.glue.store.save({"chat_id": "222", "user_name": "Bea"})
    with env.db.read() as u:
        assert u.session.scalar("SELECT count(*) FROM telegram_outbox") == 0


@aio
async def test_changing_the_group_forgets_the_topic_sessions(env):
    await grouped(env)
    await say(env, "uno", 10)
    env.glue.store.save({"chat_id": "111", "user_name": "Ana", "group_id": "-100555"})
    assert sessions(env.db) == []


@aio
async def test_a_button_still_reaches_a_permission_left_behind_by_new(env):
    await grouped(env)
    await say(env, "uno", 10)
    old = sessions(env.db)[0]["conversation_id"]
    env.glue.on_reset(10)
    env.engine.sessions[old] = FakeSession("Bash", "x", "r1")
    env.glue._conns[old] = object()
    assert await env.glue.on_decision("r1", "allow") == 1


@aio
async def test_a_turn_that_ends_late_renews_the_session(env):
    await grouped(env)
    await say(env, "uno", 10)
    first = sessions(env.db)[0]["conversation_id"]
    env.glue._now = lambda: NOON + timedelta(minutes=200)
    await env.glue._on_event(json.dumps({"type": "done", "conversation_id": first, "turn_id": "t9"}), 10)
    env.glue._now = lambda: NOON + timedelta(minutes=210)
    await say(env, "dos", 10)
    assert sessions(env.db)[0]["conversation_id"] == first


@aio
async def test_the_group_routes_link_unlink_and_report_their_state(env):
    await linked(env)

    async def create_forum_topic(**kw):
        return SimpleNamespace(message_thread_id=AVISOS)

    env.app.bot.create_forum_topic = create_forum_topic
    app = FastAPI()
    app.state.telegram = env.glue
    app.include_router(create_router())
    chat = SimpleNamespace(id=int(GROUP), type="supergroup", is_forum=True, title="Hokma")
    msg = SimpleNamespace(text="/vincular", chat=chat, message_thread_id=None, is_topic_message=False, message_id=1)
    await env.glue.link._handle_vincular(SimpleNamespace(effective_message=msg, effective_chat=chat, message=msg,
                                                         effective_user=SimpleNamespace(id=111)))
    code = env.app.bot.sent[-1]["text"].split(" is ")[1].split(".")[0]

    def call(client, verb, url, **kw):
        return getattr(client, verb)(url, headers=HEADERS, **kw)

    with TestClient(app) as client:
        bad = call(client, "post", "/api/telegram/group/approve", json={"code": "WRONG123"})
        assert bad.status_code == 400 and bad.json()["detail"]["code"] == "invalid_code"
        assert call(client, "post", "/api/telegram/group/approve", json={"code": code}).json() == {
            "group_linked": True, "avisos": "ok"}
        assert call(client, "get", "/api/telegram").json() == {"linked": True, "connected": True, "user_name": "Ana",
                                                               "group_linked": True, "avisos": "ok"}
        assert call(client, "delete", "/api/telegram/group").json() == {"group_linked": False}
        state = call(client, "get", "/api/telegram").json()
        assert state["linked"] is True and state["group_linked"] is False and state["avisos"] is None


def test_the_migration_applies_over_a_0013_database(tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    old = tmp_path / "old"
    old.mkdir()
    for f in (Path(__file__).resolve().parents[1] / "hokma" / "db" / "migrations").glob("00*.sql"):
        if int(f.name[:4]) <= 13:
            shutil.copy(f, old / f.name)
    assert migrate(migrations_dir=old).version == 13
    db = get_database()
    with db.write() as u:
        u.session.execute("INSERT INTO telegram_link (id, chat_id, user_name) VALUES (1, '111', 'Ana')")
        u.session.insert("telegram_outbox", {"text": "pending"})
    assert migrate().applied == [14]
    with db.read() as u:
        link = u.session.one("SELECT chat_id, group_id, avisos_thread_id, avisos FROM telegram_link")
        assert link == {"chat_id": "111", "group_id": None, "avisos_thread_id": None, "avisos": None}
        assert u.session.one("SELECT thread_id FROM telegram_outbox")["thread_id"] is None
        assert u.session.scalar("SELECT count(*) FROM telegram_session") == 0

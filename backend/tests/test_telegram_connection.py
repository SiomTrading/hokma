"""Bugs 1126 and 1127: `connected` means the bot is up, a bot that fell is brought back, the Avisos state is
revalidated and tells a network failure from a missing permission, and the outbox shows what Telegram refused.
Telegram is a fake (see test_telegram_glue)."""
import asyncio
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from telegram.error import NetworkError

from hokma.telegram.api import create_router
from hokma.telegram.glue import TOKEN_ENTRY
from permissions_support import HEADERS
from test_telegram_glue import TOKEN, aio, env, linked  # noqa: F401  (fixtures and helpers)

GROUP = "-100123"


def client_for(env):
    app = FastAPI()
    app.state.telegram = env.glue
    app.include_router(create_router())
    return TestClient(app)


@aio
async def test_connected_follows_the_long_poll_not_just_the_application(env):
    await linked(env)
    assert env.glue.link.connected
    env.app.updater.running = False                     # the poll died
    assert not env.glue.link.connected
    with client_for(env) as client:
        state = client.get("/api/telegram", headers=HEADERS).json()
    assert state["linked"] is True and state["connected"] is False


@aio
async def test_the_watcher_retries_with_a_backoff_until_the_bot_is_up(env):
    env.glue.keys.set(TOKEN_ENTRY, TOKEN)
    calls = []
    real = env.glue.link.start

    async def flaky(token):
        calls.append(token)
        if len(calls) < 3:
            raise ConnectionError("no network")
        await real(token)

    env.glue.link.start = flaky
    task = asyncio.create_task(env.glue.keep_connected(backoff=(0.01, 0.01), watch=0.01))
    await asyncio.sleep(0.3)
    task.cancel()
    assert len(calls) == 3 and env.glue.link.connected and env.glue.connect_error is None


@aio
async def test_the_watcher_brings_back_a_bot_whose_poll_died_and_stays_idle_without_token(env):
    task = asyncio.create_task(env.glue.keep_connected(backoff=(0.01,), watch=0.01))
    await asyncio.sleep(0.05)
    assert not env.glue.link.connected                  # no token: nothing to do
    env.glue.keys.set(TOKEN_ENTRY, TOKEN)
    await asyncio.sleep(0.1)
    assert env.glue.link.connected
    env.app.updater.running = False
    await asyncio.sleep(0.1)
    task.cancel()
    assert env.glue.link.connected


@aio
async def test_a_failed_attempt_leaves_the_reason_without_the_token(env):
    env.glue.keys.set(TOKEN_ENTRY, TOKEN)

    async def down(token):
        raise ConnectionError(f"cannot reach https://api.telegram.org/bot{token}/getMe")

    env.glue.link.start = down
    task = asyncio.create_task(env.glue.keep_connected(backoff=(5,), watch=5))
    await asyncio.sleep(0.05)
    task.cancel()
    assert env.glue.connect_error and TOKEN not in env.glue.connect_error


@aio
async def test_the_reconnect_route_retries_and_keeps_the_token(env):
    await linked(env)
    env.app.updater.running = False
    with client_for(env) as client:
        assert client.post("/api/telegram/reconnect", headers=HEADERS).json() == {"connected": True}
        env.glue.keys.delete(TOKEN_ENTRY)
        none = client.post("/api/telegram/reconnect", headers=HEADERS)
        assert none.status_code == 409 and none.json()["detail"]["code"] == "no_token"


def group_bot(env, *, create=None, action=None):
    async def create_forum_topic(**kw):
        return await create(**kw)

    async def send_chat_action(**kw):
        if action:
            await action(**kw)

    env.app.bot.create_forum_topic = create_forum_topic
    env.app.bot.send_chat_action = send_chat_action


def avisos(env):
    return (env.glue.store.load() or {}).get("avisos")


def save_group(env, thread=None):
    env.glue.store.save({"chat_id": "111", "user_name": "Ana", "group_id": GROUP,
                         "avisos_thread_id": thread, "avisos_group_id": GROUP if thread else None, "avisos": "ok"})


@aio
async def test_a_network_failure_is_not_reported_as_a_missing_permission(env):
    await linked(env)
    save_group(env)

    async def down(**kw):
        raise ConnectionError("network down")

    group_bot(env, create=down)
    assert await env.glue.link.ensure_avisos_topic() is False and avisos(env) == "unreachable"


@aio
async def test_a_refusal_for_lack_of_rights_is_reported_as_a_missing_permission(env):
    await linked(env)
    save_group(env)

    async def refused(**kw):
        raise RuntimeError("Not enough rights to create a topic")

    group_bot(env, create=refused)
    assert await env.glue.link.ensure_avisos_topic() is False and avisos(env) == "no_permission"


@aio
async def test_starting_the_bot_revalidates_a_stored_ok(env):
    await linked(env)
    save_group(env, thread=77)

    async def gone(**kw):
        raise RuntimeError("Not enough rights")

    async def create(**kw):
        raise RuntimeError("Not enough rights to create a topic")

    group_bot(env, create=create, action=gone)
    await env.glue.start()
    assert avisos(env) == "no_permission"               # was "ok" since the day it was written

    save_group(env, thread=77)

    async def fine(**kw):
        pass

    group_bot(env, create=create, action=fine)
    await env.glue.start()
    assert avisos(env) == "ok"


@aio
async def test_the_screen_state_shows_what_the_outbox_holds_and_the_last_error(env):
    await linked(env)

    async def down(**kw):
        raise RuntimeError("Forbidden: bot was blocked by the user")

    env.app.bot.send_message = down
    await env.glue.deliver("one")
    await env.glue.deliver("two")
    with client_for(env) as client:
        outbox = client.get("/api/telegram", headers=HEADERS).json()["outbox"]
    assert outbox["pending"] == 2 and "blocked" in outbox["last_error"]


@aio
async def test_a_permission_whose_buttons_could_not_be_sent_leaves_a_notice_in_the_outbox(env):
    await linked(env)

    async def down(**kw):
        raise ConnectionError("network down")

    env.app.bot.send_message = down
    event = {"type": "permission_request", "request_id": "r1", "tool": "Write", "input": {"path": "a"}}
    await env.glue._on_event(json.dumps(event), 0)
    with env.db.read() as u:
        texts = [r["text"] for r in u.session.query("SELECT text FROM telegram_outbox WHERE sent_at IS NULL")]
    assert len(texts) == 1 and "Write" in texts[0]


@aio
async def test_a_network_failure_when_setting_a_token_keeps_the_link_and_the_old_token(env):
    """Bug 1149: a failed attempt never unlinks. The chat, the group and the previous token stay."""
    await linked(env)
    save_group(env)
    OLD = TOKEN
    NEW = "987654321:" + "B" * 35

    async def down(token):
        raise NetworkError("no network")

    env.glue.link.start = down
    with client_for(env) as client:
        res = client.put("/api/telegram/token", json={"token": NEW}, headers=HEADERS)
        assert res.status_code == 502 and res.json()["detail"]["code"] == "network"
        state = client.get("/api/telegram", headers=HEADERS).json()
    assert state["linked"] is True and state["group_linked"] is True and state["user_name"] == "Ana"
    assert env.glue.keys.get(TOKEN_ENTRY) == OLD


@aio
async def test_a_failed_first_token_leaves_no_token_behind(env):
    async def down(token):
        raise NetworkError("no network")

    env.glue.link.start = down
    with client_for(env) as client:
        assert client.put("/api/telegram/token", json={"token": TOKEN}, headers=HEADERS).status_code == 502
    assert not env.glue.keys.get(TOKEN_ENTRY)

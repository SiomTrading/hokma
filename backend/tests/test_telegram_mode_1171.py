"""Bug 1171 (Hokma 0.0.37): a Telegram conversation says which permission mode it runs in, and the screen shows it.
The mode is the one of Settings > General (the same `engine.permission_mode()` the chat uses). Telegram is a fake."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hokma.telegram.api import create_router
from permissions_support import HEADERS
from test_telegram_glue import aio, env  # noqa: F401  (fixtures and helpers)
from test_telegram_topics import group_update, grouped, say


def texts(env):
    return [m["text"] for m in env.app.bot.sent]


@aio
async def test_a_new_conversation_says_the_mode_once_in_auto(env):
    await grouped(env)
    env.engine.permission_mode = lambda: "auto"
    await say(env, "uno", 10)
    await say(env, "dos", 10)
    notices = [x for x in texts(env) if x.startswith("Permission mode:")]
    assert len(notices) == 1 and "Do everything without asking" in notices[0] and "no permission buttons" in notices[0]


@aio
async def test_a_new_conversation_says_the_mode_in_ask(env):
    await grouped(env)
    env.engine.permission_mode = lambda: "ask"
    await say(env, "uno", 10)
    notices = [x for x in texts(env) if x.startswith("Permission mode:")]
    assert len(notices) == 1 and "Ask." in notices[0] and "buttons to approve or deny" in notices[0]


@aio
async def test_new_with_a_name_says_the_mode_after_the_answer(env):
    await grouped(env)
    env.engine.permission_mode = lambda: "auto"
    env.glue.link._topics[10] = "Prueba"
    await env.glue.link._handle_reset(group_update("/new Nasdaq", 10))
    sent = texts(env)
    assert sent[-2] == "New conversation started: Prueba · Nasdaq." and sent[-1].startswith("Permission mode:")


@aio
async def test_the_screen_state_carries_the_mode(env):
    await grouped(env)
    app = FastAPI()
    app.state.telegram = env.glue
    app.include_router(create_router())
    with TestClient(app) as client:
        for mode in ("ask", "auto"):
            env.engine.permission_mode = lambda m=mode: m
            assert client.get("/api/telegram", headers=HEADERS).json()["permission_mode"] == mode

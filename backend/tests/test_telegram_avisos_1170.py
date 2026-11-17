"""Bug 1170 (Hokma 0.0.36): the Avisos topic failed with "could not reach Telegram" although the bot was talking.
python-telegram-bot makes BadRequest a NetworkError, so every refusal of Telegram read as a network failure. These tests
raise the real exception classes (the earlier ones raised RuntimeError, which hid it)."""
from pathlib import Path

import pytest
from telegram.error import BadRequest, ChatMigrated, Forbidden, NetworkError, TimedOut

from hokma.core import config
from hokma.telegram.glue import classify_failure
from test_telegram_connection import GROUP, avisos, client_for, group_bot, save_group
from test_telegram_glue import aio, env, linked  # noqa: F401  (fixtures and helpers)
from permissions_support import HEADERS


def raising(exc):
    async def call(**kw):
        raise exc
    return call


def test_a_telegram_refusal_is_not_a_network_failure():
    assert classify_failure(BadRequest("Not enough rights to create a topic")) == "permission"
    assert classify_failure(BadRequest("Chat is not a forum")) == "permission"
    assert classify_failure(BadRequest("Chat not found")) == "refused"
    assert classify_failure(Forbidden("bot was kicked")) == "permission"
    assert classify_failure(ChatMigrated(-100999)) == "migrated"
    assert classify_failure(TimedOut()) == "network"
    assert classify_failure(NetworkError("httpx.ConnectError")) == "network"
    assert classify_failure(ConnectionError("down")) == "network"
    assert classify_failure(RuntimeError("something else")) == "other"


@aio
async def test_a_bad_request_for_lack_of_rights_says_no_permission_with_the_code(env, tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path))
    await linked(env)
    save_group(env)
    group_bot(env, create=raising(BadRequest("Not enough rights to create a topic")))
    assert await env.glue.link.ensure_avisos_topic() is False
    assert avisos(env) == "no_permission"
    assert env.glue.link.topic_error["code"] == 400
    log = Path(config.engine_log_path()).read_text(encoding="utf-8")
    assert "telegram avisos topic failed" in log and "BadRequest" in log and "code=400" in log


@aio
async def test_another_bad_request_is_failed_not_unreachable_and_the_screen_gets_its_text(env, tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path))
    await linked(env)
    save_group(env)
    group_bot(env, create=raising(BadRequest("Chat not found")))
    with client_for(env) as client:
        body = client.post("/api/telegram/group/avisos", headers=HEADERS).json()
        assert body["avisos"] == "failed"
        assert body["avisos_detail"]["code"] == 400 and "Chat not found" in body["avisos_detail"]["message"]
        state = client.get("/api/telegram", headers=HEADERS).json()
        assert state["avisos"] == "failed" and state["avisos_detail"]["error"] == "BadRequest"


@aio
async def test_an_unexpected_error_is_failed_with_its_class_in_the_log(env, tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path))
    await linked(env)
    save_group(env)
    group_bot(env, create=raising(RuntimeError("attached to a different loop")))
    assert await env.glue.link.ensure_avisos_topic() is False and avisos(env) == "failed"
    assert "RuntimeError" in Path(config.engine_log_path()).read_text(encoding="utf-8")


@aio
async def test_a_real_network_failure_is_still_unreachable(env, tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path))
    await linked(env)
    save_group(env)
    group_bot(env, create=raising(NetworkError("httpx.ConnectError")))
    assert await env.glue.link.ensure_avisos_topic() is False and avisos(env) == "unreachable"


@aio
async def test_a_group_that_became_a_supergroup_keeps_the_new_id_and_gets_its_topic(env, tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path))
    await linked(env)
    save_group(env)
    seen = []

    class Topic:
        message_thread_id = 55

    async def create(chat_id, name):
        seen.append(chat_id)
        if chat_id == int(GROUP):
            raise ChatMigrated(-100777)
        return Topic()

    group_bot(env, create=lambda **kw: create(kw["chat_id"], kw["name"]))
    assert await env.glue.link.ensure_avisos_topic() is True
    record = env.glue.store.load()
    assert seen == [int(GROUP), -100777]
    assert record["group_id"] == "-100777" and record["avisos_group_id"] == "-100777"
    assert record["avisos_thread_id"] == 55 and record["avisos"] == "ok"


@aio
async def test_revalidating_a_stored_topic_that_telegram_refuses_creates_it_again(env, tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path))
    await linked(env)
    save_group(env, thread=77)

    class Topic:
        message_thread_id = 88

    async def create(**kw):
        return Topic()

    group_bot(env, create=create, action=raising(BadRequest("Message thread not found")))
    assert await env.glue.link.ensure_avisos_topic(revalidate=True) is True
    assert env.glue.store.load()["avisos_thread_id"] == 88

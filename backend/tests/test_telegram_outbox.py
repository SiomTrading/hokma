"""SPEC-011 P17: every message for the owner goes through `telegram_outbox`; it retries until Telegram accepts it,
never goes out twice, survives a restart, and nothing is sent without a link. Telegram is a fake (see test_telegram_glue)."""
import asyncio
import json
from types import SimpleNamespace

from hokma.kit import hokma_tools
from hokma.kit.hokma_tools import Caller
from hokma.telegram.glue import TELEGRAM_LIMIT, TelegramGlue, fit
from test_telegram_glue import FakeApp, aio, env, linked  # noqa: F401  (fixtures and helpers)


def rows(db):
    with db.read() as u:
        return u.session.query("SELECT text, sent_at, attempts, last_error FROM telegram_outbox ORDER BY id")


def break_telegram(bot):
    real = bot.send_message

    async def down(**kw):
        raise ConnectionError("network down")

    bot.send_message = down
    return real


@aio
async def test_a_failed_send_stays_queued_and_the_retry_delivers_it_once(env):
    await linked(env)
    bot = env.app.bot
    real = break_telegram(bot)
    await env.glue.deliver("hello")
    pending = rows(env.db)
    assert len(pending) == 1 and pending[0]["sent_at"] is None and pending[0]["attempts"] == 1
    assert "network down" in pending[0]["last_error"] and bot.sent == []
    bot.send_message = real
    assert await env.glue.flush() == 1
    assert await env.glue.flush() == 0                                   # already sent: not sent again
    assert [m["text"] for m in bot.sent] == ["hello"] and rows(env.db)[0]["sent_at"] is not None


@aio
async def test_the_retry_loop_sends_what_was_waiting(env):
    await linked(env)
    real = break_telegram(env.app.bot)
    await env.glue.deliver("later")
    env.app.bot.send_message = real
    task = asyncio.create_task(env.glue.retry_loop(0.01))
    await asyncio.sleep(0.1)
    task.cancel()
    assert [m["text"] for m in env.app.bot.sent] == ["later"]


@aio
async def test_concurrent_senders_do_not_duplicate_and_keep_the_order(env):
    await linked(env)
    for text in ("one", "two", "three"):
        env.glue._store_text(text)
    await asyncio.gather(env.glue.flush(), env.glue.flush(), env.glue.flush())
    assert [m["text"] for m in env.app.bot.sent] == ["one", "two", "three"]


@aio
async def test_the_queue_survives_a_restart(env, tmp_path):
    await linked(env)
    break_telegram(env.app.bot)
    await env.glue.deliver("kept")
    await env.glue.stop()
    fresh_app = FakeApp()

    async def factory(token):
        return fresh_app

    glue = TelegramGlue(lambda: env.db, lambda: env.engine, keys=env.glue.keys, application_factory=factory,
                        data_dir=tmp_path / "pairing")
    assert await glue.start() and await glue.flush() == 1
    assert [m["text"] for m in fresh_app.bot.sent] == ["kept"]


@aio
async def test_without_a_link_nothing_is_sent_or_stored_and_the_tool_says_so(env):
    env.glue.notify_job(SimpleNamespace(name="Daily"), SimpleNamespace(status="done", detail="x"))
    assert rows(env.db) == [] and await env.glue.flush() == 0
    out, err = hokma_tools.call("notify_owner", {"text": "hi"}, Caller({"HOKMA_AGENT": "joel"}))
    assert not err and json.loads(out)["sent"] is False and "not connected" in json.loads(out)["message"]
    assert rows(env.db) == []


@aio
async def test_notify_owner_queues_for_the_linked_chat_and_the_sender_delivers(env):
    await linked(env)
    out, err = hokma_tools.call("notify_owner", {"text": "  look at the report "}, Caller({"HOKMA_AGENT": "joel"}))
    assert not err and json.loads(out)["sent"] is True
    assert [r["text"] for r in rows(env.db)] == ["look at the report"]
    assert await env.glue.flush() == 1 and env.app.bot.sent[-1]["text"] == "look at the report"
    assert hokma_tools.call("notify_owner", {"text": "  "}, Caller({"HOKMA_AGENT": "joel"}))[1] is True


@aio
async def test_routine_end_carries_the_final_answer_cut_to_the_limit(env):
    await linked(env)
    job = SimpleNamespace(name="Report")
    env.glue.notify_job(job, SimpleNamespace(status="done", detail="x" * 9000))
    for task in list(env.glue._tasks):
        await task
    sent = env.app.bot.sent[0]["text"]
    assert sent.startswith("Routine finished: Report\n\nxxx") and len(sent) <= TELEGRAM_LIMIT
    assert sent.endswith("Continues in Hokma.") and fit("short") == "short"


@aio
async def test_a_message_telegram_rejects_does_not_block_the_next_ones(env):
    await linked(env)
    real = env.app.bot.send_message

    async def picky(**kw):
        if kw["text"] == "bad":
            raise ValueError("Message is too long")
        return await real(**kw)

    env.app.bot.send_message = picky
    for text in ("bad", "good"):
        env.glue._store_text(text)
    assert await env.glue.flush() == 1
    assert [m["text"] for m in env.app.bot.sent] == ["good"] and rows(env.db)[0]["sent_at"] is None


@aio
async def test_pending_messages_do_not_pass_to_another_chat(env):
    await linked(env, "111")
    break_telegram(env.app.bot)
    await env.glue.deliver("for the first chat")
    env.glue.store.save({"chat_id": "222", "user_name": "Other"})        # the link is replaced
    assert [r for r in rows(env.db) if r["sent_at"] is None] == []

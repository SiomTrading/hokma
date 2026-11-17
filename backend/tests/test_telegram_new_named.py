"""`/new Name` and `/reset Name` (Hokma 0.0.37): the new conversation is titled "<topic> · Name". Telegram is a fake."""
from hokma.telegram.glue import LABEL_LIMIT, reset_label
from test_telegram_glue import aio, env  # noqa: F401  (fixtures and helpers)
from test_telegram_topics import group_update, grouped, say, sessions, titles


def test_the_name_is_one_clean_line_of_at_most_sixty_characters():
    assert reset_label("/new Nasdaq") == "Nasdaq"
    assert reset_label("/new@hokma_bot Nasdaq 100") == "Nasdaq 100"
    assert reset_label("/new") == "" and reset_label("/new   ") == "" and reset_label(None) == ""
    assert reset_label("/new uno\ndos\r\n\ttres\x00 ​") == "uno dos tres"
    long = reset_label("/new " + "\u00f1" * 200)
    assert long == "\u00f1" * LABEL_LIMIT and len(long) == 60
    assert reset_label("/new " + "🙂" * 100) == "🙂" * 60                    # characters, never half an emoji


@aio
async def test_new_with_a_name_titles_the_conversation_and_says_so(env):
    await grouped(env)
    env.glue.link._topics[10] = "Prueba"
    await say(env, "uno", 10)
    old = sessions(env.db)[0]["conversation_id"]
    await env.glue.link._handle_reset(group_update("/new Nasdaq", 10))
    row = sessions(env.db)[0]
    assert row["conversation_id"] != old and env.engine.store.get_conversation(old) is not None
    assert titles(env) == ["Prueba · Nasdaq"]
    assert env.app.bot.sent[-2]["text"] == "New conversation started: Prueba · Nasdaq."
    await say(env, "dos", 10)                                               # the next message continues in that one
    assert sessions(env.db)[0]["conversation_id"] == row["conversation_id"]
    assert titles(env) == ["Prueba · Nasdaq"]


@aio
async def test_reset_works_the_same_and_a_bare_new_keeps_the_dated_title(env):
    await grouped(env)
    env.glue.link._topics[10] = "Prueba"
    await say(env, "uno", 10)
    await env.glue.link._handle_reset(group_update("/reset Opciones", 10))
    assert titles(env) == ["Prueba · Opciones"]
    await env.glue.link._handle_reset(group_update("/new", 10))
    assert env.app.bot.sent[-1]["text"] == "New conversation started."
    assert sessions(env.db) == []                                           # nothing opened yet
    await say(env, "dos", 10)
    assert titles(env) == ["Prueba 2026-11-12"]


@aio
async def test_a_long_name_is_cut_to_sixty_characters(env):
    await grouped(env)
    env.glue.link._topics[10] = "Prueba"
    await env.glue.link._handle_reset(group_update("/new " + "x" * 200 + "\nresto", 10))
    assert titles(env) == ["Prueba · " + "x" * 60]
    assert env.app.bot.sent[-2]["text"].endswith("x" * 60 + ".")

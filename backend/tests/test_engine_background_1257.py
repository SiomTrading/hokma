"""Bug 1257: the answer of a subagent started in the background reaches the chat.

The CLI (2.1.332) goes on after its first `result` while such an agent works, and when the agent ends it opens a turn of its
own to tell the model. The chat keeps reading the pipe, shows that turn as a new message of the assistant with its origin,
groups the endings that come together, never blocks the owner, and kills every agent on a stop or when the wait runs out.
The CLI lines are those of agent_turn.json; fake_claude.py plays the `[bgagent]` scenarios."""
import os
import time

import pytest

from engine_support import connect, env, hard_timeout, make_client, posix_only, text_of, turn, until  # noqa: F401

from hokma.engine import stream

pytestmark = posix_only


def kinds(events):
    return [e["type"] for e in events]


def engine(env, **kwargs):
    kwargs.setdefault("group_window", 1.0)
    return env.engine(**kwargs)


def read_turns(ws, count):
    """Every event up to the `done` that closes the `count`-th turn."""
    events, dones = [], 0
    while dones < count:
        event = ws.receive_json()
        events.append(event)
        dones += event["type"] == "done"
    return events


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# ------------------------------------------------------------------------------------------------ pure helpers

def test_the_lines_that_start_and_end_a_task_of_the_main_thread():
    started = {"type": "system", "subtype": "task_started", "task_id": "t1", "tool_use_id": "u1", "is_backgrounded": True}
    assert stream.task_track(started) == ("add", "t1")
    assert stream.task_track({**started, "is_backgrounded": False}) is None
    assert stream.task_track({**started, "owned_by_subagent": True}) is None          # a step of a subagent: not ours
    assert stream.task_track({"type": "system", "subtype": "task_notification", "task_id": "t1"}) == ("done", "t1")
    assert stream.task_track({"type": "system", "subtype": "task_updated", "task_id": "t1",
                              "patch": {"status": "completed"}}) is None      # the notification is what ends it
    assert stream.task_track({"type": "assistant"}) is None


# --------------------------------------------------------------------------------------------------- the socket

def test_the_summary_of_a_background_agent_comes_back_as_a_message_of_the_assistant(env):
    with make_client(engine(env)) as client, connect(client) as ws:
        first = turn(ws, "[bgagent] investiga")
        conv_id = first[0]["conversation_id"]
        later = read_turns(ws, 1)
    assert "agent_end" not in kinds(first)                                    # the agent is still working at the first done
    [start] = [e for e in first if e["type"] == "agent_start"]
    assert text_of(first) == "launched"
    [begin] = [e for e in later if e["type"] == "turn_start"]
    assert begin["origin"] == "agents" and begin["message_id"] is None and begin["turn_id"] != first[-1]["turn_id"]
    [end] = [e for e in later if e["type"] == "agent_end"]
    assert end["agent_id"] == start["agent_id"] and end["status"] == "done" and end["result"] == "hola"
    assert end["turn_id"] == start["turn_id"]                                  # it closes the row of the turn that launched it
    assert text_of(later) == "agent A finished" and later[-1]["type"] == "done" and later[-1]["message_id"]
    assert all(e["turn_id"] == begin["turn_id"] for e in later if e["type"] in ("delta", "done"))
    messages = env.store.list_messages(conv_id)
    assert [(m.role, m.text) for m in messages] == [("user", "[bgagent] investiga"), ("assistant", "launched"),
                                                    ("assistant", "agent A finished")]
    assert messages[2].meta["origin"] == "agents" and "origin" not in messages[1].meta


def test_agents_that_end_together_give_one_summary(env):
    with make_client(engine(env, group_window=1.5)) as client, connect(client) as ws:
        first = turn(ws, "[bgagent2] investiga")
        conv_id = first[0]["conversation_id"]
        later = read_turns(ws, 1)
        ws.send_json({"type": "ping"})
        tail = until(ws, "pong")
    assert kinds(later).count("turn_start") == 1 and kinds(later).count("done") == 1 and "done" not in kinds(tail)
    assert [e["status"] for e in later if e["type"] == "agent_end"] == ["done", "done"]
    assert "agent A finished" in text_of(later) and "agent B finished" in text_of(later)
    assert [m.text for m in env.store.list_messages(conv_id) if m.meta.get("origin")] == [text_of(later)]


def test_the_owner_keeps_writing_while_an_agent_works_and_the_summary_comes_after_his_turn(env):
    with make_client(engine(env)) as client, connect(client) as ws:
        first = turn(ws, "[bgagent] investiga")
        conv_id = first[0]["conversation_id"]
        mine = turn(ws, "hola", conversation_id=conv_id)                                             # the chat is free: he asks something else
        later = read_turns(ws, 1)
    assert text_of(mine) == "pong" and mine[-1]["type"] == "done"
    [begin] = [e for e in later if e["type"] == "turn_start"]
    assert text_of(later) == "agent A finished" and begin["origin"] == "agents" and begin["message_id"] is None
    assert [(m.role, m.text) for m in env.store.list_messages(conv_id)] == [
        ("user", "[bgagent] investiga"), ("assistant", "launched"), ("user", "hola"), ("assistant", "pong"),
        ("assistant", "agent A finished")]


def test_the_owner_cannot_mix_a_message_into_the_summary_that_is_being_written(env):
    with make_client(engine(env)) as client, connect(client) as ws:
        first = turn(ws, "[bgagent-long] investiga")
        conv_id = first[0]["conversation_id"]
        seen = until(ws, "delta")                                              # the summary is on its way
        ws.send_json({"type": "send", "conversation_id": conv_id, "text": "hola"})
        refused = until(ws, "error")
        rest = read_turns(ws, 1)
    assert refused[-1]["code"] == "busy" and text_of(seen + refused + rest) == "agent A finished"
    assert [m.text for m in env.store.list_messages(conv_id)] == ["[bgagent-long] investiga", "launched", "agent A finished"]


def test_a_stop_kills_the_agents_that_work_in_the_background(env):
    with make_client(engine(env)) as client, connect(client) as ws:
        first = turn(ws, "[bgagent-never] investiga")
        conv_id = first[0]["conversation_id"]
        [start] = [e for e in first if e["type"] == "agent_start"]
        pid = env.entries("start")[0]["pid"]
        assert alive(pid)
        ws.send_json({"type": "stop", "conversation_id": conv_id})
        [end] = [e for e in until(ws, "agent_end") if e["type"] == "agent_end"]
        assert end["agent_id"] == start["agent_id"] and end["status"] == "stopped"
        deadline = time.time() + 5
        while alive(pid) and time.time() < deadline:
            time.sleep(0.05)
        assert not alive(pid)
        again = turn(ws, "hola", conversation_id=conv_id)                      # the conversation is free and reopens its engine
    assert text_of(again) == "pong"


def test_a_stop_in_the_middle_of_the_summary_ends_it_as_stopped(env):
    with make_client(engine(env)) as client, connect(client) as ws:
        first = turn(ws, "[bgagent-long] investiga")
        conv_id = first[0]["conversation_id"]
        until(ws, "delta")
        ws.send_json({"type": "stop", "conversation_id": conv_id})
        done = until(ws, "done")[-1]
        again = turn(ws, "hola", conversation_id=conv_id)
    assert done["stopped"] is True and text_of(again) == "pong"
    assert [m.meta.get("origin") for m in env.store.list_messages(conv_id) if m.role == "assistant"][1] == "agents"


def test_an_agent_that_hangs_is_stopped_when_the_wait_runs_out(env):
    with make_client(engine(env, bg_wait=1.0)) as client, connect(client) as ws:
        first = turn(ws, "[bgagent-never] investiga")
        conv_id = first[0]["conversation_id"]
        events = until(ws, "agent_end")
        notice = [e for e in events if e["type"] == "engine_notice"]
        again = turn(ws, "hola", conversation_id=conv_id)
    assert events[-1]["status"] == "stopped" and notice and "min" in notice[0]["message"]
    assert text_of(again) == "pong"
    assert [m.text for m in env.store.list_messages(conv_id)] == ["[bgagent-never] investiga", "launched", "hola", "pong"]


def test_the_wait_is_configurable_from_the_environment(env, monkeypatch):
    monkeypatch.setenv("HOKMA_AGENT_WAIT_SECONDS", "90")
    from hokma.engine.engine import ChatEngine

    assert ChatEngine().bg_wait == 90.0
    monkeypatch.setenv("HOKMA_AGENT_WAIT_SECONDS", "nonsense")
    assert ChatEngine().bg_wait == 3600.0
    assert ChatEngine(bg_wait=5).bg_wait == 5


def test_a_reopened_conversation_marks_the_summary_with_its_origin(env):
    with make_client(engine(env)) as client, connect(client) as ws:
        first = turn(ws, "[bgagent] investiga")
        conv_id = first[0]["conversation_id"]
        read_turns(ws, 1)
    with make_client(engine(env)) as client, connect(client) as ws:
        ws.send_json({"type": "open", "conversation_id": conv_id})
        history = until(ws, "state")[-2]
    rows = [m for m in history["messages"] if m["role"] == "assistant"]
    assert [r.get("origin") for r in rows] == [None, "agents"]


# ----------------------------------------------------------------- review of Vera: saved agents, race, empty turn

def test_the_end_of_a_background_agent_is_saved_with_the_message_that_launched_it(env):
    with make_client(engine(env)) as client, connect(client) as ws:
        first = turn(ws, "[bgagent] investiga")
        conv_id = first[0]["conversation_id"]
        read_turns(ws, 1)
    messages = env.store.list_messages(conv_id)
    assert messages[1].text == "launched" and messages[1].meta["agents"][0]["status"] == "done"
    assert messages[1].meta["agents"][0]["result"] == "hola" and messages[1].meta["agents"][0]["ended_at"]


def test_a_stop_and_a_closed_wait_save_the_agents_as_stopped(env):
    with make_client(engine(env, bg_wait=1.0)) as client, connect(client) as ws:
        first = turn(ws, "[bgagent-never] investiga")
        conv_id = first[0]["conversation_id"]
        until(ws, "agent_end")                                                   # the wait ran out
    assert env.store.list_messages(conv_id)[1].meta["agents"][0]["status"] == "stopped"
    with make_client(engine(env)) as client, connect(client) as ws:
        first = turn(ws, "[bgagent-never] otra", conversation_id=conv_id)
        ws.send_json({"type": "stop", "conversation_id": conv_id})
        until(ws, "agent_end")
    assert env.store.list_messages(conv_id)[3].meta["agents"][0]["status"] == "stopped"


def test_a_reopened_conversation_shows_stopped_the_agent_nobody_waits_for(env):
    with make_client(engine(env)) as client, connect(client) as ws:
        first = turn(ws, "[bgagent-never] investiga")
        conv_id = first[0]["conversation_id"]
        ws.send_json({"type": "open", "conversation_id": conv_id})
        alive_rows = until(ws, "state")[-2]["messages"]                          # the watcher is alive: it still works
        assert alive_rows[1]["agents"][0]["status"] == "working"
    [saved] = [m for m in env.store.list_messages(conv_id) if m.meta.get("agents")]
    saved.meta["agents"][0]["status"] = "working"                                # as left by a restart that never ran the end
    with make_client(engine(env)) as client, connect(client) as ws:
        ws.send_json({"type": "open", "conversation_id": conv_id})
        rows = until(ws, "state")[-2]["messages"]
    assert [a["status"] for a in rows[1]["agents"]] == ["stopped"]


def wait_for(check, seconds=5.0):
    deadline = time.time() + seconds
    while not check() and time.time() < deadline:
        time.sleep(0.02)
    assert check()


def test_between_the_notice_and_the_turn_of_the_cli_the_chat_is_free_and_the_owner_text_goes_after_the_summary(env):
    chat = engine(env, group_window=3.0)                                         # the fake waits 1.5 s between the notice and its turn
    with make_client(chat) as client, connect(client) as ws:
        first = turn(ws, "[bgagent-slow] investiga")
        conv_id = first[0]["conversation_id"]
        session = chat.sessions[conv_id]
        wait_for(lambda: session._owed == 1 and session._cont is None)           # the notice came, the CLI's turn did not
        assert session.state == "idle" and session.watching                      # nothing is opened by the notice
        ws.send_json({"type": "send", "conversation_id": conv_id, "text": "hola"})
        both = read_turns(ws, 2)                                                 # the summary first, then his own answer
    summary, mine = both[:both.index(next(e for e in both if e["type"] == "done")) + 1], both[both.index(next(e for e in both if e["type"] == "done")) + 1:]
    assert "error" not in kinds(both)
    assert text_of(summary) == "agent A finished" and text_of(mine) == "pong"    # never mixed
    # his message is saved when he writes it (bug 1257: it is visible and survives a reload at once), so it sits before
    # the summary that the CLI wrote while it was waiting; its answer comes after
    assert [(m.role, m.text) for m in env.store.list_messages(conv_id)] == [
        ("user", "[bgagent-slow] investiga"), ("assistant", "launched"), ("user", "hola"),
        ("assistant", "agent A finished"), ("assistant", "pong")]
    queued = [e for e in both if e["type"] == "queued"]
    assert len(queued) == 1 and queued[0]["text"] == "hola"                      # the panel was told at once
    starts = [e for e in both if e["type"] == "turn_start" and e.get("message_id")]
    assert [s["message_id"] for s in starts] == [queued[0]["message_id"]]        # his turn reuses the saved message


def test_a_turn_of_the_cli_that_opens_later_than_the_grouping_window_is_read_and_saved(env):
    chat = engine(env, group_window=1.0)                                         # the fake opens its turn 6 s after the notice
    with make_client(chat) as client, connect(client) as ws:
        first = turn(ws, "[bgagent-late] investiga")
        conv_id = first[0]["conversation_id"]
        session = chat.sessions[conv_id]
        time.sleep(2.5)                                                           # past the window: the wait for its init goes on
        assert session.watching and session.state == "idle" and session._owed == 1
        rest = read_turns(ws, 1)
        again = turn(ws, "hola", conversation_id=conv_id)
    assert text_of(rest) == "agent A finished" and text_of(again) == "pong"
    rows = env.store.list_messages(conv_id)
    assert [m.role for m in rows] == ["user", "assistant", "assistant", "user", "assistant"]
    assert rows[2].text == "agent A finished" and "agent A finished" not in rows[4].text


def test_a_notice_without_a_turn_never_leaves_the_chat_busy(env):
    """Bug 1257, review of Vera: the CLI announced the end and never opened its turn. The owner writes at once and his
    answer comes when the short wait for the init ends; the agent is not killed and nothing is abandoned."""
    with make_client(engine(env, init_wait=1.0)) as client, connect(client) as ws:
        first = turn(ws, "[bgagent-nocont] investiga")
        conv_id = first[0]["conversation_id"]
        pid = env.entries("start")[0]["pid"]
        seen = until(ws, "agent_end")                                              # the notice is in the panel
        assert seen[-1]["status"] == "done"
        began = time.time()
        ws.send_json({"type": "send", "conversation_id": conv_id, "text": "hola"})
        mine = read_turns(ws, 1)
        elapsed = time.time() - began
        assert alive(pid)                                                          # the agent and its process were not touched
    assert "error" not in kinds(mine) and text_of(mine) == "pong" and elapsed < 2.5
    assert len(env.entries("start")) == 1                                         # same process: nothing was killed or reopened
    assert [(m.role, m.text) for m in env.store.list_messages(conv_id)] == [
        ("user", "[bgagent-nocont] investiga"), ("assistant", "launched"), ("user", "hola"), ("assistant", "pong")]


def test_a_notice_without_a_turn_stops_waiting_and_the_agent_end_stays_in_the_panel(env):
    chat = engine(env, init_wait=0.6)
    with make_client(chat) as client, connect(client) as ws:
        first = turn(ws, "[bgagent-nocont] investiga")
        conv_id = first[0]["conversation_id"]
        session = chat.sessions[conv_id]
        until(ws, "agent_end")
        wait_for(lambda: not session.watching and session._owed == 0)             # it stopped waiting by itself
        assert session.state == "idle" and session._cont is None
        again = turn(ws, "hola", conversation_id=conv_id)
    assert text_of(again) == "pong"
    assert env.store.list_messages(conv_id)[1].meta["agents"][0]["status"] == "done"
    assert len(env.entries("start")) == 1


def test_two_notices_merged_into_one_turn_do_not_keep_the_chat_waiting(env):
    chat = engine(env, group_window=1.0, init_wait=30.0)                          # the cap after a turn is the grouping window, not 30 s
    with make_client(chat) as client, connect(client) as ws:
        first = turn(ws, "[bgagent-fused] investiga")
        conv_id = first[0]["conversation_id"]
        session = chat.sessions[conv_id]
        later = read_turns(ws, 1)
        began = time.time()
        wait_for(lambda: session._owed == 0 and not session.watching)             # the second turn never came: released at once
        mine = turn(ws, "hola", conversation_id=conv_id)
    assert text_of(later) == "agents A and B finished" and kinds(later).count("turn_start") == 1
    assert text_of(mine) == "pong" and "error" not in kinds(mine) and time.time() - began < 4.0
    rows = env.store.list_messages(conv_id)
    assert [m.text for m in rows if m.role == "assistant"] == ["launched", "agents A and B finished", "pong"]


def test_a_stop_while_the_cli_owes_a_turn_frees_the_chat_and_keeps_the_owner_text(env):
    chat = engine(env, init_wait=30.0)
    with make_client(chat) as client, connect(client) as ws:
        first = turn(ws, "[bgagent-nocont] investiga")
        conv_id = first[0]["conversation_id"]
        session = chat.sessions[conv_id]
        until(ws, "agent_end")
        wait_for(lambda: session._owed == 1)
        ws.send_json({"type": "send", "conversation_id": conv_id, "text": "uno"})
        wait_for(lambda: session._queued is not None)
        ws.send_json({"type": "send", "conversation_id": conv_id, "text": "dos"})
        refused = until(ws, "error")
        assert refused[-1]["code"] == "busy"                                      # one message waits; a second is refused, not mixed
        ws.send_json({"type": "stop", "conversation_id": conv_id})
        mine = read_turns(ws, 1)
    queued = [e for e in refused if e["type"] == "queued"]
    assert len(queued) == 1 and queued[0]["text"] == "uno"                        # echoed at once; the refused one is not
    assert [e["message_id"] for e in mine if e["type"] == "turn_start"] == [queued[0]["message_id"]]   # the stop starts it: the waiting state ends
    assert text_of(mine) == "pong" and session._owed == 0 and session._queued is None
    assert len(env.entries("start")) == 2                                         # the stop killed the process; the text runs on a new one
    assert [(m.role, m.text) for m in env.store.list_messages(conv_id)] == [
        ("user", "[bgagent-nocont] investiga"), ("assistant", "launched"), ("user", "uno"), ("assistant", "pong")]


def test_a_stop_while_the_cli_owes_a_turn_with_nothing_waiting_just_frees_the_chat(env):
    chat = engine(env, init_wait=30.0)
    with make_client(chat) as client, connect(client) as ws:
        first = turn(ws, "[bgagent-nocont] investiga")
        conv_id = first[0]["conversation_id"]
        session = chat.sessions[conv_id]
        until(ws, "agent_end")
        wait_for(lambda: session._owed == 1)
        ws.send_json({"type": "stop", "conversation_id": conv_id})
        wait_for(lambda: not session.watching and session._owed == 0)
        again = turn(ws, "hola", conversation_id=conv_id)
    assert text_of(again) == "pong"


def test_an_automatic_turn_with_nothing_to_show_leaves_no_row_in_the_panel(env):
    with make_client(engine(env, group_window=0.5)) as client, connect(client) as ws:
        first = turn(ws, "[bgagent-empty] investiga")
        conv_id = first[0]["conversation_id"]
        ws.send_json({"type": "ping"})
        seen = []
        while True:
            event = ws.receive_json()
            seen.append(event)
            if event["type"] == "agent_end":
                break
        time.sleep(1.5)                                                           # past the grouping window
        ws.send_json({"type": "ping"})
        seen += until(ws, "pong")
    assert "turn_start" not in kinds(seen) and "done" not in kinds(seen)
    assert [m.role for m in env.store.list_messages(conv_id)] == ["user", "assistant"]


def test_a_message_that_waits_is_echoed_and_saved_at_once_and_starts_when_the_init_wait_runs_out(env):
    """Bug 1257, UX of Vera: the owner's message that waits for the summary was neither shown nor saved until its turn
    started (up to INIT_WAIT seconds). Now `queued` carries it right away, the store has it, and the turn that starts
    when the wait for the init ends (init_timeout) reuses that row: one message, never two."""
    with make_client(engine(env, init_wait=1.0)) as client, connect(client) as ws:
        first = turn(ws, "[bgagent-nocont] investiga")
        conv_id = first[0]["conversation_id"]
        until(ws, "agent_end")
        ws.send_json({"type": "send", "conversation_id": conv_id, "text": "hola"})
        seen = until(ws, "queued")
        saved = [m for m in env.store.list_messages(conv_id) if m.role == "user" and m.text == "hola"]
        assert len(saved) == 1 and saved[0].id == seen[-1]["message_id"]          # already in the store, before any turn of its own
        rest = read_turns(ws, 1)
    assert [e["message_id"] for e in rest if e["type"] == "turn_start"] == [seen[-1]["message_id"]]
    assert [m.text for m in env.store.list_messages(conv_id) if m.role == "user"] == ["[bgagent-nocont] investiga", "hola"]


def test_a_closed_session_with_a_waiting_message_does_not_lose_it_in_silence(env):
    """Bug 1257: Hokma shuts down while the owner's message waits. It stays saved, marked as not sent, the panel is
    told to mark it and the chat says so; the history shows it as not sent."""
    from hokma.i18n import t

    chat = engine(env, init_wait=30.0)
    with make_client(chat) as client, connect(client) as ws:
        first = turn(ws, "[bgagent-nocont] investiga")
        conv_id = first[0]["conversation_id"]
        session = chat.sessions[conv_id]
        until(ws, "agent_end")
        wait_for(lambda: session._owed == 1)
        ws.send_json({"type": "send", "conversation_id": conv_id, "text": "uno"})
        message_id = until(ws, "queued")[-1]["message_id"]
        client.portal.call(session.close)
        after = until(ws, "engine_notice")
    assert any(e["type"] == "queued_dropped" and e["message_id"] == message_id for e in after)
    assert after[-1]["message"] == t("chat.notice.unsent") and session._queued is None
    row = next(m for m in env.store.list_messages(conv_id) if m.id == message_id)
    assert row.meta.get("unsent") is True and chat._history_row(row)["unsent"] is True
    assert "pong" not in [m.text for m in env.store.list_messages(conv_id)]       # it never reached the engine


def test_a_late_turn_of_the_cli_after_init_timeout_with_the_owner_turn_open_documents_the_shift(env):
    """Declared risk of bug 1257, fixed here as documented behaviour (it is not a defect of the queue).

    The wait for the init gives up (init_timeout, here 1 s) and the owner's message goes through the pipe; the CLI
    opens the turn it announced only later (the fake: 6 s after the notice). The pipe is one: the CLI answers its own
    turn first, so the owner's turn closes with the SUMMARY text and the answer to his message comes one turn late.
    What is guaranteed, and what this test fixes:
    - nothing is lost or mixed into a wrong row: both of his messages are saved, once, in order;
    - the chat ends idle, without a watcher, the same process (nothing is killed);
    - his next message is answered normally (the shifted answer arrives as that turn's text).
    What is NOT guaranteed: that the answer of the turn that closes after init_timeout belongs to his message."""
    chat = engine(env, group_window=1.0, init_wait=1.0)
    with make_client(chat) as client, connect(client) as ws:
        first = turn(ws, "[bgagent-late] investiga")
        conv_id = first[0]["conversation_id"]
        session = chat.sessions[conv_id]
        wait_for(lambda: session._owed == 1 and session._cont is None)
        ws.send_json({"type": "send", "conversation_id": conv_id, "text": "hola"})
        mine = until(ws, "done", "error")                                         # closes after the late turn, ~6 s after the notice
        again = turn(ws, "otra", conversation_id=conv_id)
        assert session.state == "idle" and not session.watching and session._queued is None
    assert kinds(mine).count("queued") == 1 and kinds(mine).count("turn_start") == 1 and "error" not in kinds(mine)
    assert text_of(mine) == "agent A finished"                                    # the shift: the summary came as the answer of his turn
    assert text_of(again) == "pong" and "error" not in kinds(again)
    assert len(env.entries("start")) == 1
    assert [(m.role, m.text) for m in env.store.list_messages(conv_id)] == [
        ("user", "[bgagent-late] investiga"), ("assistant", "launched"), ("user", "hola"),
        ("assistant", "agent A finished"), ("user", "otra"), ("assistant", "pong")]

"""Subagents and background commands on the chat socket (SPEC-013, 3253-A): which agent did each step, what the chat
does not get any more, and what a reconnect and a saved answer carry. The CLI lines come from agent_turn.json and
background_turn.json, recorded with CLI 2.1.331."""
import pytest

from engine_support import connect, env, hard_timeout, make_client, posix_only, turn, until  # noqa: F401

from hokma.engine import stream
from hokma.engine.agents import MAX_AGENTS, MAX_STEPS, AgentTracker

pytestmark = posix_only


# ------------------------------------------------------------------------------------------------ pure helpers

def test_agent_call_reads_the_slug_and_a_short_description():
    call = stream.agent_call("Agent", {"subagent_type": "iris", "description": "Investigar  NQ", "prompt": "x"})
    assert call == {"slug": "iris", "description": "Investigar NQ"}
    assert stream.agent_call("Task", {"prompt": "p" * 500})["description"] == "p" * 200
    assert stream.agent_call("Agent", {})["slug"] == ""
    assert stream.agent_call("Read", {"description": "x"}) is None
    assert stream.agent_call("Agent", "not a dict") is None


def test_task_lines_and_the_launch_receipt():
    start = {"type": "system", "subtype": "task_started", "tool_use_id": "t1", "task_type": "local_bash", "description": "ls"}
    assert stream.task_event(start) == {"type": "task_start", "id": "t1", "task_type": "local_bash", "text": "ls"}
    end = {"type": "system", "subtype": "task_notification", "tool_use_id": "t1", "status": "completed", "summary": "ok"}
    assert stream.task_event(end) == {"type": "task_end", "id": "t1", "status": "completed", "text": "ok"}
    assert stream.task_event({"type": "system", "subtype": "task_updated", "tool_use_id": "t1"}) is None
    assert stream.task_event({"type": "system", "subtype": "task_started"}) is None
    assert stream.launch_receipt({"tool_use_result": {"isAsync": True}}) is True
    assert stream.launch_receipt({"tool_use_result": {"stdout": ""}}) is False


# ------------------------------------------------------------------------------------------------------ tracker

class Clock:
    now = 100.0

    def __call__(self) -> float:
        return self.now


def use(tracker, tool_id, name="Read", parent="", **tool_input):
    return tracker.on_tool_use({"id": tool_id, "name": name, "input": tool_input, "parent": parent})


def test_main_thread_steps_are_not_consumed():
    tracker = AgentTracker("turn")
    assert use(tracker, "t1", file_path="a.txt") is None
    assert tracker.on_tool_result({"id": "t1", "text": "x"}) is None
    assert tracker.agents == []


def test_a_nested_agent_hangs_from_its_parent_and_each_step_names_its_agent():
    tracker = AgentTracker("turn", Clock())
    [start] = use(tracker, "a1", "Agent", subagent_type="iris", description="Investigar")
    assert start["type"] == "agent_start" and start["slug"] == "iris" and start["parent"] == ""
    [nested] = use(tracker, "a2", "Task", parent="a1", subagent_type="hugo", description="Datos")
    assert nested["parent"] == "a1" and nested["agent_id"] == "a2"
    [step] = use(tracker, "s1", "Read", parent="a2", file_path="data/prices.csv")
    assert step["agent_id"] == "a2" and step["step"]["target"] == "data/prices.csv" and step["step"]["kind"] == "read_file"
    assert tracker.agent_of_step("s1") == {"id": "a2", "slug": "hugo"}
    assert tracker.agent_of_step("zzz") is None


def test_a_step_with_an_unknown_parent_stays_on_the_main_thread():
    assert use(AgentTracker("turn"), "s1", parent="never-started", file_path="a") is None


def test_the_receipt_of_a_background_agent_does_not_end_it_but_its_notification_does():
    tracker = AgentTracker("turn")
    use(tracker, "a1", "Agent", subagent_type="iris", description="d")
    assert tracker.on_tool_result({"id": "a1", "text": "Async agent launched", "launched": True}) == []
    assert tracker.agents[0]["status"] == "working"
    [end] = tracker.on_task({"type": "task_end", "id": "a1", "status": "completed", "text": "the report"}, [])
    assert end["type"] == "agent_end" and end["status"] == "done" and end["result"] == "the report"
    assert tracker.on_task({"type": "task_end", "id": "a1", "status": "failed", "text": "late"}, []) == []   # closed once


def test_a_synchronous_agent_ends_with_its_tool_result_and_an_error_is_an_error():
    tracker = AgentTracker("turn")
    use(tracker, "a1", "Agent", subagent_type="iris", description="d")
    use(tracker, "a2", "Agent", subagent_type="iris", description="e")
    [ok] = tracker.on_tool_result({"id": "a1", "text": "answer"})
    [bad] = tracker.on_tool_result({"id": "a2", "text": "boom", "is_error": True})
    assert (ok["status"], ok["result"]) == ("done", "answer") and (bad["status"], bad["result"]) == ("error", "boom")


def test_steps_are_throttled_to_two_a_second_and_the_last_one_wins():
    clock = Clock()
    tracker = AgentTracker("turn", clock)
    use(tracker, "a1", "Agent", subagent_type="iris", description="d")
    assert len(use(tracker, "s1", parent="a1", file_path="1")) == 1
    assert use(tracker, "s2", parent="a1", file_path="2") == []                       # held back
    assert tracker.on_tool_result({"id": "s2", "text": "r", "is_error": False}) == []   # still held back
    clock.now += 0.6
    assert [e["step"]["id"] for e in use(tracker, "s3", parent="a1", file_path="3")] == ["s3"]
    use(tracker, "s4", parent="a1", file_path="4")                                    # held back again
    events = tracker.on_task({"type": "task_end", "id": "a1", "status": "completed", "text": ""}, [])
    assert [e["type"] for e in events] == ["agent_step", "agent_end"] and events[0]["step"]["id"] == "s4"
    assert [s["id"] for s in tracker.agents[0]["steps"]] == ["s1", "s2", "s3", "s4"]   # nothing is lost, only the events


def test_a_result_of_a_step_past_the_live_cap_never_reaches_the_chat():
    from hokma.engine.agents import MAX_LIVE_STEPS
    tracker = AgentTracker("turn", Clock())
    use(tracker, "a1", "Agent", subagent_type="iris", description="Investigar")
    for n in range(MAX_LIVE_STEPS + 1):
        use(tracker, f"s{n}", parent="a1", file_path="a.txt")
    assert len(tracker.agents[0]["steps"]) == MAX_LIVE_STEPS
    last = f"s{MAX_LIVE_STEPS}"
    assert tracker.agent_of_step(last) == {"id": "a1", "slug": "iris"}
    assert tracker.on_tool_result({"id": last, "text": "x"}) == []      # consumed (not None): the session does not emit it


def test_a_turn_that_ends_stops_what_still_works():
    tracker = AgentTracker("turn")
    use(tracker, "a1", "Agent", subagent_type="iris", description="d")
    tracker.on_task({"type": "task_start", "id": "b1", "task_type": "local_bash", "text": "sleep"}, [{"id": "b1", "target": "sleep 9"}])
    out = tracker.finish()
    assert [(e["type"], e["status"]) for e in out] == [("agent_end", "stopped"), ("shell_end", "stopped")]
    assert tracker.finish() == []


def test_a_saved_answer_keeps_20_agents_and_40_steps_each_and_a_long_result_is_cut():
    tracker = AgentTracker("turn", Clock())
    for n in range(25):
        use(tracker, f"a{n}", "Agent", subagent_type="iris", description=f"d{n}")
    for n in range(60):
        use(tracker, f"s{n}", parent="a0", file_path=str(n))
        tracker.on_tool_result({"id": f"s{n}", "text": "r" * 900})
    tracker.on_tool_result({"id": "a0", "text": "x" * 9000})
    kept = tracker.stored()
    assert len(kept) == MAX_AGENTS == 20 and len(kept[0]["steps"]) == MAX_STEPS == 40
    assert len(kept[0]["result"]) == 4000 and len(kept[0]["steps"][0]["result"]) == 600
    assert len(tracker.snapshot()["agents"]) == 25                                      # the live list is whole


# ------------------------------------------------------------------------------------------- the socket

def kinds(events):
    return [e["type"] for e in events]


def test_an_agent_turn_names_the_agent_and_keeps_its_steps_out_of_the_chat(env):
    with make_client(env.engine()) as client, connect(client) as ws:
        events = turn(ws, "[agent] lee a.txt")
    assert kinds(events)[-1] == "done"
    [start] = [e for e in events if e["type"] == "agent_start"]
    assert start["slug"] == "general-purpose" and start["description"] == "Leer a.txt" and start["parent"] == ""
    steps = [e for e in events if e["type"] == "agent_step"]
    assert steps and all(s["agent_id"] == start["agent_id"] for s in steps)
    assert steps[0]["step"]["name"] == "Read" and steps[0]["step"]["kind"] == "read_file"
    [end] = [e for e in events if e["type"] == "agent_end"]
    assert end["status"] == "done" and end["result"] == "hola" and end["ended_at"] >= start["started_at"]
    assert kinds(events).index("agent_start") < kinds(events).index("agent_end") < kinds(events).index("done")
    assert "tool_use" not in kinds(events) and "tool_result" not in kinds(events) and "tool_output" not in kinds(events)
    conv_id = events[0]["conversation_id"]
    saved = env.store.list_messages(conv_id)[-1]
    assert "tools" not in saved.meta                                  # the main thread did no tool of its own
    [agent] = saved.meta["agents"]
    assert agent["status"] == "done" and agent["result"] == "hola" and agent["slug"] == "general-purpose"
    assert [s["name"] for s in agent["steps"]] == ["Read"] and agent["steps"][0]["is_error"] is False


def test_a_reopened_conversation_carries_its_agents_in_the_history(env):
    with make_client(env.engine()) as client, connect(client) as ws:
        first = turn(ws, "[agent] lee a.txt")
        conv_id = first[0]["conversation_id"]
    with make_client(env.engine()) as client, connect(client) as ws:
        ws.send_json({"type": "open", "conversation_id": conv_id})
        history = until(ws, "state")[-2]
    assert history["type"] == "history"
    [row] = [m for m in history["messages"] if m["role"] == "assistant"]
    assert row["agents"][0]["status"] == "done" and row["agents"][0]["steps"][0]["name"] == "Read"


def test_the_permission_of_a_step_of_an_agent_names_the_agent(env):
    with make_client(env.engine()) as client, connect(client) as ws:
        ws.send_json({"type": "send", "text": "[agent-perm] lee a.txt"})
        events = until(ws, "permission_request")
        request = events[-1]
        [start] = [e for e in events if e["type"] == "agent_start"]
        assert request["agent"] == {"id": start["agent_id"], "slug": "general-purpose"}
        assert request["tool"] == "Read"
        ws.send_json({"type": "permission_response", "conversation_id": request["conversation_id"],
                      "request_id": request["request_id"], "decision": "allow"})
        rest = until(ws, "done", "error")
    assert kinds(rest)[-1] == "done" and any(e["type"] == "agent_end" and e["status"] == "done" for e in rest)


def test_a_stop_with_an_agent_alive_closes_it_as_stopped_before_done(env):
    with make_client(env.engine()) as client, connect(client) as ws:
        ws.send_json({"type": "send", "text": "[agent-slow] lee a.txt"})
        events = until(ws, "delta")
        conv_id = events[0]["conversation_id"]
        [start] = [e for e in events if e["type"] == "agent_start"]
        ws.send_json({"type": "stop", "conversation_id": conv_id})
        rest = until(ws, "done", "error")
    [end] = [e for e in rest if e["type"] == "agent_end"]
    assert end["agent_id"] == start["agent_id"] and end["status"] == "stopped"
    assert kinds(rest).index("agent_end") < kinds(rest).index("done") and rest[-1]["stopped"] is True
    [agent] = env.store.list_messages(conv_id)[-1].meta["agents"]
    assert agent["status"] == "stopped" and agent["ended_at"]


def test_a_reconnect_in_the_middle_of_the_turn_gets_the_live_agents(env):
    with make_client(env.engine()) as client:
        with connect(client) as ws:
            ws.send_json({"type": "send", "text": "[agent-slow] lee a.txt"})
            events = until(ws, "delta")
            conv_id = events[0]["conversation_id"]
        with connect(client) as ws:
            ws.send_json({"type": "open", "conversation_id": conv_id})
            state = until(ws, "state")[-1]
            partial = state["partial"]
            assert [a["status"] for a in partial["agents"]] == ["working"] and partial["agents"][0]["slug"] == "general-purpose"
            ws.send_json({"type": "stop", "conversation_id": conv_id})
            until(ws, "done", "error")


def test_a_background_command_opens_and_closes_a_shell_line_with_its_text(env):
    with make_client(env.engine()) as client, connect(client) as ws:
        events = turn(ws, "[bgshell] corre algo")
    [start] = [e for e in events if e["type"] == "shell_start"]
    assert start["command"] == "sleep 3; echo listo" and start["started_at"]
    [end] = [e for e in events if e["type"] == "shell_end"]
    assert end["shell_id"] == start["shell_id"] and end["status"] == "done"
    assert kinds(events).index("shell_start") < kinds(events).index("shell_end") < kinds(events).index("done")
    assert "tool_use" in kinds(events)                                # the Bash call itself is a step of the main thread

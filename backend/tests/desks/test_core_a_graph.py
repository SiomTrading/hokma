"""Desk graph with fake analysts and a fake model (TA-14, TA-16, TA-17, TA-20)."""
from datetime import datetime, timezone

import pytest

from hokma.desks.core import graph as g
from hokma.desks.core.state import Message, ToolCall, initial_state

AS_OF = datetime(2026, 1, 2, 15, 0, tzinfo=timezone.utc)


class FakeModel:
    """Scripted ChatModel: records what it saw and answers by speaker (read from the system text)."""

    def __init__(self, tool_always=()):
        self.calls = []
        self.tool_always = set(tool_always)

    def chat(self, system, messages, tools=()):
        speaker = system.split("speaker=")[1]
        self.calls.append({"speaker": speaker, "system": system, "messages": list(messages), "tools": list(tools)})
        if speaker in self.tool_always and tools:
            return g.ModelReply(f"{speaker} thinking", (ToolCall(f"c{len(self.calls)}", "get_bars", {"symbol": "AAPL"}),))
        return g.ModelReply(f"{speaker} says something useful")


def prompt_for(role, speaker, state):
    return f"role={role};speaker={speaker}"


def start(*analysts):
    return initial_state(desk_id=1, symbol="aapl", line="SWC", as_of=AS_OF, horizon="weeks", analysts=analysts)


def flow(*keys, **kw):
    return g.FlowSpec(tuple(g.AnalystSpec(k) for k in keys), **kw)


def test_node_order_of_the_full_flow_TA_16():
    model = FakeModel()
    graph = g.build_desk_graph(flow("market", "news", debate_rounds=1, with_trader=True, risk_rounds=1), model, prompt_for)
    result = graph.invoke(start("market", "news"))
    assert result.path == ("analyst:market", "clear:market", "analyst:news", "clear:news", "bull", "bear", "judge", "trader",
                           "aggressive", "conservative", "neutral", "arbiter")
    assert result.state["reports"]["market"]["text"] == "market says something useful"
    assert result.state["debate"]["verdict"] == "judge says something useful"
    assert result.state["final_decision"] == "arbiter says something useful"
    assert result.state["debate"]["count"] == 2 and result.state["risk_debate"]["count"] == 3


def test_stages_are_optional_TA_16():
    result = g.build_desk_graph(flow("market"), FakeModel(), prompt_for).invoke(start("market"))
    assert result.path == ("analyst:market", "clear:market")
    result = g.build_desk_graph(flow("market", with_trader=True), FakeModel(), prompt_for).invoke(start("market"))
    assert result.path[-1] == "trader"
    with pytest.raises(g.GraphError):
        g.FlowSpec(())
    with pytest.raises(g.GraphError):
        flow("a", "a")


def test_debate_rounds_follow_the_count_TA_16():
    result = g.build_desk_graph(flow("market", debate_rounds=3), FakeModel(), prompt_for).invoke(start("market"))
    assert [n for n in result.path if n in ("bull", "bear")] == ["bull", "bear"] * 3 and result.path[-1] == "judge"


def test_conditional_edges_declare_every_target_TA_17():
    graph = g.build_desk_graph(flow("market", debate_rounds=1, with_trader=True, risk_rounds=1), FakeModel(), prompt_for)
    for source, (_, pmap) in graph._cond.items():
        assert pmap, source
        assert all(t in graph.node_names or t == g.END for t in pmap.values()), source
    assert set(graph._cond["bull"][1]) == {"bull", "bear", "judge"}
    assert set(graph._cond["neutral"][1]) == {"aggressive", "conservative", "neutral", "arbiter"}


def test_router_never_reaches_an_undeclared_destination_TA_17():
    b = g.StateGraph()
    b.add_node("a", lambda s: {})
    b.add_node("b", lambda s: {})
    b.add_edge(g.START, "a")
    b.add_conditional_edges("a", lambda s: "elsewhere", {"b": "b"})
    b.add_edge("b", g.END)
    with pytest.raises(g.UndeclaredRoute, match="elsewhere"):
        b.compile().invoke({})
    # The safe wrapper turns a fall-through label into the declared fallback.
    c = g.StateGraph()
    c.add_node("a", lambda s: {})
    c.add_node("b", lambda s: {"seen": True})
    c.add_edge(g.START, "a")
    c.add_conditional_edges("a", g.safe_router(lambda s: "elsewhere", {"b": "b"}, "b"), {"b": "b"})
    c.add_edge("b", g.END)
    assert c.compile().invoke({}).state["seen"] is True
    with pytest.raises(g.GraphError):
        g.safe_router(lambda s: "x", {"b": "b"}, "zzz")


def test_compile_rejects_broken_graphs_TA_17():
    def base():
        b = g.StateGraph()
        b.add_node("a", lambda s: {})
        return b

    b = base()
    with pytest.raises(g.GraphError, match="entry"):
        b.compile()
    b = base()
    b.add_edge(g.START, "a")
    with pytest.raises(g.GraphError, match="outgoing"):
        b.compile()
    b = base()
    b.add_edge(g.START, "a")
    b.add_conditional_edges("a", lambda s: "x", {"x": "ghost"})
    with pytest.raises(g.GraphError, match="unknown"):
        b.compile()
    b = base()
    b.add_node("island", lambda s: {})
    b.add_edge(g.START, "a")
    b.add_edge("a", g.END)
    b.add_edge("island", g.END)
    with pytest.raises(g.GraphError, match="reachable"):
        b.compile()
    with pytest.raises(g.GraphError):
        b.add_conditional_edges("a", lambda s: "x", {})
    with pytest.raises(g.GraphError):
        g.complete_path_map()


def test_recursion_limit_stops_a_loop_TA_17():
    b = g.StateGraph()
    b.add_node("a", lambda s: {})
    b.add_edge(g.START, "a")
    b.add_edge("a", "a")
    with pytest.raises(g.GraphError, match="recursion limit"):
        b.compile(recursion_limit=5).invoke({})


def test_messages_are_cleared_between_analysts_TA_14():
    model = FakeModel()
    result = g.build_desk_graph(flow("market", "news"), model, prompt_for).invoke(start("market", "news"))
    second = [c for c in model.calls if c["speaker"] == "news"][0]
    texts = [m.content for m in second["messages"]]
    assert len(texts) == 1
    assert "AAPL" in texts[0] and "2026-01-02" in texts[0] and texts[0].lower() != "continue"
    assert "Proceed with your assigned analysis" in texts[0]
    assert len(result.state["messages"]) == 1


def test_msg_clear_removes_every_message_and_ids_are_unique_TA_14():
    state = start("market")
    state["messages"] = [Message("user", "a", "1"), Message("assistant", "b", "2")]
    update = g.msg_clear_node(state)
    after = g.apply_message_updates(state["messages"], update["messages"])
    assert len(after) == 1 and after[0].id and after[0].id not in ("1", "2")


def test_analyst_tool_loop_ends_at_its_cap_TA_20():
    model = FakeModel(tool_always={"market"})
    ran = []
    spec = g.AnalystSpec("market", tools=({"name": "get_bars"},), run_tool=lambda call: (ran.append(call.name) or "bars: 3"), max_tool_rounds=2)
    graph = g.build_desk_graph(g.FlowSpec((spec,)), model, prompt_for)
    result = graph.invoke(start("market"))
    assert result.path == ("analyst:market", "tools:market", "analyst:market", "tools:market", "analyst:market", "clear:market")
    assert ran == ["get_bars", "get_bars"] and result.state["tool_rounds"]["market"] == 2
    assert [bool(c["tools"]) for c in model.calls] == [True, True, False]       # the last call goes out without tools
    assert result.state["reports"]["market"]["status"] == "ok"
    tool_msgs = [c["messages"][-1] for c in model.calls[1:]]
    assert all(m.role == "tool" and m.content == "bars: 3" for m in tool_msgs)


def test_analyst_without_tool_calls_goes_straight_to_clear_TA_20():
    spec = g.AnalystSpec("market", tools=({"name": "get_bars"},))
    result = g.build_desk_graph(g.FlowSpec((spec,)), FakeModel(), prompt_for).invoke(start("market"))
    assert result.path == ("analyst:market", "clear:market")


def test_empty_report_is_declared_absent_TA_20():
    class Silent:
        def chat(self, system, messages, tools=()):
            return g.ModelReply("")

    result = g.build_desk_graph(flow("market", debate_rounds=1), Silent(), prompt_for).invoke(start("market"))
    assert result.state["reports"]["market"]["status"] == "absent"
    assert "not available, not an empty finding" in result.state["reports"]["market"]["text"]


def test_debaters_see_the_summary_and_never_invent_a_rebuttal_TA_16():
    model = FakeModel()
    g.build_desk_graph(flow("market", debate_rounds=2), model, prompt_for).invoke(start("market"))
    bull_first = [c for c in model.calls if c["speaker"] == "bull"][0]["messages"][0].content
    assert "has not spoken yet" in bull_first
    bear_first = [c for c in model.calls if c["speaker"] == "bear"][0]["messages"][0].content
    assert "bull says something useful" in bear_first

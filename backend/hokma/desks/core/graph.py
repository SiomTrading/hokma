"""Desk graph: analysts, debate, judge, trader and risk voices as one run of connected nodes (SPEC-017: TA-14, TA-16, TA-17, TA-20).

The graph is a small engine of its own: nodes are functions of the state, edges are fixed or conditional, and a
conditional edge always carries a complete `path_map`, so a router can never send the run to an undeclared place.
Language models are reached through the `ChatModel` protocol below; the `hokma.llm` layer provides the real one.

Patterns the module fixes for every desk:
- Between two analysts the message history is cleared and replaced by one placeholder anchored to the instrument and the
  date, so a provider never reads a bare word as the task (TA-14).
- An analyst talks to its tools in a loop that ends at a fixed number of rounds (TA-20).
- A debate keeps a rolling summary, not the history (TA-24, see state.py).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, Sequence

from hokma.desks.core.state import (
    DeskState, Message, RemoveMessage, ToolCall, make_report, opponent_or_opening, record_round, report_or_absent,
)

START = "__start__"
END = "__end__"
DEFAULT_RECURSION_LIMIT = 100
DEFAULT_MAX_TOOL_ROUNDS = 3
RISK_VOICES = ("aggressive", "conservative", "neutral")

Node = Callable[[DeskState], Mapping[str, Any]]
Router = Callable[[DeskState], str]
ToolSchema = Mapping[str, Any]


class GraphError(Exception):
    """The graph is built wrongly or ran past its limit."""


class UndeclaredRoute(GraphError):
    """A router returned a label that its `path_map` does not declare."""


@dataclass(frozen=True)
class ModelReply:
    content: str
    tool_calls: tuple[ToolCall, ...] = ()


class ChatModel(Protocol):
    """What the graph needs from a language model: one call with a system text, messages and optional tool schemas."""

    def chat(self, system: str, messages: Sequence[Message], tools: Sequence[ToolSchema] = ()) -> ModelReply: ...


@dataclass(frozen=True)
class RunResult:
    state: DeskState
    path: tuple[str, ...]


def new_message_id() -> str:
    return uuid.uuid4().hex[:12]


def apply_message_updates(current: Sequence[Message], updates: Sequence[Message | RemoveMessage]) -> list[Message]:
    """Removals drop the message with that id; any other item is appended (given an id when it has none)."""
    out = list(current)
    for item in updates:
        if isinstance(item, RemoveMessage):
            out = [m for m in out if m.id != item.id]
        else:
            out.append(item if item.id else Message(item.role, item.content, new_message_id(), item.tool_calls, item.tool_call_id, item.name))
    return out


def complete_path_map(*targets: str) -> dict[str, str]:
    """A path map where every label is its own destination: a router that returns any declared name is always valid."""
    if not targets:
        raise GraphError("a path map needs at least one target")
    return {t: t for t in targets}


def safe_router(router: Router, path_map: Mapping[str, str], fallback: str) -> Router:
    """Wrap a router so a label outside the map becomes `fallback` (which must be in the map)."""
    if fallback not in path_map:
        raise GraphError(f"fallback {fallback!r} is not in the path map")

    def wrapped(state: DeskState) -> str:
        label = router(state)
        return label if label in path_map else fallback

    return wrapped


class StateGraph:
    def __init__(self) -> None:
        self._nodes: dict[str, Node] = {}
        self._edges: dict[str, str] = {}
        self._cond: dict[str, tuple[Router, dict[str, str]]] = {}
        self._entry: str | None = None

    def add_node(self, name: str, fn: Node) -> None:
        if name in (START, END) or name in self._nodes:
            raise GraphError(f"node name {name!r} is reserved or already used")
        self._nodes[name] = fn

    def add_edge(self, source: str, target: str) -> None:
        if source == START:
            self._entry = target
            return
        if source in self._edges or source in self._cond:
            raise GraphError(f"node {source!r} already has an outgoing edge")
        self._edges[source] = target

    def add_conditional_edges(self, source: str, router: Router, path_map: Mapping[str, str]) -> None:
        if source in self._edges or source in self._cond:
            raise GraphError(f"node {source!r} already has an outgoing edge")
        if not path_map:
            raise GraphError(f"conditional edge of {source!r} needs a path map")
        self._cond[source] = (router, dict(path_map))

    def compile(self, recursion_limit: int = DEFAULT_RECURSION_LIMIT) -> "CompiledGraph":
        if self._entry is None:
            raise GraphError("the graph has no entry node: add_edge(START, node)")
        known = set(self._nodes) | {END}
        for source, target in self._edges.items():
            if source not in self._nodes or target not in known:
                raise GraphError(f"edge {source!r} -> {target!r} points to an unknown node")
        for source, (_, pmap) in self._cond.items():
            bad = sorted(t for t in pmap.values() if t not in known)
            if source not in self._nodes or bad:
                raise GraphError(f"conditional edge of {source!r} points to unknown node(s) {bad}")
        if self._entry not in self._nodes:
            raise GraphError(f"entry node {self._entry!r} is unknown")
        loose = sorted(n for n in self._nodes if n not in self._edges and n not in self._cond)
        if loose:
            raise GraphError(f"node(s) {loose} have no outgoing edge")
        reach, todo = set(), [self._entry]
        while todo:
            node = todo.pop()
            if node in reach or node == END:
                continue
            reach.add(node)
            todo += [self._edges[node]] if node in self._edges else list(self._cond[node][1].values())
        if unreachable := sorted(set(self._nodes) - reach):
            raise GraphError(f"node(s) {unreachable} are not reachable from the entry")
        return CompiledGraph(dict(self._nodes), dict(self._edges), dict(self._cond), self._entry, recursion_limit)


class CompiledGraph:
    def __init__(self, nodes, edges, cond, entry, recursion_limit) -> None:
        self._nodes, self._edges, self._cond, self._entry, self._limit = nodes, edges, cond, entry, recursion_limit

    @property
    def node_names(self) -> tuple[str, ...]:
        return tuple(self._nodes)

    def invoke(self, state: DeskState) -> RunResult:
        current: DeskState = dict(state)    # type: ignore[assignment]
        path: list[str] = []
        node = self._entry
        while node != END:
            if len(path) >= self._limit:
                raise GraphError(f"recursion limit {self._limit} reached; path tail: {path[-6:]}")
            path.append(node)
            update = self._nodes[node](current) or {}
            for key, value in update.items():
                if key == "messages":
                    current["messages"] = apply_message_updates(current.get("messages", []), value)
                else:
                    current[key] = value    # type: ignore[literal-required]
            if node in self._edges:
                node = self._edges[node]
                continue
            router, pmap = self._cond[node]
            label = router(current)
            if label not in pmap:
                raise UndeclaredRoute(f"router of {node!r} returned {label!r}; declared: {sorted(pmap)}")
            node = pmap[label]
        return RunResult(current, tuple(path))


# ---- Msg Clear (TA-14) -------------------------------------------------------------------------------------------

def msg_clear_node(state: DeskState) -> dict[str, Any]:
    """Clear the history and leave one placeholder anchored to the instrument and the date."""
    removals = [RemoveMessage(m.id) for m in state.get("messages", [])]
    context = state.get("instrument_context") or f"The instrument under analysis is {state.get('symbol', 'unknown')}."
    text = (f"Proceed with your assigned analysis for this workflow. {context} "
            f"The analysis cut-off is {state.get('as_of', 'the requested date')}.")
    return {"messages": [*removals, Message("user", text)]}


# ---- analyst with tools (TA-20) ----------------------------------------------------------------------------------

def analyst_node(key: str, model: ChatModel, system_for: Callable[[DeskState], str], tools: Sequence[ToolSchema] = (),
                 max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS) -> Node:
    """One analyst turn. Once its tool rounds reach the cap, the call goes out without tools to force the final report."""

    def run(state: DeskState) -> dict[str, Any]:
        rounds = state.get("tool_rounds", {}).get(key, 0)
        offered = tuple(tools) if rounds < max_tool_rounds else ()
        reply = model.chat(system_for(state), state.get("messages", []), offered)
        calls = reply.tool_calls if offered else ()
        update: dict[str, Any] = {"messages": [Message("assistant", reply.content or "", tool_calls=tuple(calls))]}
        if not calls:
            update["reports"] = {**state.get("reports", {}), key: make_report(key, reply.content)}
        return update

    return run


def tool_node(key: str, run_tool: Callable[[ToolCall], str]) -> Node:
    """Run the tool calls of the last assistant message and count one round for this analyst."""

    def run(state: DeskState) -> dict[str, Any]:
        last = state["messages"][-1]
        results = [Message("tool", run_tool(call), tool_call_id=call.id, name=call.name) for call in last.tool_calls]
        rounds = dict(state.get("tool_rounds", {}))
        rounds[key] = rounds.get(key, 0) + 1
        return {"messages": results, "tool_rounds": rounds}

    return run


def analyst_router(key: str, tools_node: str, clear_node: str, max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS) -> Router:
    def route(state: DeskState) -> str:
        last = state["messages"][-1] if state.get("messages") else None
        if last is not None and last.tool_calls and state.get("tool_rounds", {}).get(key, 0) < max_tool_rounds:
            return tools_node
        return clear_node

    return route


# ---- full flow (TA-16) -------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class AnalystSpec:
    key: str
    tools: tuple[ToolSchema, ...] = ()
    run_tool: Callable[[ToolCall], str] | None = None
    max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS


@dataclass(frozen=True)
class FlowSpec:
    """Which stages a desk uses. Analysts are required; the other stages are optional and always in the same order."""

    analysts: tuple[AnalystSpec, ...]
    debate_rounds: int = 0       # rounds of bull/bear before the judge; 0 skips the debate and the judge
    with_trader: bool = False
    risk_rounds: int = 0         # rounds of the three risk voices before the arbiter; 0 skips risk

    def __post_init__(self) -> None:
        if not self.analysts:
            raise GraphError("a flow needs at least one analyst")
        keys = [a.key for a in self.analysts]
        if len(set(keys)) != len(keys):
            raise GraphError("analyst keys must be unique")
        if self.debate_rounds < 0 or self.risk_rounds < 0:
            raise GraphError("rounds cannot be negative")


PromptFor = Callable[[str, str, DeskState], str]   # (role, speaker, state) -> system prompt


def reports_text(state: DeskState) -> str:
    reports = state.get("reports", {})
    return "\n\n".join(f"### {key}\n{report_or_absent(r.get('text') if r.get('status') == 'ok' else '', key)}" for key, r in reports.items())


def _text_node(model: ChatModel, role: str, speaker: str, prompt_for: PromptFor, user_for: Callable[[DeskState], str],
               apply: Callable[[DeskState, str], dict[str, Any]]) -> Node:
    def run(state: DeskState) -> dict[str, Any]:
        reply = model.chat(prompt_for(role, speaker, state), [Message("user", user_for(state))])
        return apply(state, (reply.content or "").strip())

    return run


def build_desk_graph(flow: FlowSpec, model: ChatModel, prompt_for: PromptFor, *, recursion_limit: int = DEFAULT_RECURSION_LIMIT) -> CompiledGraph:
    """Assemble the standard flow: analysts one after another, then the optional debate, trader and risk stages."""
    g = StateGraph()

    def analyst_prompt(key: str) -> Callable[[DeskState], str]:
        return lambda state: prompt_for("analyst", key, state)

    # Where each optional stage starts; END when nothing follows.
    first_risk = "aggressive" if flow.risk_rounds else END
    first_trader = "trader" if flow.with_trader else first_risk
    first_debate = "bull" if flow.debate_rounds else first_trader

    specs = flow.analysts
    for i, spec in enumerate(specs):
        agent, tools_n, clear = f"analyst:{spec.key}", f"tools:{spec.key}", f"clear:{spec.key}"
        g.add_node(agent, analyst_node(spec.key, model, analyst_prompt(spec.key), spec.tools, spec.max_tool_rounds))
        g.add_node(tools_n, tool_node(spec.key, spec.run_tool or (lambda call: "(no tool runner for this analyst)")))
        g.add_node(clear, msg_clear_node)
        g.add_conditional_edges(agent, analyst_router(spec.key, tools_n, clear, spec.max_tool_rounds), complete_path_map(tools_n, clear))
        g.add_edge(tools_n, agent)
        g.add_edge(clear, f"analyst:{specs[i + 1].key}" if i < len(specs) - 1 else first_debate)
    g.add_edge(START, f"analyst:{specs[0].key}")

    if flow.debate_rounds:
        def side(name: str, other: str) -> Node:
            def user_for(state: DeskState) -> str:
                debate = state["debate"]
                theirs = opponent_or_opening(debate["last_response"] if debate["last_speaker"] == other else "", other)
                return (f"Analyst reports:\n{reports_text(state)}\n\nDebate so far (summary): {debate['summary'] or '(none)'}\n\n"
                        f"Last argument of the {other} side:\n{theirs}")

            def apply(state: DeskState, text: str) -> dict[str, Any]:
                return {"debate": record_round(state["debate"], name, text)}

            return _text_node(model, "researcher", name, prompt_for, user_for, apply)

        g.add_node("bull", side("bull", "bear"))
        g.add_node("bear", side("bear", "bull"))
        debate_map = complete_path_map("bull", "bear", "judge")

        def debate_router(state: DeskState) -> str:
            debate = state["debate"]
            if debate["count"] >= 2 * flow.debate_rounds:
                return "judge"
            return "bear" if debate["last_speaker"] == "bull" else "bull"

        for name in ("bull", "bear"):
            g.add_conditional_edges(name, safe_router(debate_router, debate_map, "judge"), debate_map)
        g.add_node("judge", _text_node(
            model, "judge", "judge", prompt_for,
            lambda s: f"Analyst reports:\n{reports_text(s)}\n\nDebate summary: {s['debate']['summary'] or '(none)'}",
            lambda s, text: {"debate": {**s["debate"], "verdict": text}}))
        g.add_edge("judge", first_trader)

    if flow.with_trader:
        g.add_node("trader", _text_node(
            model, "trader", "trader", prompt_for,
            lambda s: f"Verdict of the case:\n{s['debate']['verdict'] or '(no debate in this run)'}\n\nAnalyst reports:\n{reports_text(s)}",
            lambda s, text: {"trader_plan": text}))
        g.add_edge("trader", first_risk)

    if flow.risk_rounds:
        def voice(name: str) -> Node:
            def user_for(state: DeskState) -> str:
                return (f"Proposal under review:\n{state.get('trader_plan') or '(no proposal in this run)'}\n\n"
                        f"Risk discussion so far (summary): {state['risk_debate']['summary'] or '(none)'}")

            return _text_node(model, "risk", name, prompt_for, user_for,
                              lambda s, text: {"risk_debate": record_round(s["risk_debate"], name, text)})

        risk_map = complete_path_map(*RISK_VOICES, "arbiter")

        def risk_router(state: DeskState) -> str:
            risk = state["risk_debate"]
            if risk["count"] >= len(RISK_VOICES) * flow.risk_rounds:
                return "arbiter"
            return RISK_VOICES[(RISK_VOICES.index(risk["last_speaker"]) + 1) % len(RISK_VOICES)]

        for name in RISK_VOICES:
            g.add_node(name, voice(name))
            g.add_conditional_edges(name, safe_router(risk_router, risk_map, "arbiter"), risk_map)
        g.add_node("arbiter", _text_node(
            model, "risk", "arbiter", prompt_for,
            lambda s: f"Proposal:\n{s.get('trader_plan') or '(none)'}\n\nRisk discussion summary: {s['risk_debate']['summary'] or '(none)'}",
            lambda s, text: {"final_decision": text, "risk_debate": {**s["risk_debate"], "verdict": text}}))
        g.add_edge("arbiter", END)

    return g.compile(recursion_limit)

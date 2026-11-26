"""Subagents and background commands of one chat turn: what the panel on the right shows (SPEC-013).

The adapter marks every step with the agent that did it (`parent`). The tracker turns that into the list of agents of the
turn, with their steps, and says which socket event each change produces. Steps of an agent never reach the chat as
`tool_use`: the session sends them only as `agent_*` events."""
from __future__ import annotations

import time
from typing import Any, Callable

from hokma.engine import stream

MAX_AGENTS = 20                # agents kept with a saved answer
MAX_STEPS = 40                 # steps kept per agent in a saved answer
MAX_LIVE_STEPS = 200           # steps held in memory per agent while the turn runs
MAX_STEP_RESULT = 600          # characters of the result of a step
MAX_AGENT_RESULT = 4000        # characters of the answer of an agent
STEP_GAP = 0.5                 # seconds: at most two `agent_step` per second per agent, the last one wins
MAX_SHELL_COMMAND = 200

_TASK_STATUS = {"completed": "done", "failed": "error"}


def _now_ms() -> int:
    return int(time.time() * 1000)


class AgentTracker:
    def __init__(self, turn_id: str, clock: Callable[[], float] = time.monotonic) -> None:
        self.turn_id = turn_id
        self.saved_as: tuple[str, str] | None = None     # (conversation id, message id) once the answer that launched them is saved
        self._clock = clock
        self.agents: list[dict[str, Any]] = []
        self.shells: list[dict[str, Any]] = []
        self._agent: dict[str, dict[str, Any]] = {}
        self._owner: dict[str, str] = {}                 # step id -> agent id
        self._shell: dict[str, dict[str, Any]] = {}
        self._last_step_at: dict[str, float] = {}
        self._dirty: dict[str, dict[str, Any]] = {}      # agent id -> the step that was held back

    # ------------------------------------------------------------------------------------------------- events

    def on_tool_use(self, ev: dict[str, Any]) -> list[dict[str, Any]] | None:
        """None: a step of the main thread, the session handles it as always. A list: consumed here (maybe empty)."""
        parent = str(ev.get("parent") or "")
        name, tool_input = str(ev.get("name") or ""), ev.get("input")
        call = stream.agent_call(name, tool_input)
        if call is not None:
            agent = {"id": str(ev.get("id") or ""), "slug": call["slug"], "description": call["description"],
                     "parent": parent if parent in self._agent else "", "status": "working",
                     "started_at": _now_ms(), "ended_at": None, "step": None, "steps": [], "result": None}
            self.agents.append(agent)
            self._agent[agent["id"]] = agent
            return [{"type": "agent_start", "turn_id": self.turn_id, "agent_id": agent["id"], "slug": agent["slug"],
                     "description": agent["description"], "parent": agent["parent"], "started_at": agent["started_at"]}]
        agent = self._agent.get(parent)
        if agent is None:
            return None
        step = {"id": str(ev.get("id") or ""), "name": name, **stream.summarize_tool(name, tool_input),
                "is_error": None, "result": None}
        agent["step"] = step["id"]
        self._owner[step["id"]] = agent["id"]            # always: the result of a step past the cap is still the agent's
        if len(agent["steps"]) < MAX_LIVE_STEPS:
            agent["steps"].append(step)
        return self._step_event(agent, step)

    def on_tool_result(self, ev: dict[str, Any]) -> list[dict[str, Any]] | None:
        tool_id = str(ev.get("id") or "")
        text = str(ev.get("text") or "")
        is_error = bool(ev.get("is_error"))
        agent = self._agent.get(tool_id)
        if agent is not None:
            if ev.get("launched"):                       # the receipt of a background agent: it is still working
                return []
            return self._end(agent, "error" if is_error else "done", text)
        owner = self._agent.get(self._owner.get(tool_id, ""))
        if owner is None:
            return None
        for step in owner["steps"]:
            if step["id"] == tool_id:
                step["is_error"], step["result"] = is_error, text[:MAX_STEP_RESULT]
                return self._step_event(owner, step)
        return []

    def on_task(self, ev: dict[str, Any], tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """`task_start` / `task_end` from the CLI: a background command opens a shell line, the end of a background
        agent carries its answer. `tools` are the lines of the main thread, where the command text is."""
        task_id = str(ev.get("id") or "")
        if ev["type"] == "task_start":
            if ev.get("task_type") != "local_bash" or task_id in self._shell:
                return []
            command = next((t.get("target") for t in tools if t.get("id") == task_id), None)
            if command is None:
                command = next((s.get("target") for a in self.agents for s in a["steps"] if s["id"] == task_id), "")
            shell = {"id": task_id, "command": str(command or ev.get("text") or "")[:MAX_SHELL_COMMAND],
                     "status": "running", "started_at": _now_ms(), "ended_at": None}
            self.shells.append(shell)
            self._shell[task_id] = shell
            return [{"type": "shell_start", "turn_id": self.turn_id, "shell_id": task_id, "command": shell["command"],
                     "started_at": shell["started_at"]}]
        status = _TASK_STATUS.get(str(ev.get("status") or ""), "stopped")
        agent = self._agent.get(task_id)
        if agent is not None:
            return self._end(agent, status, str(ev.get("text") or ""))
        shell = self._shell.get(task_id)
        if shell is not None and shell["status"] == "running":
            return [self._end_shell(shell, status)]
        return []

    def working(self) -> bool:
        """True while an agent or a background command of this turn still runs (bug 1257: the turn can end before they do)."""
        return any(a["status"] == "working" for a in self.agents) or any(s["status"] == "running" for s in self.shells)

    def finish(self) -> list[dict[str, Any]]:
        """Whatever still works is stopped: the conversation closes for real (a stop, a timeout, the process closed)."""
        out: list[dict[str, Any]] = []
        for agent in self.agents:
            if agent["status"] == "working":
                out += self._end(agent, "stopped", "")
        for shell in self.shells:
            if shell["status"] == "running":
                out.append(self._end_shell(shell, "stopped"))
        return out

    # --------------------------------------------------------------------------------------------------- reads

    def agent_of_step(self, tool_use_id: str) -> dict[str, str] | None:
        agent = self._agent.get(self._owner.get(tool_use_id, ""))
        return None if agent is None else {"id": agent["id"], "slug": agent["slug"]}

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        return {"agents": [{**a, "steps": [dict(s) for s in a["steps"]]} for a in self.agents],
                "shells": [dict(s) for s in self.shells]}

    def stored(self) -> list[dict[str, Any]]:
        """What travels with the saved answer: 20 agents, 40 steps each, the answer of an agent whole."""
        return [{**a, "steps": [dict(s) for s in a["steps"][:MAX_STEPS]]} for a in self.agents[:MAX_AGENTS]]

    # ------------------------------------------------------------------------------------------------ internals

    def _step_event(self, agent: dict[str, Any], step: dict[str, Any]) -> list[dict[str, Any]]:
        now = self._clock()
        if now - self._last_step_at.get(agent["id"], -STEP_GAP) < STEP_GAP:
            self._dirty[agent["id"]] = step
            return []
        self._last_step_at[agent["id"]] = now
        self._dirty.pop(agent["id"], None)
        return [self._step_payload(agent, step)]

    def _step_payload(self, agent: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
        return {"type": "agent_step", "turn_id": self.turn_id, "agent_id": agent["id"],
                "step": {k: step[k] for k in ("id", "name", "kind", "target", "is_error")}}

    def _end(self, agent: dict[str, Any], status: str, text: str) -> list[dict[str, Any]]:
        if agent["status"] != "working":
            return []
        out = []
        held = self._dirty.pop(agent["id"], None)
        if held is not None:
            out.append(self._step_payload(agent, held))
        agent["status"], agent["ended_at"], agent["result"] = status, _now_ms(), text[:MAX_AGENT_RESULT]
        out.append({"type": "agent_end", "turn_id": self.turn_id, "agent_id": agent["id"], "status": status,
                    "ended_at": agent["ended_at"], "result": agent["result"]})
        return out

    def _end_shell(self, shell: dict[str, Any], status: str) -> dict[str, Any]:
        shell["status"], shell["ended_at"] = status, _now_ms()
        return {"type": "shell_end", "turn_id": self.turn_id, "shell_id": shell["id"], "status": status,
                "ended_at": shell["ended_at"]}

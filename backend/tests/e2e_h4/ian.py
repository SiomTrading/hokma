"""A fake model engine for Ian: the model answers with the text a model would write (a JSON tool call, or at the end the report), and the engine does what a real
one does, which is to offer the tools of his agent file and nothing else, run each call through the server entry point as Ian and hand the answer back."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from e2e_h4.support import IAN, call, caller

from hokma.db.seeds import parse_frontmatter
from hokma.factory import researcher
from hokma.llm.jsonx import extract_json
from hokma.llm.protocol import LLMReply

AGENT_FILE = Path(__file__).resolve().parents[3] / "kit" / "template" / "agents" / "strategy-creator.md"


def offered_tools() -> tuple[str, ...]:
    """The tools of the agent file, as the plain names the server knows."""
    line = parse_frontmatter(AGENT_FILE.read_text(encoding="utf-8"))["tools"]
    return tuple(t.strip().removeprefix(researcher.MCP_PREFIX) for t in line.split(","))


class FakeLLM:
    """The same shape as the fake of the researcher tests: `complete` returns the next scripted text. A step may be a function of what came back."""

    model = "fake-model"

    def __init__(self, *steps: Any) -> None:
        self.steps, self.seen = list(steps), []

    def complete(self, messages, *, schema=None, max_tokens=4096):
        self.seen.append(messages[-1]["content"])
        step = self.steps.pop(0)
        last = json.loads(messages[-1]["content"]) if messages[-1]["content"] else None
        return LLMReply(text=step(last) if callable(step) else step, model=self.model)


def tool(name: str, **args: Any) -> str:
    return json.dumps({"tool": name, "args": args})


class Session:
    """One conversation of Ian: the engine loop over a fake model. `answers` keeps (tool, answer, is_error) of every call, `refused` the calls the engine did not allow."""

    def __init__(self, model: FakeLLM, *, conversation: int = 1, task: int = 1) -> None:
        self.model, self.who = model, caller(IAN, conversation, task)
        self.offered, self.answers, self.refused = offered_tools(), [], []

    def run(self) -> str:
        """Drive the model until it writes its report (an answer with no tool call). Returns that text."""
        observation: Any = None
        while True:
            text = self.model.complete([{"role": "user", "content": json.dumps(observation, default=str) if observation is not None else ""}]).text
            data = extract_json(text)
            if "tool" not in data:
                return text
            name = data["tool"]
            if name not in self.offered:                                           # the engine offers the tools of the agent file and no other
                observation = {"error_code": "blocked", "message": f"{name} is not one of your tools"}
                self.refused.append(name)
                continue
            answer, is_error = call(name, data["args"], self.who)
            self.answers.append((name, answer, is_error))
            observation = answer

    def last(self, name: str) -> Any:
        return next(a for n, a, _e in reversed(self.answers) if n == name)

    def guard(self):
        from hokma.kit import backtest_tools

        return backtest_tools.research_session(self.who)


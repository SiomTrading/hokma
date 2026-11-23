"""Evaluation harness for desk agents (SPEC-017: ED-040).

An eval case gives an agent a task, the snapshot of facts it may use and the tools it may call. The harness runs the agent and
scores the answer with checks that need no model: did it call the tools the task needs, does the answer hold the facts it must
hold, and, above all, does every number in the answer exist in the snapshot or in what a tool returned. A number with no source is
the failure the desks must never ship, so it fails the case whatever else is right.

An agent here is any callable `agent(task, tools) -> AgentAnswer`: a real desk agent behind its prompt, or a fake in a test.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from hokma.desks.core import numbers

Tool = Callable[..., Any]


@dataclass(frozen=True)
class AgentAnswer:
    text: str
    tool_calls: tuple[str, ...] = ()
    error: str = ""


@dataclass(frozen=True)
class EvalCase:
    id: str
    task: str
    snapshot: Any = field(default_factory=dict)              # the facts the answer may state
    must_call: tuple[str, ...] = ()                          # tools the task cannot be solved without
    must_include: tuple[str, ...] = ()                       # substrings the answer must hold (case-insensitive)
    must_not_include: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] | None = None             # None: any tool of the harness
    max_seconds: float | None = None
    ignore_numbers_below: float = 0.0                        # skip small numbers such as list markers


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    passed: bool
    failures: tuple[str, ...]
    unverified: tuple[str, ...] = ()
    seconds: float = 0.0
    tool_calls: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvalReport:
    results: tuple[CaseResult, ...]

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return len(self.results) - self.passed

    @property
    def ok(self) -> bool:
        return bool(self.results) and self.failed == 0

    def summary(self) -> str:
        lines = [f"{self.passed}/{len(self.results)} cases passed"]
        for r in self.results:
            if not r.passed:
                lines.append(f"- {r.case_id}: " + "; ".join(r.failures))
        return "\n".join(lines)


class _Recorder:
    """The tools of the harness, each one recording that it was called and what it returned."""

    def __init__(self, tools: Mapping[str, Tool], allowed: tuple[str, ...] | None) -> None:
        self.calls: list[str] = []
        self.returned: list[Any] = []
        self.denied: list[str] = []
        self._tools, self._allowed = dict(tools), allowed

    def as_dict(self) -> dict[str, Tool]:
        def wrap(name: str, fn: Tool) -> Tool:
            def call(*args: Any, **kwargs: Any) -> Any:
                if self._allowed is not None and name not in self._allowed:
                    self.denied.append(name)
                    raise PermissionError(f"tool {name} is not allowed in this case")
                self.calls.append(name)
                result = fn(*args, **kwargs)
                self.returned.append(result)
                return result
            return call
        return {name: wrap(name, fn) for name, fn in self._tools.items()}


def run_case(agent: Callable[[str, Mapping[str, Tool]], AgentAnswer], case: EvalCase, tools: Mapping[str, Tool] | None = None,
             clock: Callable[[], float] = time.monotonic) -> CaseResult:
    """Run one case. The result never raises for a bad agent: a crash is a failed case."""
    recorder = _Recorder(tools or {}, case.allowed_tools)
    started = clock()
    failures: list[str] = []
    try:
        answer = agent(case.task, recorder.as_dict())
    except Exception as exc:                                    # noqa: BLE001  (an agent that crashes fails its case)
        return CaseResult(case.id, False, (f"the agent raised {type(exc).__name__}: {exc}",), seconds=clock() - started,
                          tool_calls=tuple(recorder.calls))
    seconds = clock() - started
    if answer.error:
        failures.append(f"the agent reported an error: {answer.error}")
    called = set(recorder.calls) | set(answer.tool_calls)
    for name in case.must_call:
        if name not in called:
            failures.append(f"did not call the tool {name}")
    for name in recorder.denied:
        failures.append(f"tried a tool that is not allowed: {name}")
    lowered = answer.text.lower()
    for needle in case.must_include:
        if needle.lower() not in lowered:
            failures.append(f"the answer does not say {needle!r}")
    for needle in case.must_not_include:
        if needle.lower() in lowered:
            failures.append(f"the answer says {needle!r}")
    if not answer.text.strip():
        failures.append("the answer is empty")
    sources = {"snapshot": case.snapshot, "tools": recorder.returned, "task": case.task}
    bad = numbers.unverified_numbers(answer.text, sources, ignore_below=case.ignore_numbers_below)
    unverified = tuple(c.raw for c in bad)
    if unverified:
        failures.append("numbers with no source: " + ", ".join(unverified))
    if case.max_seconds is not None and seconds > case.max_seconds:
        failures.append(f"took {seconds:.1f}s, the limit is {case.max_seconds}s")
    return CaseResult(case.id, not failures, tuple(failures), unverified, seconds, tuple(recorder.calls))


def run_suite(agent: Callable[[str, Mapping[str, Tool]], AgentAnswer], cases: list[EvalCase],
              tools: Mapping[str, Tool] | None = None) -> EvalReport:
    """Run every case in order and collect the results."""
    return EvalReport(tuple(run_case(agent, case, tools) for case in cases))

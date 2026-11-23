"""A fake `hokma.engines` adapter: scripted events, no process, no network."""
from __future__ import annotations

from typing import Any, AsyncIterator

from hokma.engines.base import EngineContext, EngineEvent


class FakeSession:
    def __init__(self, events: list[Any]) -> None:
        self.events, self.sent, self.answers, self.closed = events, [], [], False
        self.session_ref = None

    async def send(self, blocks: list[dict[str, Any]]) -> AsyncIterator[EngineEvent]:
        self.sent.append(blocks)
        for event in self.events:
            if isinstance(event, BaseException):
                raise event
            yield event

    async def answer_permission(self, request_id: str, allow: bool, reason: str, tool_input: Any = None) -> None:
        self.answers.append((request_id, allow, reason))

    async def interrupt(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True

    @property
    def alive(self) -> bool:
        return not self.closed


def text_turn(*parts: str, usage: dict | None = None) -> list[EngineEvent]:
    return [*({"type": "text", "text": p} for p in parts), {"type": "done", "status": "complete", "usage": usage or {}}]


def error_turn(code: str, message: str = "") -> list[EngineEvent]:
    return [{"type": "error", "code": code, "message": message or code}]


class FakeAdapter:
    """One script per opened session, in order; the last script repeats."""

    kind = "fake"

    def __init__(self, *scripts: list[Any]) -> None:
        self.scripts = list(scripts) or [text_turn("ok")]
        self.contexts: list[EngineContext] = []
        self.sessions: list[FakeSession] = []

    def caps(self) -> dict:
        return {"hooks": False, "os_sandbox": False, "native_subagents": False, "resume": False, "images": False, "mcp": False}

    def new_ref(self) -> str | None:
        return None

    async def open(self, ctx: EngineContext) -> FakeSession:
        self.contexts.append(ctx)
        script = self.scripts[min(len(self.sessions), len(self.scripts) - 1)]
        session = FakeSession(script)
        self.sessions.append(session)
        return session

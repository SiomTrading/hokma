"""The contract between the chat and an engine (Claude Code, Codex, a direct API...).

The chat and the scheduler speak only this. Each adapter turns its own protocol into `EngineEvent`s and answers
permission requests; Hokma owns permissions, protections, history and tools. The panel cards (`delta`, `tool_use`,
`tool_result`, `permission_request`, `done`, `error`) do not change shape: the panel does not know which engine runs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Literal, Protocol, TypedDict


class EngineCaps(TypedDict):
    hooks: bool              # the engine runs guard/hardblock itself (Claude Code only)
    os_sandbox: bool         # an operating-system sandbox is available on this machine
    native_subagents: bool   # it has its own tool to start agents
    resume: bool
    images: bool
    mcp: bool


class EngineEvent(TypedDict, total=False):
    type: Literal["session", "text", "message_break", "tool_use", "tool_result", "permission_request", "usage",
                  "task_start", "task_end", "done", "error",
                  "bg_notice", "continuation", "quiet", "init_timeout", "timeout", "closed"]
    ref: str                  # session
    text: str                 # text, tool_result
    id: str                   # tool_use, tool_result, task_start, task_end (the tool_use id the task belongs to)
    parent: str               # tool_use, tool_result: the tool_use id of the agent that did the step, "" on the main thread
    launched: bool            # tool_result: the agent was started in the background; this is the receipt, not its answer
    task_type: str            # task_start: "local_agent" | "local_bash"
    name: str                 # tool_use
    input: dict[str, Any]     # tool_use, permission_request
    is_error: bool            # tool_result
    request_id: str           # permission_request
    tool: str                 # permission_request
    usage: dict[str, Any]     # done: tokens_in, tokens_out, cache_read, cost_usd
    status: str               # done: "complete" | "stopped"; task_end: "completed" | "failed" | "killed" ...
    result: str               # done: the final text when the engine sends one
    code: str                 # error
    message: str              # error
    owed: int                 # bg_notice, continuation, init_timeout: turns the CLI announced and has not opened yet
    # bg_notice / continuation / quiet / init_timeout / timeout / closed: only `follow()` sends them (bug 1257). A background task of
    # the main thread ended, the CLI began a turn of its own, nothing more came for a while, the wait ran out, the
    # process closed.


@dataclass
class EngineContext:
    """Everything an adapter needs to open a session. `connection` is the saved way in (None = the default)."""
    conversation_id: str
    model: str                                  # the concrete model for this turn
    cwd: str
    agent: str | None = None
    session_ref: str | None = None
    resume: bool = False                        # the engine already has this session
    mode: str = "ask"                           # Hokma permission mode
    task_id: str = ""
    connection: dict[str, Any] | None = None
    tier: str = "balanced"
    instructions: str = ""                      # HOKMA.md + the agent's persona, for engines without `--agent`
    history: list[dict[str, str]] = field(default_factory=list)     # earlier turns, for a fresh session or API engine
    extra: dict[str, Any] = field(default_factory=dict)


class EngineSession(Protocol):
    session_ref: str | None

    def send(self, blocks: list[dict[str, Any]]) -> AsyncIterator[EngineEvent]: ...
    async def answer_permission(self, request_id: str, allow: bool, reason: str, tool_input: Any = None) -> None: ...
    async def interrupt(self) -> None: ...
    async def close(self) -> None: ...

    @property
    def alive(self) -> bool: ...


class EngineAdapter(Protocol):
    kind: str

    def caps(self) -> EngineCaps: ...
    def new_ref(self) -> str | None: ...
    async def open(self, ctx: EngineContext) -> EngineSession: ...


class EngineError(Exception):
    """Raised by an adapter before a turn can start (binary missing, integrity, bad connection)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def user_text(blocks: list[dict[str, Any]]) -> str:
    """The text of a message made of content blocks (images and files are ignored)."""
    return "\n".join(str(b.get("text", "")) for b in blocks if isinstance(b, dict) and b.get("type") == "text").strip()


def user_images(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The inline image blocks of a message (base64 source), in order."""
    return [b for b in blocks if isinstance(b, dict) and b.get("type") == "image"
            and isinstance(b.get("source"), dict) and b["source"].get("type") == "base64"]


Launch = Callable[[], Any]

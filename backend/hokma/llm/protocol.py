"""The contract of a model client and the client that runs on an engine (SPEC-017: AH-50).

`LLMClient.complete` takes chat messages and returns an `LLMReply`. `ChatLLM` is the one implementation: it opens a session of
a `hokma.engines` adapter, sends the last user message, collects the text, and closes the session. Tools are not offered and
any permission request is refused: a desk model call is a question, not a task.
"""
from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, TypeVar, runtime_checkable

from hokma.engines.base import EngineAdapter, EngineContext, EngineError
from hokma.llm.jsonx import LLMParseError, extract_json
from hokma.llm.retry import RetryPolicy, call_with_retry, coerce_max_tokens

T = TypeVar("T")
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT = 180.0
SCHEMA_PREFIX = "Reply with one JSON object and nothing else. It must match this JSON schema:\n"


class LLMError(Exception):
    """A model call failed. `code` is the stable code of the engine layer (`network`, `quota`, `auth`...)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code, self.message = code, message or code


@dataclass(frozen=True)
class LLMReply:
    text: str
    model: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    cached: bool = False
    attempts: int = 1
    parsed: dict | None = None            # the JSON object, when a schema was asked for and the answer had one

    def json(self) -> dict:
        """The answer as a JSON object (parsed when a schema was asked, else extracted now). LLMParseError if none."""
        return self.parsed if self.parsed is not None else extract_json(self.text)


@runtime_checkable
class LLMClient(Protocol):
    model: str

    def complete(self, messages: list[dict], *, schema: dict | None = None, max_tokens: int = DEFAULT_MAX_TOKENS) -> LLMReply: ...


def run_sync(factory: Callable[[], Awaitable[T]]) -> T:
    """Run a coroutine to the end from synchronous code, also when a loop is already running in this thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())                       # type: ignore[arg-type]
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = asyncio.run(factory())           # type: ignore[arg-type]
        except BaseException as exc:                        # noqa: BLE001  (handed to the caller below)
            box["error"] = exc

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


def split_messages(messages: list[dict]) -> tuple[str, list[dict[str, str]], str]:
    """(system text, earlier turns, last user text) of a chat message list. Raises ValueError when there is no user turn."""
    system: list[str] = []
    turns: list[dict[str, str]] = []
    for message in messages:
        role = str(message.get("role", "user"))
        content = message.get("content", "")
        if isinstance(content, list):
            from hokma.llm.jsonx import _flatten

            content = _flatten(content)
        text = str(content)
        if role == "system":
            system.append(text)
        elif role in ("user", "assistant"):
            turns.append({"role": role, "content": text})
    if not turns or turns[-1]["role"] != "user":
        raise ValueError("the last message must be a user message")
    return "\n\n".join(system), turns[:-1], turns[-1]["content"]


class ChatLLM:
    """A model reached through one `hokma.engines` adapter."""

    def __init__(self, adapter: EngineAdapter, context: EngineContext, *, model: str = "", provider: str = "",
                 policy: RetryPolicy | None = None, sleep: Callable[[float], None] | None = None,
                 timeout: float = DEFAULT_TIMEOUT, params: dict[str, Any] | None = None) -> None:
        self.adapter, self.context = adapter, context
        self.model = model or context.model
        self.provider = provider
        self.policy = policy or RetryPolicy()
        self.timeout = timeout
        self.params = dict(params or {})
        self._sleep = sleep
        self.last_attempts = 1

    # -- one attempt ---------------------------------------------------------------------------------------------
    async def _attempt(self, system: str, history: list[dict[str, str]], user: str, max_tokens: int) -> tuple[str, dict[str, Any]]:
        cap = self.params.get("max_tokens") or self.params.get("max_output_tokens")
        limit = min(max_tokens, int(cap)) if cap else max_tokens            # the setting is a ceiling, the call may ask for less
        ctx = EngineContext(**{**self.context.__dict__, "instructions": "\n\n".join(x for x in (self.context.instructions, system) if x),
                               "history": history, "extra": {**self.context.extra, **self.params, "max_tokens": limit}})
        session = await self.adapter.open(ctx)
        text, usage = "", {}

        async def read() -> None:
            nonlocal text, usage
            async for event in session.send([{"type": "text", "text": user}]):
                kind = event.get("type")
                if kind == "text":
                    text += str(event.get("text", ""))
                elif kind == "permission_request":
                    await session.answer_permission(str(event.get("request_id", "")), False, "a model call cannot use tools")
                elif kind == "error":
                    raise LLMError(str(event.get("code") or "engine_error"), str(event.get("message") or ""))
                elif kind == "done":
                    usage = dict(event.get("usage") or {})
                    if not text and event.get("result"):
                        text = str(event["result"])
                    return

        try:
            await asyncio.wait_for(read(), self.timeout)
        except asyncio.TimeoutError:
            raise LLMError("timeout", "no answer in time") from None
        except EngineError as exc:
            raise LLMError(exc.code, exc.message) from None
        finally:
            await session.close()
        return text, usage

    @staticmethod
    def _code_of(error: BaseException) -> str | None:
        return error.code if isinstance(error, LLMError) else None

    # -- the protocol --------------------------------------------------------------------------------------------
    def complete(self, messages: list[dict], *, schema: dict | None = None, max_tokens: int = DEFAULT_MAX_TOKENS) -> LLMReply:
        max_tokens = coerce_max_tokens(max_tokens)
        system, history, user = split_messages(messages)
        if schema is not None:
            system = (system + "\n\n" if system else "") + SCHEMA_PREFIX + json.dumps(schema, sort_keys=True)
        attempts = 0

        def once() -> tuple[str, dict[str, Any]]:
            nonlocal attempts
            attempts += 1
            try:
                return run_sync(lambda: self._attempt(system, history, user, max_tokens))
            except EngineError as exc:
                raise LLMError(exc.code, exc.message) from None

        kwargs: dict[str, Any] = {"policy": self.policy, "code_of": self._code_of}
        if self._sleep is not None:
            kwargs["sleep"] = self._sleep
        text, usage = call_with_retry(once, **kwargs)
        self.last_attempts = attempts
        parsed = None
        if schema is not None:
            try:
                parsed = extract_json(text)
            except LLMParseError:
                parsed = None
        return LLMReply(text=text, model=self.model, usage=usage, attempts=attempts, parsed=parsed)

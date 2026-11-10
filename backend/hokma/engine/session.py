"""One conversation = one ChatSession = at most one live `claude` process."""
import asyncio
import os
import time
import uuid
from typing import Any, Awaitable, Callable

from hokma.engine import stream
from hokma.engine.protocols import ConversationRecord, ProjectRecord
from hokma.engines.base import EngineContext, EngineError, EngineSession
from hokma.engines.claude_code import SECRET_ENV  # noqa: F401  (kept for importers)
from hokma.engines.registry import tier_of
from hokma.permissions import modes

Emit = Callable[[dict[str, Any]], Awaitable[None]]

MAX_STORED_TOOLS = 40              # tool lines kept with a saved answer
MAX_STORED_RESULT = 600            # characters of each tool result kept with a saved answer
PERMISSION_TIMEOUT = 600.0         # seconds without an answer = deny
STOP_GRACE = 3.0                   # seconds the CLI gets to honour an interrupt before the tree is killed


class TurnError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


class Pending:
    def __init__(self, request_id: str, tool_use_id: str, tool: str, tool_input: Any, summary: dict[str, str],
                 expires_at: float) -> None:
        self.request_id, self.tool_use_id, self.tool = request_id, tool_use_id, tool
        self.input, self.summary, self.expires_at = tool_input, summary, expires_at
        self.future: asyncio.Future[tuple[str, str]] = asyncio.get_running_loop().create_future()

    def event(self) -> dict[str, Any]:
        return {"type": "permission_request", "request_id": self.request_id, "tool_use_id": self.tool_use_id,
                "tool": self.tool, "input": self.input, "summary": self.summary, "expires_at": self.expires_at}


class ChatSession:
    def __init__(self, engine: "Any", conversation_id: str) -> None:
        self.engine = engine
        self.conversation_id = conversation_id
        self.state = "idle"
        self.pending: dict[str, Pending] = {}
        self.last_used = time.monotonic()
        self._eng: EngineSession | None = None
        self._eng_key: tuple | None = None
        self._session_id: str | None = None
        self._session_started = False
        self._task: asyncio.Task | None = None
        self._turn_done = asyncio.Event()
        self._stop_requested = False
        self._waiters: set[asyncio.Task] = set()
        self.live: dict[str, Any] | None = None     # the turn in progress: its id, text so far and tool lines

    def snapshot(self) -> dict[str, Any] | None:
        """What a socket that reconnects in the middle of a turn needs to rebuild the answer so far."""
        if self.live is None:
            return None
        return {"turn_id": self.live["turn_id"], "text": "".join(self.live["parts"]),
                "tools": [dict(t) for t in self.live["tools"]]}

    # ---------------------------------------------------------------- public

    @property
    def has_process(self) -> bool:
        return self._eng is not None and self._eng.alive

    async def emit(self, event: dict[str, Any]) -> None:
        await self.engine.broadcast(self.conversation_id, {"conversation_id": self.conversation_id, **event})

    def start_turn(self, conv: ConversationRecord, project: ProjectRecord, blocks: list[dict[str, Any]],
                   user_text: str, meta: dict[str, Any]) -> None:
        self.state = "running"
        self._stop_requested = False
        self._turn_done.clear()
        self.last_used = time.monotonic()
        self._task = asyncio.create_task(self._turn(conv, project, blocks, user_text, meta))

    async def stop(self) -> None:
        """Interrupt the running turn; if the CLI does not finish within STOP_GRACE seconds, kill the tree."""
        if self.state != "running":
            return
        self._stop_requested = True
        for pending in list(self.pending.values()):
            await self.resolve(pending.request_id, "deny", "stop")
        eng = self._eng
        if eng is not None and eng.alive:
            await eng.interrupt()
        try:
            await asyncio.wait_for(self._turn_done.wait(), self.engine.stop_grace)
        except asyncio.TimeoutError:
            if eng is not None:
                await eng.close()
            await self._turn_done.wait()

    async def resolve(self, request_id: str, decision: str, by: str) -> bool:
        pending = self.pending.get(request_id)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result((decision, by))
        return True

    async def close(self) -> None:
        if self._task and not self._task.done():
            await self.stop()
        await self._drop_process()

    # --------------------------------------------------------------- engine

    async def _drop_process(self) -> None:
        eng, self._eng, self._eng_key = self._eng, None, None
        if eng is not None:
            await eng.close()

    async def _ensure_engine(self, conv: ConversationRecord, project: ProjectRecord,
                             mode: str = modes.DEFAULT_MODE) -> EngineSession:
        adapter, connection, model = self.engine.resolve_engine(conv)
        if conv.engine_session and not self._session_id:
            self._session_id, self._session_started = conv.engine_session, True
        if not self._session_id:
            self._session_id = adapter.new_ref()
        # The permission mode is part of the key: when the owner changes it, the process of this conversation is
        # replaced before the next turn and resumed with the other mode.
        key = (adapter.kind, connection and connection.get("id"), model, conv.agent, project.path, self._session_id,
               mode)
        if self._eng is not None and self._eng.alive and self._eng_key == key:
            return self._eng
        await self._drop_process()
        self.engine.make_room(self)
        ctx = EngineContext(conversation_id=self.conversation_id, model=model, cwd=project.path, agent=conv.agent,
                            session_ref=self._session_id, resume=self._session_started, mode=mode,
                            connection=connection, history=self.engine.history_for(conv),
                            instructions=self.engine.instructions_for(project.path, conv.agent),
                            tier=tier_of(conv.model))
        try:
            self._eng = await adapter.open(ctx)
        except EngineError as exc:
            raise TurnError(exc.code, exc.message) from exc
        self._session_id = self._eng.session_ref or self._session_id
        self._eng_key = key
        return self._eng

    # ------------------------------------------------------------------ turn

    async def _turn(self, conv: ConversationRecord, project: ProjectRecord, blocks: list[dict[str, Any]],
                    user_text: str, meta: dict[str, Any]) -> None:
        store = self.engine.store
        turn_id = uuid.uuid4().hex[:12]
        user_msg = store.add_message(conv.id, "user", user_text, meta=meta)
        if not store.get_conversation(conv.id).title:
            title = self.engine.make_title(user_text)
            store.update_conversation(conv.id, title=title)
            # the same body as everywhere else: a general conversation says `project: null`, not the working folder
            await self.emit({"type": "conversation", **self.engine._conv_body(conv, False), "title": title})
        await self.emit({"type": "turn_start", "turn_id": turn_id, "message_id": user_msg.id})

        parts: list[str] = []
        tools: list[dict[str, Any]] = []
        usage: dict[str, Any] = {"tokens_in": 0, "tokens_out": 0, "cache_read": 0, "cost_usd": 0.0}
        status, error, result_seen = "complete", None, False
        eng: EngineSession | None = None
        guard_blocks: list[dict[str, Any]] = []
        self.live = {"turn_id": turn_id, "parts": parts, "tools": tools}
        try:
            mode = self.engine.permission_mode()
            if modes.snapshots_wanted(mode):
                await self.engine.take_checkpoint(project.path, self.conversation_id)
            eng = await self._ensure_engine(conv, project, mode)
            async for ev in eng.send(blocks):
                kind = ev["type"]
                if kind == "session":
                    self._session_started = True
                    if conv.engine_session != self._session_id:
                        conv.engine_session = self._session_id
                        store.update_conversation(conv.id, engine_session=self._session_id)
                elif kind == "message_break":
                    if parts and not parts[-1].endswith("\n\n"):
                        parts.append("\n\n")
                        await self.emit({"type": "delta", "turn_id": turn_id, "text": "\n\n"})
                elif kind == "text":
                    parts.append(ev["text"])
                    await self.emit({"type": "delta", "turn_id": turn_id, "text": ev["text"]})
                elif kind == "tool_use":
                    summary = stream.summarize_tool(ev.get("name", ""), ev.get("input"))
                    tools.append({"id": ev.get("id", ""), "name": ev.get("name"), **summary,
                                  "at": sum(len(p) for p in parts), "is_error": None, "result": None})
                    await self.emit({"type": "tool_use", "turn_id": turn_id, "tool_use_id": ev.get("id", ""),
                                     "name": ev.get("name", ""), "input": ev.get("input") or {},
                                     "summary": summary})
                elif kind == "tool_result":
                    await self._publish_blocks(turn_id, guard_blocks)
                    for line in tools:
                        if line["id"] == ev.get("id"):
                            line["is_error"] = bool(ev.get("is_error"))
                            line["result"] = str(ev.get("text") or "")[:MAX_STORED_RESULT]
                    await self.emit({"type": "tool_result", "turn_id": turn_id, "tool_use_id": ev.get("id", ""),
                                     "is_error": bool(ev.get("is_error")),
                                     "text": str(ev.get("text") or "")[:stream.MAX_RESULT_TEXT]})
                elif kind == "permission_request":
                    if (ev.get("input") or {}).get("cancelled"):
                        await self.resolve(str(ev.get("request_id", "")), "deny", "engine")
                    else:
                        await self._on_permission_request(eng, turn_id, ev)
                elif kind == "done":
                    result_seen = True
                    if ev.get("status") == "stopped" or self._stop_requested:
                        status = "stopped"
                    else:
                        usage = ev.get("usage") or usage
                        if not parts and ev.get("result"):
                            parts.append(str(ev["result"]))
                            await self.emit({"type": "delta", "turn_id": turn_id, "text": parts[0]})
                    break
                elif kind == "error":
                    result_seen = True
                    if self._stop_requested:
                        status = "stopped"
                    else:
                        status, error = "error", TurnError(ev.get("code", "engine_error"), ev.get("message", ""))
                    break
            if not result_seen:                                # the engine ended without a verdict
                status = "stopped" if self._stop_requested else "error"
                if status == "error":
                    error = TurnError("engine_exited", "the engine ended the turn without an answer")
        except TurnError as exc:
            status, error = "error", exc
        except (OSError, asyncio.CancelledError) as exc:
            status = "stopped" if self._stop_requested else "error"
            if status == "error":
                error = TurnError("engine_exited", f"{type(exc).__name__}: {exc}".strip())
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            await self._publish_blocks(turn_id, guard_blocks)
            await self._finish_turn(conv, turn_id, parts, tools, usage, status, error, guard_blocks)

    async def _publish_blocks(self, turn_id: str, blocks: list[dict[str, Any]]) -> None:
        """Refusals of the always-on protections written by the hook since the last look: stored in `guard_events`
        by the engine and shown in the chat as `guard_block` cards. The ones of this conversation also travel with
        the saved answer, so the card is still there when the conversation is opened again."""
        for event in await self.engine.drain_blocks():
            cid = event.get("conversation_id")
            target = cid if cid else self.conversation_id
            payload = {k: v for k, v in event.items() if k != "conversation_id"}
            if target == self.conversation_id:
                blocks.append({k: v for k, v in payload.items() if k != "type"})
                await self.emit({**payload, "turn_id": turn_id})
            else:
                await self.engine.broadcast(target, {"conversation_id": target, **payload})

    async def _finish_turn(self, conv: ConversationRecord, turn_id: str, parts: list[str], tools: list[dict],
                           usage: dict[str, Any], status: str, error: TurnError | None,
                           blocks: list[dict[str, Any]] | None = None) -> None:
        for pending in list(self.pending.values()):
            await self.resolve(pending.request_id, "deny", "engine")
        await asyncio.gather(*self._waiters, return_exceptions=True)
        text = "".join(parts)
        message_id = None
        kept = tools[:MAX_STORED_TOOLS]
        if text or status == "complete" or kept:
            meta = {**({"tools": kept} if kept else {}), **({"blocks": blocks[:MAX_STORED_TOOLS]} if blocks else {})}
            rec = self.engine.store.add_message(conv.id, "assistant", text, usage["tokens_in"], usage["tokens_out"],
                                                status, meta or None)
            message_id = rec.id
        self.live = None                        # in the same step as the save: a socket that opens now sees the message
        if status != "complete":
            await self._drop_process()          # a stopped or failed process is replaced and resumed on the next turn
        self.state = "idle"
        self.last_used = time.monotonic()
        try:
            if error is not None:
                await self.emit({"type": "error", "turn_id": turn_id, "code": error.code, "message": error.message})
            else:
                zero = {"tokens_in": 0, "tokens_out": 0, "cache_read": 0, "cost_usd": 0.0}
                await self.emit({"type": "done", "turn_id": turn_id, "message_id": message_id,
                                 "stopped": status == "stopped", "usage": usage if status == "complete" else zero,
                                 "engine_session": self._session_id if self._session_started else None})
        finally:
            self._turn_done.set()

    # ----------------------------------------------------------- permissions

    async def _on_permission_request(self, eng: EngineSession, turn_id: str, ev: dict[str, Any]) -> None:
        request_id = str(ev.get("request_id", ""))
        tool = str(ev.get("tool", ""))
        pending = Pending(request_id, str(ev.get("id", "")), tool, ev.get("input") or {},
                          stream.summarize_tool(tool, ev.get("input")),
                          time.time() + self.engine.permission_timeout)
        self.pending[request_id] = pending
        await self.emit({"turn_id": turn_id, **pending.event()})
        waiter = asyncio.create_task(self._await_decision(eng, pending))
        self._waiters.add(waiter)
        waiter.add_done_callback(self._waiters.discard)

    async def _await_decision(self, eng: EngineSession, pending: Pending) -> None:
        try:
            decision, by = await asyncio.wait_for(asyncio.shield(pending.future), self.engine.permission_timeout)
        except asyncio.TimeoutError:
            decision, by = "deny", "timeout"
            if not pending.future.done():
                pending.future.set_result((decision, by))
        self.pending.pop(pending.request_id, None)
        allow = decision == "allow"
        reason = {"human": "The owner rejected this action.", "timeout": "No answer from the owner in time.",
                  "stop": "The owner stopped the turn."}.get(by, "The action was cancelled.")
        try:
            await eng.answer_permission(pending.request_id, allow, reason, pending.input)  # type: ignore[call-arg]
        except OSError:
            pass
        human = by == "human"
        self.engine.store.record_permission(
            self.conversation_id, pending.tool, pending.summary,
            ("allowed_by_human" if allow else "denied_by_human") if human else f"denied_by_{by}",
            "human" if human else "system", "dashboard")
        await self.emit({"type": "permission_resolved", "request_id": pending.request_id, "decision": decision,
                         "by": by})


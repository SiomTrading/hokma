"""One conversation = one ChatSession = at most one live `claude` process."""
import asyncio
import os
import time
import uuid
from typing import Any, Awaitable, Callable

from hokma.engine import stream
from hokma.engine.process import ClaudeProcess, find_claude
from hokma.engine.protocols import ConversationRecord, ProjectRecord

Emit = Callable[[dict[str, Any]], Awaitable[None]]

MAX_STORED_TOOLS = 40              # tool lines kept with a saved answer
MAX_STORED_RESULT = 600            # characters of each tool result kept with a saved answer
PERMISSION_TIMEOUT = 600.0         # seconds without an answer = deny
STOP_GRACE = 3.0                   # seconds the CLI gets to honour an interrupt before the tree is killed
BASE_FLAGS = ["-p", "--output-format", "stream-json", "--input-format", "stream-json",
              "--include-partial-messages", "--verbose", "--permission-prompt-tool", "stdio",
              "--permission-mode", "default"]
SECRET_ENV = ("HOKMA_SESSION_TOKEN", "HOKMA_TOKEN")


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
        self._proc: ClaudeProcess | None = None
        self._proc_key: tuple | None = None
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
        return self._proc is not None and self._proc.alive

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
        proc = self._proc
        if proc is not None and proc.alive:
            try:
                await proc.write(stream.interrupt_request(f"stop-{uuid.uuid4().hex[:8]}"))
            except OSError:
                pass
        try:
            await asyncio.wait_for(self._turn_done.wait(), self.engine.stop_grace)
        except asyncio.TimeoutError:
            if proc is not None:
                await proc.terminate()
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

    # --------------------------------------------------------------- process

    async def _drop_process(self) -> None:
        proc, self._proc, self._proc_key = self._proc, None, None
        if proc is not None:
            await proc.terminate()

    async def _ensure_process(self, conv: ConversationRecord, project: ProjectRecord) -> ClaudeProcess:
        if conv.engine_session and not self._session_id:
            self._session_id, self._session_started = conv.engine_session, True
        if not self._session_id:
            self._session_id = str(uuid.uuid4())
        key = (conv.model, conv.agent, project.path, self._session_id)
        if self._proc is not None and self._proc.alive and self._proc_key == key:
            return self._proc
        await self._drop_process()
        self.engine.make_room(self)
        try:
            spec = self.engine.launch()
        except Exception as exc:                         # IntegrityError and any failure while preparing settings
            raise TurnError("integrity", f"{type(exc).__name__}: {exc}") from exc
        env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV and v != self.engine.session_token}
        env.update(spec.env)
        claude = self.engine.claude_bin or find_claude(env)
        if not claude:
            raise TurnError("claude_not_found", "the `claude` command is not installed or not on PATH")
        argv = [claude, *spec.args, *BASE_FLAGS, "--model", conv.model]
        if conv.agent:
            argv += ["--agent", conv.agent]
        argv += ["--resume", self._session_id] if self._session_started else ["--session-id", self._session_id]
        try:
            self._proc = await ClaudeProcess.spawn(argv, env, project.path)
        except OSError as exc:
            raise TurnError("claude_not_found", str(exc)) from exc
        self._proc_key = key
        return self._proc

    # ------------------------------------------------------------------ turn

    async def _turn(self, conv: ConversationRecord, project: ProjectRecord, blocks: list[dict[str, Any]],
                    user_text: str, meta: dict[str, Any]) -> None:
        store = self.engine.store
        turn_id = uuid.uuid4().hex[:12]
        user_msg = store.add_message(conv.id, "user", user_text, meta=meta)
        if not store.get_conversation(conv.id).title:
            title = self.engine.make_title(user_text)
            store.update_conversation(conv.id, title=title)
            await self.emit({"type": "conversation", "title": title, "model": conv.model, "agent": conv.agent,
                             "project": {"id": project.id, "name": project.name}, "created": False})
        await self.emit({"type": "turn_start", "turn_id": turn_id, "message_id": user_msg.id})

        parts: list[str] = []
        tools: list[dict[str, Any]] = []
        usage: dict[str, Any] = {"tokens_in": 0, "tokens_out": 0, "cache_read": 0, "cost_usd": 0.0}
        status, error, result_seen = "complete", None, False
        proc: ClaudeProcess | None = None
        self.live = {"turn_id": turn_id, "parts": parts, "tools": tools}
        try:
            proc = await self._ensure_process(conv, project)
            await proc.write(stream.user_message(blocks))
            while True:
                msg = await proc.read()
                if msg is None:
                    break
                kind = msg["type"]
                if kind == "system" and msg.get("subtype") == "init":
                    self._session_started = True
                    if conv.engine_session != self._session_id:
                        conv.engine_session = self._session_id
                        store.update_conversation(conv.id, engine_session=self._session_id)
                elif kind == "stream_event":
                    if stream.is_message_start(msg) and parts and not parts[-1].endswith("\n\n"):
                        parts.append("\n\n")
                        await self.emit({"type": "delta", "turn_id": turn_id, "text": "\n\n"})
                    text = stream.text_delta(msg)
                    if text:
                        parts.append(text)
                        await self.emit({"type": "delta", "turn_id": turn_id, "text": text})
                elif kind == "assistant":
                    for block in stream.tool_uses(msg):
                        summary = stream.summarize_tool(block.get("name", ""), block.get("input"))
                        tools.append({"id": block.get("id", ""), "name": block.get("name"), **summary,
                                      "at": sum(len(p) for p in parts), "is_error": None, "result": None})
                        await self.emit({"type": "tool_use", "turn_id": turn_id, "tool_use_id": block.get("id", ""),
                                         "name": block.get("name", ""), "input": block.get("input") or {},
                                         "summary": summary})
                elif kind == "user":
                    for res in stream.tool_results(msg):
                        for line in tools:
                            if line["id"] == res.get("tool_use_id"):
                                line["is_error"] = bool(res.get("is_error"))
                                line["result"] = str(res.get("text") or "")[:MAX_STORED_RESULT]
                        await self.emit({"type": "tool_result", "turn_id": turn_id, **res})
                elif kind == "control_request":
                    await self._on_control_request(proc, turn_id, msg)
                elif kind == "control_cancel_request":
                    await self.resolve(str(msg.get("request_id", "")), "deny", "engine")
                elif kind == "result":
                    result_seen = True
                    if stream.is_aborted(msg) or self._stop_requested:
                        status = "stopped"
                    elif msg.get("is_error"):
                        status, error = "error", TurnError("engine_error", str(msg.get("result") or msg.get("subtype")))
                    else:
                        usage = stream.usage_of(msg)
                        if not parts and msg.get("result"):
                            parts.append(str(msg["result"]))
                            await self.emit({"type": "delta", "turn_id": turn_id, "text": parts[0]})
                    break
            if not result_seen:                                # the process ended without a result
                if self._stop_requested:
                    status = "stopped"
                else:
                    status = "error"
                    await proc.terminate()
                    error = TurnError("engine_exited", f"claude exited with code {proc.returncode}: "
                                                       f"{proc.stderr_tail()}".strip())
        except TurnError as exc:
            status, error = "error", exc
        except (OSError, asyncio.CancelledError) as exc:
            status = "stopped" if self._stop_requested else "error"
            if status == "error":
                tail = proc.stderr_tail() if proc else ""
                error = TurnError("engine_exited", f"{type(exc).__name__}: {exc} {tail}".strip())
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            await self._finish_turn(conv, turn_id, parts, tools, usage, status, error)

    async def _finish_turn(self, conv: ConversationRecord, turn_id: str, parts: list[str], tools: list[dict],
                           usage: dict[str, Any], status: str, error: TurnError | None) -> None:
        for pending in list(self.pending.values()):
            await self.resolve(pending.request_id, "deny", "engine")
        await asyncio.gather(*self._waiters, return_exceptions=True)
        text = "".join(parts)
        message_id = None
        kept = tools[:MAX_STORED_TOOLS]
        if text or status == "complete" or kept:
            rec = self.engine.store.add_message(conv.id, "assistant", text, usage["tokens_in"], usage["tokens_out"],
                                                status, {"tools": kept} if kept else None)
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

    async def _on_control_request(self, proc: ClaudeProcess, turn_id: str, msg: dict[str, Any]) -> None:
        request_id = str(msg.get("request_id", ""))
        request = msg.get("request") or {}
        if request.get("subtype") != "can_use_tool":
            await proc.write(stream.control_error(request_id, "unsupported request"))
            return
        tool = str(request.get("tool_name", ""))
        pending = Pending(request_id, str(request.get("tool_use_id", "")), tool, request.get("input") or {},
                          stream.summarize_tool(tool, request.get("input")),
                          time.time() + self.engine.permission_timeout)
        self.pending[request_id] = pending
        await self.emit({"turn_id": turn_id, **pending.event()})
        waiter = asyncio.create_task(self._await_decision(proc, pending))
        self._waiters.add(waiter)
        waiter.add_done_callback(self._waiters.discard)

    async def _await_decision(self, proc: ClaudeProcess, pending: Pending) -> None:
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
            await proc.write(stream.permission_reply(pending.request_id, allow, pending.input, reason))
        except OSError:
            pass
        human = by == "human"
        self.engine.store.record_permission(
            self.conversation_id, pending.tool, pending.summary,
            ("allowed_by_human" if allow else "denied_by_human") if human else f"denied_by_{by}",
            "human" if human else "system", "dashboard")
        await self.emit({"type": "permission_resolved", "request_id": pending.request_id, "decision": decision,
                         "by": by})


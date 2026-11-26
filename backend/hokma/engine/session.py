"""One conversation = one ChatSession = at most one live `claude` process."""
import asyncio
import os
import time
import uuid
from typing import Any, Awaitable, Callable

from hokma.engine import errlog, stream
from hokma.engine.agents import AgentTracker
from hokma.engine.protocols import ConversationRecord, ProjectRecord
from hokma.engines.base import EngineContext, EngineError, EngineSession
from hokma.engines.claude_code import SECRET_ENV  # noqa: F401  (kept for importers)
from hokma.engines.registry import tier_of
from hokma.permissions import modes

Emit = Callable[[dict[str, Any]], Awaitable[None]]

MAX_STORED_TOOLS = 40              # tool lines kept with a saved answer
MAX_STORED_RESULT = 600            # characters of each tool result kept with a saved answer
MAX_LIVE_OUTPUT = 4000             # characters of a running command's live output kept for a socket that reconnects
PERMISSION_TIMEOUT = 600.0         # seconds without an answer = deny
STOP_GRACE = 3.0                   # seconds the CLI gets to honour an interrupt before the tree is killed
STEP_LIMIT = 10.0                  # seconds any single step of a stop may take (interrupt, kill, wrap-up)
BG_WAIT = 3600.0                   # seconds an agent started in the background may work before the chat gives up on it (bug 1257)
INIT_WAIT = 60.0                   # seconds the chat waits for the CLI to open the turn it announced; then it stops waiting for that turn, never kills (bug 1257)
GROUP_WINDOW = 4.0                 # seconds after an ending or an automatic turn in which more endings are grouped into one message


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
        self.channel = "dashboard"          # where the owner answered from (set by `resolve`)

    def event(self) -> dict[str, Any]:
        return {"type": "permission_request", "request_id": self.request_id, "tool_use_id": self.tool_use_id,
                "tool": self.tool, "input": self.input, "summary": self.summary, "expires_at": self.expires_at}


class _TurnCtx:
    """What one turn collects while it runs: the answer, the tool lines, the agents and how it ended. A turn is the
    owner's message or, since bug 1257, the one the CLI opens by itself when an agent started in the background ends
    (`origin == "agents"`: no user message, one saved answer for all the turns that end together)."""

    def __init__(self, conv: ConversationRecord, turn_id: str, agents: AgentTracker, origin: str = "") -> None:
        self.conv, self.turn_id, self.agents, self.origin = conv, turn_id, agents, origin
        self.parts: list[str] = []
        self.tools: list[dict[str, Any]] = []
        self.usage: dict[str, Any] = {"tokens_in": 0, "tokens_out": 0, "cache_read": 0, "cost_usd": 0.0}
        self.status: str = "complete"
        self.error: TurnError | None = None
        self.result_seen = False
        self.guard_blocks: list[dict[str, Any]] = []
        self.announced = not origin     # an automatic turn is shown to the panel only when it has something to show


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
        self._watcher: asyncio.Task | None = None   # reads the engine between turns while agents work in the background
        self._bg: list[AgentTracker] = []           # agents of finished turns that still work
        self._cont: _TurnCtx | None = None          # the turn the CLI opened by itself, while it runs
        self._owed = 0                              # turns the CLI announced (a notice) and has not opened yet; the chat is free meanwhile
        self._queued: tuple | None = None           # the owner's message written while a turn is owed: sent when it closes or the wait ends (already saved: its last item is the id of the saved message)

    def snapshot(self) -> dict[str, Any] | None:
        """What a socket that reconnects in the middle of a turn needs to rebuild the answer so far."""
        if self.live is None:
            return None
        return {"turn_id": self.live["turn_id"], "text": "".join(self.live["parts"]),
                "tools": [dict(t) for t in self.live["tools"]], **self.live["agents"].snapshot()}

    # ---------------------------------------------------------------- public

    @property
    def has_process(self) -> bool:
        return self._eng is not None and self._eng.alive

    async def emit(self, event: dict[str, Any]) -> None:
        await self.engine.broadcast(self.conversation_id, {"conversation_id": self.conversation_id, **event})

    def start_turn(self, conv: ConversationRecord, project: ProjectRecord, blocks: list[dict[str, Any]],
                   user_text: str, meta: dict[str, Any], message_id: str | None = None) -> bool:
        """Starts the owner's turn. False: the conversation is busy (a turn, or the one the CLI opened by itself when an
        agent ended, took it while the caller was awaiting) and nothing was started: the pipe is not touched (bug 1257).
        While the CLI owes a turn it announced (the notice came, its init did not) the chat is free but the pipe is not:
        the message is kept (one only) and written when that turn closes or the short wait for its init runs out, so the
        owner's text and the summary never mix. A second message in that window is refused as busy.

        The queued message is saved and announced at once (a `queued` event), so the panel shows it with its "waiting
        for the lead" state instead of leaving up to `INIT_WAIT` seconds without an echo; its turn then reuses the saved
        message (`message_id`) and never saves it twice (bug 1257)."""
        if self.state == "running" or self._cont is not None:
            return False
        if self.watching and self._owed > 0 and self._watcher is not asyncio.current_task():
            if self._queued is not None:
                return False
            saved = self.engine.store.add_message(conv.id, "user", user_text, meta=meta)
            self._queued = (conv, project, blocks, user_text, meta, saved.id)
            self.last_used = time.monotonic()
            self.engine.background(self.emit({"type": "queued", "message_id": saved.id, "text": user_text}))
            return True
        self._owed = 0
        self.state = "running"
        self._stop_requested = False
        self._turn_done.clear()
        self.last_used = time.monotonic()
        if self._watcher is not None and not self._watcher.done() and self._watcher is not asyncio.current_task():
            self._watcher.cancel()              # the owner's turn reads the pipe from now on; `_turn` waits for this to end
        self._task = asyncio.create_task(self._turn(conv, project, blocks, user_text, meta, message_id))
        return True

    async def stop(self) -> None:
        """Stop the running turn. Never waits forever and always frees the conversation (task 3175).

        1. interrupt the engine and give it `stop_grace` seconds to end the turn by itself;
        2. kill the engine's whole process tree (and the commands it started) and wait for the turn to end;
        3. if the turn is still stuck (a pipe nobody closes, a call that never returns), cancel it;
        4. whatever happened, `finally` leaves the conversation idle, without a process and with its turn closed, so
           this chat, the other chats and deleting conversations keep working."""
        if self.state != "running" and not self.watching:
            return
        if self._cont is not None or self.state != "running":
            return await self._kill_background()     # no turn of the owner runs: the agents are all that is left
        self._stop_requested = True
        eng = self._eng
        try:
            for pending in list(self.pending.values()):
                await self.resolve(pending.request_id, "deny", "stop")
            if eng is not None and eng.alive:
                await self._bounded(eng.interrupt(), "interrupt")
            if await self._turn_ended(self.engine.stop_grace):
                return
            if eng is not None:
                await self._bounded(eng.close(), "kill")
            if await self._turn_ended(self.step_limit):
                return
            task = self._task
            if task is not None and not task.done():
                errlog.log(f"conversation {self.conversation_id}: the turn did not end after the kill; cancelled")
                task.cancel()
            await self._turn_ended(self.step_limit)
        except Exception as exc:                          # noqa: BLE001
            errlog.log(f"conversation {self.conversation_id}: stop failed", exc)
        finally:
            if not self._turn_done.is_set():
                await self._release_stuck_turn(eng)

    async def _kill_background(self) -> None:
        """Stop with only the background agents alive (or the turn the CLI opened by itself running): everything
        stops, the process is killed with its whole tree, and the agents close as stopped (bug 1257)."""
        self._stop_requested = True
        try:
            for pending in list(self.pending.values()):
                await self.resolve(pending.request_id, "deny", "stop")
            await self._cancel_watcher()
            cont = self._cont
            if cont is not None:
                await self._finish_turn(cont, "stopped", None)     # saves what it said, kills the process, says `done`
            else:
                await self._bounded(self._drop_process(), "kill")
        except Exception as exc:                          # noqa: BLE001
            errlog.log(f"conversation {self.conversation_id}: stop failed", exc)
        finally:
            self._owed = 0
            await self._start_queued()                    # a message written during the wait is not lost: it runs on a fresh process

    @property
    def step_limit(self) -> float:
        return float(getattr(self.engine, "step_limit", STEP_LIMIT))

    async def _turn_ended(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(asyncio.shield(self._turn_done.wait()), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _bounded(self, work: Awaitable[Any], what: str) -> None:
        """One step of a stop, at most `step_limit` seconds; a failure is logged, never raised."""
        try:
            await asyncio.wait_for(work, self.step_limit)
        except asyncio.TimeoutError:
            errlog.log(f"conversation {self.conversation_id}: {what} took over {self.step_limit:.0f} s")
        except Exception as exc:                          # noqa: BLE001
            errlog.log(f"conversation {self.conversation_id}: {what} failed", exc)

    async def _release_stuck_turn(self, eng: EngineSession | None) -> None:
        """The last resort of a stop: the turn did not wrap up on its own, so the conversation is freed here. The
        process is dropped (its kill goes on in the background) and the panel is told the turn stopped."""
        errlog.log(f"conversation {self.conversation_id}: the turn was released by force after a stop")
        live, self.live = self.live, None
        for pending in list(self.pending.values()):
            if not pending.future.done():
                pending.future.set_result(("deny", "stop"))
        self.pending.clear()
        if self._eng is eng:
            self._eng, self._eng_key = None, None
        if eng is not None:
            asyncio.get_running_loop().create_task(self._bounded(eng.close(), "kill"))
        self.state = "idle"
        self.last_used = time.monotonic()
        self._turn_done.set()
        try:
            await asyncio.wait_for(self.emit({"type": "done", "turn_id": (live or {}).get("turn_id", ""),
                                              "message_id": None, "stopped": True,
                                              "usage": {"tokens_in": 0, "tokens_out": 0, "cache_read": 0,
                                                        "cost_usd": 0.0},
                                              "engine_session": None}), self.step_limit)
        except Exception:                                 # noqa: BLE001
            pass

    async def resolve(self, request_id: str, decision: str, by: str, channel: str = "dashboard") -> bool:
        pending = self.pending.get(request_id)
        if pending is None or pending.future.done():
            return False
        pending.channel = channel
        pending.future.set_result((decision, by))
        return True

    async def close(self) -> None:
        await self._drop_queued()
        if self._task and not self._task.done():
            await self.stop()
        await self._bounded(self._drop_process(), "close")

    async def _drop_queued(self) -> None:
        """The session closes (Hokma shuts down, the conversation is deleted) with the owner's message still waiting:
        it is never lost in silence. It stays saved, marked as not sent, and the chat says so (bug 1257)."""
        queued, self._queued = self._queued, None
        if queued is None:
            return
        await self._mark_unsent(queued[5], notice=True)

    async def _mark_unsent(self, message_id: str, notice: bool) -> None:
        from hokma.i18n import t

        try:
            self.engine.store.update_message_meta(self.conversation_id, message_id, unsent=True)
        except Exception as exc:                          # noqa: BLE001  the conversation may be gone: still tell the panel
            errlog.log(f"conversation {self.conversation_id}: the unsent message was not marked", exc)
        try:
            await self.emit({"type": "queued_dropped", "message_id": message_id})
            if notice:
                await self.emit({"type": "engine_notice", "message": t("chat.notice.unsent")})
        except Exception as exc:                          # noqa: BLE001  never in the way of the shutdown
            errlog.log(f"conversation {self.conversation_id}: the unsent notice was not sent", exc)

    # --------------------------------------------------------------- engine

    async def _drop_process(self, from_watcher: bool = False) -> None:
        if not from_watcher:
            await self._cancel_watcher()
        for event in self._finish_background():      # the process takes its agents with it: they end as stopped
            try:
                await self.emit(event)
            except Exception as exc:                  # noqa: BLE001  never in the way of the kill
                errlog.log(f"conversation {self.conversation_id}: agent end not sent", exc)
        eng, self._eng, self._eng_key = self._eng, None, None
        if eng is not None:
            await eng.close()

    @staticmethod
    def _error_text(ev: dict[str, Any]) -> str:
        """The engine's own message; when it sent none, the code and where to look, never a bare "something failed"."""
        from hokma.i18n import t

        code = str(ev.get("code") or "engine_error")
        return str(ev.get("message") or "").strip() or t("chat.err.no_detail", code=code)

    @staticmethod
    def _mark_connection_failed(connection: Any, code: str) -> None:
        """A chat that cannot open its engine must not leave "Connected" on the connection (bug 1095): the saved
        status came from an earlier test, before the reinstall or the update."""
        cid = connection.get("id") if isinstance(connection, dict) else None
        if cid is None:
            return
        try:
            from hokma.db import get_database

            with get_database().write() as u:
                u.engine_connections.set_status(cid, "error", code)
        except Exception:                                   # noqa: BLE001  (never in the way of the error itself)
            pass

    async def _ensure_engine(self, conv: ConversationRecord, project: ProjectRecord,
                             mode: str = modes.DEFAULT_MODE) -> EngineSession:
        adapter, connection, model = self.engine.resolve_engine(conv)
        replaced = self.engine.fallbacks.pop(self.conversation_id, None)
        if replaced:
            from hokma.i18n import t

            await self.emit({"type": "engine_notice", "message": t("chat.notice.engine_fallback", **{"from": replaced["from"], "to": replaced["to"]})})
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
            errlog.log(f"conversation {self.conversation_id}: the engine did not open ({exc.code}): {exc.message}", exc)
            self._mark_connection_failed(connection, exc.code)
            raise TurnError(exc.code, exc.message) from exc
        self._session_id = self._eng.session_ref or self._session_id
        self._eng_key = key
        return self._eng

    # ------------------------------------------------------------------ turn

    async def _turn(self, conv: ConversationRecord, project: ProjectRecord, blocks: list[dict[str, Any]],
                    user_text: str, meta: dict[str, Any], message_id: str | None = None) -> None:
        await self._cancel_watcher()            # one reader of the pipe at a time (bug 1257)
        store = self.engine.store
        turn_id = uuid.uuid4().hex[:12]
        ctx = _TurnCtx(conv, turn_id, AgentTracker(turn_id))
        self.live = {"turn_id": turn_id, "parts": ctx.parts, "tools": ctx.tools, "agents": ctx.agents}
        try:
            user_msg_id = message_id or store.add_message(conv.id, "user", user_text, meta=meta).id    # a queued message was saved when it was written
            if not store.get_conversation(conv.id).title:
                title = self.engine.make_title(user_text)
                store.update_conversation(conv.id, title=title)
                # the same body as everywhere else: a general conversation says `project: null`, not the working folder
                await self.emit({"type": "conversation", **self.engine._conv_body(conv, False), "title": title})
            await self.emit({"type": "turn_start", "turn_id": turn_id, "message_id": user_msg_id})
            mode = self.engine.permission_mode()
            if modes.snapshots_wanted(mode):
                await self.engine.take_checkpoint(project.path, self.conversation_id)
            eng = await self._ensure_engine(conv, project, mode)
            async for ev in eng.send(blocks):
                if await self._handle(ctx, eng, ev):
                    break
            if not ctx.result_seen:                            # the engine ended without a verdict
                ctx.status = "stopped" if self._stop_requested else "error"
                if ctx.status == "error":
                    ctx.error = TurnError("engine_exited", "the engine ended the turn without an answer")
        except TurnError as exc:
            ctx.status, ctx.error = "error", exc
        except (OSError, asyncio.CancelledError) as exc:
            ctx.status = "stopped" if self._stop_requested else "error"
            if ctx.status == "error":
                ctx.error = TurnError("engine_exited", f"{type(exc).__name__}: {exc}".strip())
            if isinstance(exc, asyncio.CancelledError):
                raise
        except Exception as exc:                               # a bug while starting the engine: say what it was
            ctx.status = "error"
            ctx.error = TurnError("engine_error", f"{type(exc).__name__}: {exc}".strip())
            errlog.log(f"conversation {self.conversation_id}: the turn failed", exc)
        finally:
            try:
                await asyncio.wait_for(self._publish_blocks(turn_id, ctx.guard_blocks), self.step_limit)
            except Exception as exc:                       # noqa: BLE001  never in the way of closing the turn
                errlog.log(f"conversation {self.conversation_id}: guard blocks not published", exc)
            await self._finish_turn(ctx, ctx.status, ctx.error)

    async def _handle(self, ctx: "_TurnCtx", eng: EngineSession, ev: dict[str, Any], background: bool = False) -> bool:
        """One engine event of a turn. True: the answer is complete and the caller stops reading. `background` is a
        turn the CLI opened by itself (bug 1257): its `done` and its errors close nothing, the caller sees the pause."""
        conv, turn_id, parts, tools, agents = ctx.conv, ctx.turn_id, ctx.parts, ctx.tools, ctx.agents
        store = self.engine.store
        kind = ev["type"]
        if not ctx.announced and ((kind == "text" and ev.get("text")) or kind in ("message_break", "tool_use", "tool_result", "tool_output",
                                           "permission_request") or (kind == "done" and not parts and ev.get("result"))):
            await self._announce(ctx)
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
            consumed = self._offer(ctx, "on_tool_use", ev)
            if consumed is not None:                   # an agent starts or does a step: the panel, not the chat
                for out in consumed:
                    await self.emit(out)
                return False
            summary = stream.summarize_tool(ev.get("name", ""), ev.get("input"))
            tools.append({"id": ev.get("id", ""), "name": ev.get("name"), **summary,
                          "at": sum(len(p) for p in parts), "is_error": None, "result": None})
            await self.emit({"type": "tool_use", "turn_id": turn_id, "tool_use_id": ev.get("id", ""),
                             "name": ev.get("name", ""), "input": ev.get("input") or {}, "summary": summary})
        elif kind == "tool_result":
            await self._publish_blocks(turn_id, ctx.guard_blocks)
            consumed = self._offer(ctx, "on_tool_result", ev)
            if consumed is not None:
                for out in consumed:
                    await self.emit(out)
                return False
            for line in tools:
                if line["id"] == ev.get("id"):
                    line["is_error"] = bool(ev.get("is_error"))
                    line["result"] = str(ev.get("text") or "")[:MAX_STORED_RESULT]
            await self.emit({"type": "tool_result", "turn_id": turn_id, "tool_use_id": ev.get("id", ""),
                             "is_error": bool(ev.get("is_error")),
                             "text": str(ev.get("text") or "")[:stream.MAX_RESULT_TEXT]})
        elif kind in ("task_start", "task_end"):          # background agents and commands of the CLI
            # A start belongs to the turn that is reading it; an end may belong to an agent of an earlier turn.
            for tracker in ([agents] if kind == "task_start" else self._trackers(ctx)):
                outs = tracker.on_task(ev, tools)
                if any(o["type"] in ("agent_end", "shell_end") for o in outs):
                    self._persist_agents(tracker)
                for out in outs:
                    await self.emit(out)
        elif kind == "tool_output":                    # live output of a command Hokma runs (task 3175)
            chunk = str(ev.get("text") or "")
            for line in tools:
                if line["id"] == ev.get("id"):
                    line["output"] = (line.get("output", "") + chunk)[-MAX_LIVE_OUTPUT:]
            await self.emit({"type": "tool_output", "turn_id": turn_id, "tool_use_id": ev.get("id", ""), "text": chunk})
        elif kind == "permission_request":
            if (ev.get("input") or {}).get("cancelled"):
                await self.resolve(str(ev.get("request_id", "")), "deny", "engine")
            else:
                await self._on_permission_request(eng, turn_id, ev, self._trackers(ctx))
        elif kind == "done":
            ctx.result_seen = True
            if ev.get("status") == "stopped" or self._stop_requested:
                ctx.status = "stopped"
            else:
                got = ev.get("usage") or {}
                if background:                           # each automatic turn adds to the one message that groups them
                    for key in ("tokens_in", "tokens_out", "cache_read", "cost_usd"):
                        ctx.usage[key] = ctx.usage.get(key, 0) + (got.get(key) or 0)
                else:
                    ctx.usage = got or ctx.usage
                if not parts and ev.get("result"):
                    parts.append(str(ev["result"]))
                    await self.emit({"type": "delta", "turn_id": turn_id, "text": parts[0]})
            return not background
        elif kind == "engine_notice":                   # bug 1173: "model X is gone, you now use Y"
            await self.emit({"type": "engine_notice", "message": str(ev.get("message") or "")[:500]})
        elif kind == "error":
            ctx.result_seen = True
            if self._stop_requested:
                ctx.status = "stopped"
            else:
                ctx.status, ctx.error = "error", TurnError(ev.get("code", "engine_error"), self._error_text(ev))
            return not background
        if self._bg:
            self._bg = [t for t in self._bg if t.working()]
        return False

    # ----------------------------------------------- agents that outlive their turn (bug 1257)

    def _trackers(self, ctx: "_TurnCtx") -> list[AgentTracker]:
        """The agents of the turn that reads and, after them, those of earlier turns that still work."""
        return [ctx.agents, *[t for t in self._bg if t is not ctx.agents]]

    def _offer(self, ctx: "_TurnCtx", method: str, ev: dict[str, Any]) -> list[dict[str, Any]] | None:
        for tracker in self._trackers(ctx):
            consumed = getattr(tracker, method)(ev)
            if consumed is not None:
                if any(o["type"] in ("agent_end", "shell_end") for o in consumed):
                    self._persist_agents(tracker)
                return consumed
        return None

    @property
    def holds_agents(self) -> bool:
        """True while this process still answers for agents of a saved message: their state on screen is real."""
        return self.watching or bool(self._bg)

    def _persist_agents(self, tracker: AgentTracker) -> None:
        """An agent of a saved answer ended: the saved message carries its end, so a reopened conversation is not
        left with an agent that works for ever (bug 1257)."""
        if tracker.saved_as is None:
            return
        try:
            self.engine.store.update_message_meta(*tracker.saved_as, agents=tracker.stored())
        except Exception as exc:                       # noqa: BLE001  never in the way of the turn
            errlog.log(f"conversation {self.conversation_id}: the end of an agent was not saved", exc)

    @property
    def watching(self) -> bool:
        """True while the chat waits for agents started in the background: a conversation may be idle and still hold them."""
        return self._watcher is not None and not self._watcher.done()

    def _finish_background(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for tracker in self._bg:
            out += tracker.finish()
            self._persist_agents(tracker)
        self._bg.clear()
        return out

    async def _cancel_watcher(self) -> None:
        watcher, self._watcher = self._watcher, None
        if watcher is None or watcher.done() or watcher is asyncio.current_task():
            return
        watcher.cancel()
        await asyncio.wait({watcher}, timeout=self.step_limit)

    def _arm(self, conv: ConversationRecord) -> None:
        """After a turn: if the engine still has agents working in the background, keep reading what it says."""
        eng = self._eng
        if self.watching or eng is None or not eng.alive or not hasattr(eng, "follow"):
            return
        if not getattr(eng, "background_pending", False):
            return
        self._watcher = asyncio.create_task(self._watch(conv, eng))

    async def _open_continuation(self, conv: ConversationRecord) -> "_TurnCtx | None":
        """The turn the CLI opens by itself when a background agent ends: a message of the assistant that nobody asked
        for. The panel shows it as a new answer with its origin; the agent's own detail stays in the side panel."""
        if self.state != "idle" or self._cont is not None:
            return None                              # the owner's turn took the conversation first
        self.state = "running"
        self._stop_requested = False
        self._turn_done.clear()
        self.last_used = time.monotonic()
        turn_id = uuid.uuid4().hex[:12]
        ctx = _TurnCtx(conv, turn_id, AgentTracker(turn_id), origin="agents")
        self._cont = ctx
        self.live = {"turn_id": turn_id, "parts": ctx.parts, "tools": ctx.tools, "agents": ctx.agents}
        return ctx

    async def _announce(self, ctx: "_TurnCtx") -> None:
        """The row of an automatic turn appears when it has something to show: a CLI turn that says nothing leaves no
        empty "summary of the agents" in the panel (bug 1257)."""
        if ctx.announced:
            return
        ctx.announced = True
        await self.emit({"type": "turn_start", "turn_id": ctx.turn_id, "message_id": None, "origin": ctx.origin})

    async def _watch(self, conv: ConversationRecord, eng: Any) -> None:
        cont: _TurnCtx | None = None
        idle = _TurnCtx(conv, "", AgentTracker(""))        # what is read between turns: steps and ends of agents
        ended = ""
        gen = eng.follow(self.engine.group_window, self.engine.bg_wait, self.engine.init_wait)
        try:
            async for ev in gen:
                kind = ev["type"]
                if "owed" in ev:
                    self._owed = int(ev["owed"])
                if kind == "bg_notice":
                    continue                                   # the CLI will open a turn: nothing opens here until its init (bug 1257)
                if kind == "continuation" or (kind == "text" and cont is None):
                    if cont is None:
                        cont = await self._open_continuation(conv)
                    elif kind == "continuation" and cont.parts and not cont.parts[-1].endswith("\n\n"):
                        cont.parts.append("\n\n")                      # a second automatic turn: same message
                        await self.emit({"type": "delta", "turn_id": cont.turn_id, "text": "\n\n"})
                    if kind != "text":
                        continue
                if kind in ("quiet", "init_timeout"):
                    if kind == "init_timeout":
                        errlog.log(f"conversation {self.conversation_id}: the turn the CLI announced did not open; not waiting for it")
                    if cont is not None:
                        closing, cont = cont, None
                        await self._finish_turn(closing, closing.status, closing.error)
                    if self._queued is not None and self._owed == 0:
                        break                                  # the owner's message goes now, through the same pipe
                    continue
                if kind in ("timeout", "closed"):
                    ended = kind
                    break
                await self._handle(cont or idle, eng, ev, background=True)
            if not ended and cont is not None:
                closing, cont = cont, None
                await self._finish_turn(closing, closing.status, closing.error)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                               # noqa: BLE001  a bug here must not freeze the chat
            ended = "error"
            errlog.log(f"conversation {self.conversation_id}: the wait for background agents failed", exc)
        finally:
            try:
                await gen.aclose()
            except Exception:                                  # noqa: BLE001
                pass
        if ended:
            await self._abandon(conv, eng, cont, ended)
        self._owed = 0
        if self._watcher is asyncio.current_task():
            self._watcher = None                               # this reader is done: the owner's turn may take the pipe
        await self._start_queued()

    async def _start_queued(self) -> None:
        """Writes the message the owner left while the CLI owed a turn. Never lost: if it cannot start, the panel is told."""
        queued, self._queued = self._queued, None
        if queued is None:
            return
        self._owed = 0
        if not self.start_turn(*queued):
            await self._mark_unsent(queued[5], notice=False)
            await self.emit({"type": "error", "code": "busy", "message": "a turn is already running"})

    async def _abandon(self, conv: ConversationRecord, eng: Any, cont: "_TurnCtx | None", reason: str) -> None:
        """The wait cannot go on: the agents ran out of time, or the process closed. Whatever still works is stopped
        and the owner is told; the next message opens the engine again and resumes the conversation."""
        from hokma.i18n import t

        errlog.log(f"conversation {self.conversation_id}: the wait for background agents ended ({reason})")
        if reason == "timeout":
            minutes = max(1, round(self.engine.bg_wait / 60))
            await self.emit({"type": "engine_notice", "message": t("chat.notice.agents_timeout", minutes=minutes)})
        if cont is not None:
            await self._finish_turn(cont, "stopped" if reason == "timeout" else "error",
                                    None if reason == "timeout" else TurnError("engine_exited", "the engine closed"))
        else:
            await self._drop_process(from_watcher=True)

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

    async def _finish_turn(self, ctx: "_TurnCtx", status: str, error: TurnError | None) -> None:
        conv, turn_id, parts, tools, usage, blocks, agents = (ctx.conv, ctx.turn_id, ctx.parts, ctx.tools, ctx.usage,
                                                              ctx.guard_blocks, ctx.agents)
        stale = self.live is None or self.live.get("turn_id") != turn_id   # released by force by a stop already
        message_id = None
        try:
            if not stale:
                await self._settle_agents(ctx, status)
            for pending in list(self.pending.values()):
                await self.resolve(pending.request_id, "deny", "engine")
            if self._waiters:
                waiters = list(self._waiters)
                _done, late = await asyncio.wait(waiters, timeout=self.step_limit)
                for waiter in late:             # an answer the engine never read must not hold the turn open
                    waiter.cancel()
            text = "".join(parts)
            kept = [{k: v for k, v in t.items() if k != "output"} for t in tools[:MAX_STORED_TOOLS]]
            kept_agents = agents.stored()
            if text or kept or kept_agents or (status == "complete" and not ctx.origin):
                meta = {**({"origin": ctx.origin} if ctx.origin else {}),
                        **({"tools": kept} if kept else {}),
                        **({"agents": kept_agents} if kept_agents else {}),
                        **({"blocks": blocks[:MAX_STORED_TOOLS]} if blocks else {})}
                rec = self.engine.store.add_message(conv.id, "assistant", text, usage["tokens_in"],
                                                    usage["tokens_out"], status, meta or None)
                message_id = rec.id
                if kept_agents:
                    agents.saved_as = (conv.id, rec.id)
        except Exception as exc:                # noqa: BLE001  saving must never keep the conversation busy
            errlog.log(f"conversation {self.conversation_id}: the answer could not be saved", exc)
        if stale:
            return                              # the conversation was freed already and may run a new turn now
        self.live = None                        # in the same step as the save: a socket that opens now sees the message
        if self._cont is ctx:
            self._cont = None
        try:
            if status != "complete":
                await self._bounded(self._drop_process(from_watcher=ctx.origin != ""), "close")   # replaced and resumed on the next turn
        finally:
            self.state = "idle"
            self.last_used = time.monotonic()
        try:
            if not ctx.announced and (error is not None or message_id is not None):
                await self._announce(ctx)
            if not ctx.announced:
                pass                              # an automatic turn with nothing to show: the panel never saw it
            elif error is not None:
                errlog.log(f"conversation {self.conversation_id}: {error.code}: {error.message}"[:4000])
                await self.emit({"type": "error", "turn_id": turn_id, "code": error.code, "message": error.message})
            else:
                zero = {"tokens_in": 0, "tokens_out": 0, "cache_read": 0, "cost_usd": 0.0}
                await self.emit({"type": "done", "turn_id": turn_id, "message_id": message_id,
                                 "stopped": status == "stopped", "usage": usage if status == "complete" else zero,
                                 "engine_session": self._session_id if self._session_started else None})
        finally:
            self._turn_done.set()
        if status == "complete" and error is None and not ctx.origin:
            self._arm(conv)

    async def _settle_agents(self, ctx: "_TurnCtx", status: str) -> None:
        """The end of a turn. The agents it started in the background are not over: while the CLI still runs them they
        stay open (bug 1257) and the conversation goes on reading until each one reports. In any other case the turn
        closes for real and whatever still works is stopped, before `done`."""
        eng = self._eng
        keep = status == "complete" and eng is not None and eng.alive and bool(getattr(eng, "background_pending", False))
        out: list[dict[str, Any]] = []
        if keep:
            if ctx.agents.working() and ctx.agents not in self._bg:
                self._bg.append(ctx.agents)
            self._bg = [t for t in self._bg if t.working()]
        else:
            out += ctx.agents.finish()
            out += self._finish_background()
        for event in out:
            await self.emit(event)

    # ----------------------------------------------------------- permissions

    async def _on_permission_request(self, eng: EngineSession, turn_id: str, ev: dict[str, Any],
                                     trackers: list[AgentTracker] | None = None) -> None:
        request_id = str(ev.get("request_id", ""))
        tool = str(ev.get("tool", ""))
        pending = Pending(request_id, str(ev.get("id", "")), tool, ev.get("input") or {},
                          stream.summarize_tool(tool, ev.get("input")),
                          time.time() + self.engine.permission_timeout)
        self.pending[request_id] = pending
        who = next((w for w in (t.agent_of_step(pending.tool_use_id) for t in trackers or []) if w), None)
        await self.emit({"turn_id": turn_id, **pending.event(), **({"agent": who} if who else {})})
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
            "human" if human else "system", pending.channel)
        await self.emit({"type": "permission_resolved", "request_id": pending.request_id, "decision": decision,
                         "by": by})


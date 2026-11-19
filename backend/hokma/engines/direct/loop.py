"""The direct engine: no CLI, Hokma talks HTTP to the model and RUNS the tools itself.

That is why every tool call passes the broker before it runs, and in "ask" mode the owner is asked before anything
that is not a plain read. Cycle: model -> tool calls -> results -> model, until the model answers without a call.
"""
from __future__ import annotations

import asyncio
import sys
import threading
import uuid
from typing import Any, AsyncIterator, Callable, Iterator

from hokma.core.config import hokma_data_dir, hokma_home
from hokma.engines import catalog, diagnose, models, secrets
from hokma.engines.base import EngineCaps, EngineContext, EngineError, EngineEvent, user_images, user_text
from hokma.engines.envfilter import filtered_env
from hokma.permissions import modes
from hokma.engines.direct import tools, transcript, wire_anthropic, wire_openai
from hokma.engines.direct.wire import WireError
from hokma.permissions.broker import BrokerCtx, check

MAX_ROUNDS = 40
MAX_REPLACEMENTS = 3               # models tried in one turn after the saved one was refused (bug 1173)
MAX_RESULT_EVENT = 4000
WIRES = {"openai": wire_openai, "anthropic": wire_anthropic}


def broker_ctx(ctx: EngineContext) -> BrokerCtx:
    given = ctx.extra.get("broker")
    if given is not None:
        return given
    from hokma.kit.settings import protected_paths

    return BrokerCtx(data=str(hokma_data_dir()), roots=[str(hokma_home()), ctx.cwd],
                     protect=[str(p) for p in protected_paths()], conversation_id=ctx.conversation_id,
                     task_id=ctx.task_id, shell="PowerShell" if sys.platform == "win32" else "Bash")


def broker_input(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """The native arguments in the shape the protection rules read (they were written for Claude Code's tools)."""
    out = dict(args)
    if name in ("read_file", "write_file", "edit_file") and "path" in args:
        out["file_path"] = args["path"]
    if name == "edit_file":
        out["old_string"], out["new_string"] = args.get("old", ""), args.get("new", "")
        out["replace_all"] = bool(args.get("replace_all"))
    if name == "list_dir":
        out["path"] = args.get("path") or "."
        out["pattern"] = "*"
    return out


class DirectSession:
    def __init__(self, ctx: EngineContext, conn: dict[str, Any], key: str | None, extra_tools: "ToolBox | None" = None
                 ) -> None:
        self.ctx, self.conn = ctx, conn
        self.provider = conn.get("provider")
        self.wire = WIRES[conn.get("wire") or "openai"]
        self.base_url = conn.get("base_url") or catalog.provider(self.provider).get("base_url") or ""
        self.key = key
        self.key_name = f"direct-{ctx.conversation_id or 'task-' + ctx.task_id}"
        self.session_ref: str | None = self.key_name
        self.history = transcript.load(self.key_name)
        if not self.history:
            for h in ctx.history:
                if h.get("text"):
                    self._remember({"role": h["role"], "text": h["text"]})
        self.extra_tools = extra_tools
        self._answers: dict[str, asyncio.Future] = {}
        self._interrupted = False
        self._cancels: set[threading.Event] = set()
        self._stream: Any = None
        self._closed = False

    @property
    def alive(self) -> bool:
        return not self._closed

    def _remember(self, message: dict[str, Any]) -> None:
        self.history.append(message)
        # the transcript keeps the text only: an image travels with the turn it was sent in, not on disk
        transcript.append(self.key_name, {k: v for k, v in message.items() if k != "images"})

    # ------------------------------------------------------------------ turn

    async def send(self, blocks: list[dict[str, Any]]) -> AsyncIterator[EngineEvent]:
        yield {"type": "session", "ref": self.key_name}
        images = user_images(blocks)
        self._remember({"role": "user", "text": user_text(blocks), **({"images": images} if images else {})})
        usage = {"tokens_in": 0, "tokens_out": 0, "cache_read": 0, "cost_usd": 0.0}
        defs = tools.definitions() + (self.extra_tools.definitions() if self.extra_tools else [])
        bctx = broker_ctx(self.ctx)
        tools.PROTECTED[:] = bctx.protect
        for message in await asyncio.to_thread(models.take_notices, self.conn.get("id")):
            yield {"type": "engine_notice", "message": message}         # a model replaced at start (bug 1173)
        replaced = 0
        for _ in range(MAX_ROUNDS):
            text, calls = "", []
            self._stream = self.wire.open_stream(self.base_url, self.key, self.ctx.model, self.ctx.instructions,
                                                 self.history, defs)
            try:
                async for kind, value in self._pump(self.wire.parse(self._stream.lines())):
                    if kind == "text":
                        text += value
                        yield {"type": "text", "text": value}
                    elif kind == "call":
                        calls.append(value)
                    elif kind == "usage":
                        for k in ("tokens_in", "tokens_out", "cache_read"):
                            usage[k] += int(value.get(k) or 0)
            except WireError as exc:
                if self._interrupted:
                    yield {"type": "done", "status": "stopped"}
                else:
                    code = diagnose.classify(self.provider, exc.status, exc.body)
                    message = diagnose.redact(str(exc), self.key)
                    from hokma.engine import errlog          # bug 1173: the provider's real answer, never the key

                    errlog.log(f"{self.provider or 'api'} model {self.ctx.model!r}: {code}: {message}", scope="engines")
                    if code == "model" and replaced < MAX_REPLACEMENTS and self.conn.get("id") is not None:
                        # The model is gone or does not take this request: ask the provider's list again, take the
                        # replacement for this level, say so and go on. A model the student chose is not replaced.
                        old = self.ctx.model
                        try:
                            new, manual = await asyncio.to_thread(models.replace, self.conn["id"], self.ctx.tier, old)
                        except Exception as exc:          # noqa: BLE001  (the refusal is still told as it came)
                            errlog.log(f"model replacement failed: {type(exc).__name__}", scope="engines")
                            new, manual = None, False
                        if new and new != old:
                            replaced += 1
                            self.ctx.model = new
                            yield {"type": "engine_notice", "message": models.replaced_text(old, new)}
                            continue
                        if manual:
                            code = "model_choice"
                    yield {"type": "error", "code": code, "message": message}
                return
            except Exception as exc:
                if self._interrupted:
                    yield {"type": "done", "status": "stopped"}
                else:
                    yield {"type": "error", "code": "engine_error", "message": f"{type(exc).__name__}: {exc}"}
                return
            finally:
                self._stream.close()
            self._remember({"role": "assistant", "text": text, "calls": calls})
            if self._interrupted:
                yield {"type": "done", "status": "stopped"}
                return
            if not calls:
                yield {"type": "done", "status": "complete", "usage": usage}
                return
            results = []
            for call in calls:
                yield {"type": "tool_use", "id": call["id"], "name": call["name"], "input": call["input"]}
                run_text, is_error = "", False
                async for item in self._run_call(call, bctx):
                    if item["type"] == "result":
                        run_text, is_error = item["text"], item["is_error"]
                    else:
                        yield item
                results.append({"id": call["id"], "name": call["name"], "text": run_text, "is_error": is_error})
                yield {"type": "tool_result", "id": call["id"], "is_error": is_error,
                       "text": run_text[:MAX_RESULT_EVENT]}
                if self._interrupted:
                    break
            self._remember({"role": "tool", "results": results})
            if self._interrupted:
                yield {"type": "done", "status": "stopped"}
                return
        yield {"type": "error", "code": "engine_error", "message": "too many tool rounds in one turn"}

    async def _pump(self, gen: Iterator[tuple[str, Any]]) -> AsyncIterator[tuple[str, Any]]:
        loop = asyncio.get_running_loop()
        sentinel = object()
        it = iter(gen)
        while True:
            item = await loop.run_in_executor(None, next, it, sentinel)
            if item is sentinel:
                return
            yield item

    async def _run_call(self, call: dict[str, Any], bctx: BrokerCtx) -> AsyncIterator[dict[str, Any]]:
        name, args = call["name"], call["input"]
        if self.extra_tools is not None and self.extra_tools.owns(name):
            broker_name, request = self.extra_tools.broker_request(name, args)
        else:
            broker_name, request = name, broker_input(name, args)
        verdict = check(broker_name, request, cwd=self.ctx.cwd, ctx=bctx)
        if not verdict.allowed:
            yield {"type": "result", "text": verdict.text, "is_error": True}
            return
        if modes.asks(self.ctx.mode, name) and name not in tools.READ_ONLY:
            request_id = uuid.uuid4().hex[:12]
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._answers[request_id] = fut
            yield {"type": "permission_request", "request_id": request_id, "id": call["id"], "tool": name,
                   "input": args}
            allow, reason = await fut
            self._answers.pop(request_id, None)
            if not allow:
                yield {"type": "result", "text": reason, "is_error": True}
                return
        if self.extra_tools is not None and self.extra_tools.owns(name):
            text, is_error = await asyncio.to_thread(self.extra_tools.run, name, args)
        elif name == "run_command":
            async for item in self._run_command(call, args):
                yield item
            return
        else:
            text, is_error = await asyncio.to_thread(tools.run, name, self.ctx.cwd, args, self._env())
        yield {"type": "result", "text": text, "is_error": is_error}

    async def _run_command(self, call: dict[str, Any], args: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """The command runs in a thread; its output comes back as `tool_output` events while it runs, and a Stop
        sets `cancel`, which ends the command and what it started (task 3175)."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str] = asyncio.Queue()
        cancel = threading.Event()
        if self._interrupted:
            cancel.set()
        self._cancels.add(cancel)

        def on_output(text: str) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, text)

        work = asyncio.ensure_future(asyncio.to_thread(tools.run, "run_command", self.ctx.cwd, args, self._env(),
                                                       on_output, cancel))
        try:
            while not work.done():
                getter = asyncio.ensure_future(queue.get())
                await asyncio.wait({work, getter}, return_when=asyncio.FIRST_COMPLETED)
                if getter.done():
                    yield {"type": "tool_output", "id": call["id"], "text": getter.result()}
                else:
                    getter.cancel()
            while not queue.empty():
                yield {"type": "tool_output", "id": call["id"], "text": queue.get_nowait()}
            text, is_error = work.result()
        finally:
            cancel.set()                    # the turn went away (closed, cancelled): the command must not outlive it
            self._cancels.discard(cancel)
        yield {"type": "result", "text": text, "is_error": is_error}

    def _env(self) -> dict[str, str]:
        """What a command run by the model sees: the allow list plus Hokma's work variables, like every engine."""
        env = filtered_env()
        env.update(modes.work_env(self.ctx.cwd, self.ctx.conversation_id, self.ctx.task_id,
                                  self.ctx.agent or ""))
        return env

    # --------------------------------------------------------------- control

    async def answer_permission(self, request_id: str, allow: bool, reason: str, tool_input: Any = None) -> None:
        fut = self._answers.get(request_id)
        if fut is not None and not fut.done():
            fut.set_result((allow, reason))

    async def interrupt(self) -> None:
        self._interrupted = True
        for cancel in list(self._cancels):          # a running command and its whole tree end now
            cancel.set()
        if self._stream is not None:
            self._stream.close()
        for fut in list(self._answers.values()):
            if not fut.done():
                fut.set_result((False, "The owner stopped the turn."))

    async def close(self) -> None:
        self._closed = True
        await self.interrupt()
        if self.extra_tools is not None and hasattr(self.extra_tools, "close"):
            self.extra_tools.close()


class ToolBox:
    """Extra tools (Hokma's MCP, approved MCP servers) exposed to the model next to the native ones."""

    def definitions(self) -> list[dict[str, Any]]: ...
    def owns(self, name: str) -> bool: ...
    def broker_request(self, name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]: ...
    def run(self, name: str, args: dict[str, Any]) -> tuple[str, bool]: ...


class DirectAdapter:
    kind = "api"

    def __init__(self, toolbox_factory: Callable[[EngineContext], ToolBox | None] | None = None) -> None:
        self.toolbox_factory = toolbox_factory

    def caps(self) -> EngineCaps:
        return {"hooks": False, "os_sandbox": False, "native_subagents": False, "resume": True, "images": True,
                "mcp": True}

    def new_ref(self) -> str | None:
        return None

    async def open(self, ctx: EngineContext) -> DirectSession:
        conn = ctx.connection
        if not conn:
            raise EngineError("no_connection", "no engine is connected")
        if not ctx.model and conn.get("id") is not None:
            # Never tested, or saved before 0.0.39: the model comes from the provider's list now (bug 1173). When the
            # list itself is refused (a bad key, the region, the quota) that refusal is the reason shown.
            try:
                ctx.model = (await asyncio.to_thread(models.replace, conn["id"], ctx.tier, ""))[0] or ""
                err = None if ctx.model else await asyncio.to_thread(models.list_error, conn["id"])
            except Exception as exc:                     # noqa: BLE001  (no saved row: "no model", never a crash)
                from hokma.engine import errlog

                errlog.log(f"model for connection {conn['id']} not resolved: {type(exc).__name__}", scope="engines")
                err = None
            if not ctx.model:
                if err:
                    raise EngineError(diagnose.classify(conn.get("provider"), err["status"], err["body"]),
                                      f"model list: HTTP {err['status']}: {err['body'][:400]}")
        if not ctx.model:
            raise EngineError("no_model", "no model is set for this level; pick one in Settings > Engines")
        if not (conn.get("base_url") or catalog.provider(conn.get("provider")).get("base_url")):
            raise EngineError("no_connection", "the connection has no URL")
        key = secrets.get(conn["id"]) if conn.get("id") is not None else None
        if key is None and conn.get("auth_kind") != "local":
            raise EngineError("auth", "the key of this connection is not in the secure store")
        box = await asyncio.to_thread(self.toolbox_factory, ctx) if self.toolbox_factory else None
        return DirectSession(ctx, conn, key, box)

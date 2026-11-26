"""Claude Code as an engine: `claude -p` over stream-json. This is what `engine/session.py` did on its own before
0.0.9, moved behind the contract without a change in behaviour. Hooks stay active (guard and hardblock)."""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, AsyncIterator

from hokma.engine import stream
from hokma.engine.process import ClaudeProcess, find_claude
from hokma.engines import diagnose
from hokma.engines.envfilter import filtered_env
from hokma.engines.base import EngineCaps, EngineContext, EngineError, EngineEvent
from hokma.permissions import modes

BASE_FLAGS = ["-p", "--output-format", "stream-json", "--input-format", "stream-json",
              "--include-partial-messages", "--verbose", "--permission-prompt-tool", "stdio"]
SECRET_ENV = ("HOKMA_SESSION_TOKEN", "HOKMA_TOKEN")
TIER_ALIAS = {"fast": "haiku", "balanced": "sonnet", "deep": "opus"}


def _with_key_helper(args: list[str], helper: str) -> list[str]:
    """Copy the generated settings file next to itself with `apiKeyHelper` added and point `--settings` at the copy.
    The copy is rebuilt on every launch, like the original, inside the protected configuration folder."""
    import json
    from pathlib import Path

    out = list(args)
    if "--settings" in out:
        i = out.index("--settings") + 1
        src = Path(out[i])
        try:
            data = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data["apiKeyHelper"] = helper
        dst = src.with_name("settings.engine.json")
        dst.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        out[i] = str(dst)
    return out


class ClaudeCodeSession:
    def __init__(self, proc: ClaudeProcess, ref: str, partial: bool = True) -> None:
        self._proc = proc
        self._partial = partial          # false for scheduled runs: whole assistant messages, no deltas
        self.session_ref: str | None = ref
        self._interrupted = False
        # Bug 1257: the CLI goes on after its first `result` while an agent started in the background works. When the
        # agent ends it opens a turn of its own to tell the model, and that turn only exists on this pipe.
        self._bg: set[str] = set()       # background tasks of the main thread that have not ended
        self._bg_since = 0.0             # when the first of them started (monotonic), for the wait limit

    @property
    def alive(self) -> bool:
        return self._proc.alive

    @property
    def pid(self) -> int:
        return self._proc._proc.pid

    @property
    def background_pending(self) -> bool:
        """True while something started in the background of the main thread still works."""
        return bool(self._bg)

    def _track(self, msg: dict[str, Any]) -> bool:
        """Keeps the list of background tasks; True for the end of one, which the CLI answers with a turn of its own."""
        tracked = stream.task_track(msg)
        if tracked is None:
            return False
        op, key = tracked
        if op == "add":
            if not self._bg:
                self._bg_since = time.monotonic()
            self._bg.add(key)
            return False
        self._bg.discard(key)
        return op == "done"

    async def _translate(self, msg: dict[str, Any], seen: dict[str, bool]) -> tuple[list[EngineEvent], bool]:
        """One line of the CLI as engine events; the flag is True when the line closes a turn (`result`)."""
        proc = self._proc
        out: list[EngineEvent] = []
        kind = msg["type"]
        if kind == "system" and msg.get("subtype") == "init":
            out.append({"type": "session", "ref": self.session_ref or ""})
        elif kind == "stream_event":
            if stream.is_message_start(msg) and seen["text"]:
                out.append({"type": "message_break"})
            text = stream.text_delta(msg)
            if text:
                seen["text"] = True
                out.append({"type": "text", "text": text})
        elif kind == "assistant":
            if not self._partial:
                whole = stream.assistant_text(msg)
                if whole:
                    if seen["text"]:
                        out.append({"type": "message_break"})
                    seen["text"] = True
                    out.append({"type": "text", "text": whole})
            parent = msg.get("parent_tool_use_id") or ""
            for block in stream.tool_uses(msg):
                out.append({"type": "tool_use", "id": block.get("id", ""), "name": block.get("name", ""),
                            "input": block.get("input") or {}, "parent": parent})
        elif kind == "user":
            parent = msg.get("parent_tool_use_id") or ""
            launched = stream.launch_receipt(msg)
            for res in stream.tool_results(msg):
                out.append({"type": "tool_result", "id": res.get("tool_use_id", ""),
                            "is_error": bool(res.get("is_error")), "text": str(res.get("text") or ""),
                            "parent": parent, "launched": launched})
        elif kind == "system":
            task = stream.task_event(msg)
            if task:
                out.append(task)
        elif kind == "control_request":
            request = msg.get("request") or {}
            request_id = str(msg.get("request_id", ""))
            if request.get("subtype") != "can_use_tool":
                await proc.write(stream.control_error(request_id, "unsupported request"))
            else:
                out.append({"type": "permission_request", "request_id": request_id,
                            "id": str(request.get("tool_use_id", "")), "tool": str(request.get("tool_name", "")),
                            "input": request.get("input") or {}})
        elif kind == "control_cancel_request":
            out.append({"type": "permission_request", "request_id": str(msg.get("request_id", "")), "tool": "",
                        "input": {"cancelled": True}})
        elif kind == "result":
            if stream.is_aborted(msg) or self._interrupted:
                out.append({"type": "done", "status": "stopped"})
            elif msg.get("is_error"):
                # The real text of the failure (a hook that blocked the prompt, an API error) and what the
                # CLI wrote on stderr: shown in the chat and kept in engine.log, never a bare "it failed".
                detail = str(msg.get("result") or msg.get("subtype") or "")
                tail = proc.stderr_tail()
                text = (detail + ("\n" + tail if tail else "")).strip()[:2000]
                out.append({"type": "error", "code": diagnose.classify_text(None, text), "message": text})
            else:
                out.append({"type": "done", "status": "complete", "usage": stream.usage_of(msg),
                            "result": "" if seen["text"] else str(msg.get("result") or "")})
            return out, True
        return out, False

    async def send(self, blocks: list[dict[str, Any]]) -> AsyncIterator[EngineEvent]:
        proc = self._proc
        seen = {"text": False}
        try:
            await proc.write(stream.user_message(blocks))
            while True:
                msg = await proc.read()
                if msg is None:
                    break
                self._track(msg)                    # an ending inside a turn is read by that turn: no automatic turn follows
                events, end = await self._translate(msg, seen)
                for event in events:
                    yield event
                if end:
                    if events and events[-1]["type"] != "done":
                        self._bg.clear()                         # a failed turn ends what was waited for
                    return
            if self._interrupted:
                yield {"type": "done", "status": "stopped"}
            else:
                await proc.terminate()
                yield {"type": "error", "code": "engine_exited",
                       "message": f"claude exited with code {proc.returncode}: {proc.stderr_tail()}".strip()}
        except OSError as exc:
            if self._interrupted:
                yield {"type": "done", "status": "stopped"}
            else:
                yield {"type": "error", "code": "engine_exited",
                       "message": f"{type(exc).__name__}: {exc} {proc.stderr_tail()}".strip()}

    async def follow(self, grace: float, max_wait: float, init_wait: float = 60.0) -> AsyncIterator[EngineEvent]:
        """What the CLI says after a turn ended while agents started in the background still work (bug 1257). Recorded with CLI
        2.1.332: when such an agent ends, the CLI opens a turn of its own to tell the model (system/init, the model's text,
        result), one per ending; an ending that falls inside a turn is read by that turn and opens none.

        Nothing is written. The lines are the same ones `send()` reads, plus these markers: `bg_notice` (a background
        task ended, the CLI owes a turn), `continuation` (that turn began), `quiet` (`grace` seconds went by after a
        notice or a turn with no new turn: whatever was open is complete), `init_timeout` (the owed turn did not begin
        within `init_wait` seconds of its notice, or within `grace` after a turn that left one owed: two notices merged
        into one turn; nothing is killed, the chat stops waiting for it), `timeout` (a task has worked `max_wait`
        seconds since it started) and `closed` (the process closed its output). `bg_notice`, `continuation` and
        `init_timeout` carry `owed`, how many turns the CLI still owes. It returns when nothing is owed and nothing is open."""
        proc = self._proc
        seen = {"text": False}
        in_turn = False
        group = False
        pending = 0                    # notices whose turn has not begun: the CLI owes one turn per notice
        await_until = 0.0
        quiet_at = time.monotonic() + grace
        try:
            while True:
                now = time.monotonic()
                deadline = (self._bg_since or now) + max_wait if self._bg else float("inf")
                if self._bg and now >= deadline and not in_turn:
                    yield {"type": "timeout"}
                    return
                if in_turn:
                    limit, due = deadline, "timeout"
                else:
                    options = []
                    if group:
                        options.append((quiet_at, "quiet"))
                    if pending:
                        options.append((await_until, "init"))
                    if self._bg:
                        options.append((deadline, "timeout"))
                    if not options:
                        return
                    limit, due = min(options)
                try:
                    msg = await asyncio.wait_for(proc.read(), None if limit == float("inf") else max(limit - now, 0.001))
                except asyncio.TimeoutError:
                    if due == "quiet":
                        group = False
                        yield {"type": "quiet"}
                        continue
                    if due == "init":
                        pending = 0
                        yield {"type": "init_timeout", "owed": 0}
                        continue
                    yield {"type": "timeout"}
                    return
                if msg is None:
                    yield {"type": "closed"}
                    return
                notice = self._track(msg)
                if msg["type"] == "system" and msg.get("subtype") == "init":
                    pending = max(pending - 1, 0)
                    in_turn, group, seen["text"] = True, True, False
                    yield {"type": "continuation", "owed": pending}
                elif notice and not in_turn:
                    pending += 1
                    group, quiet_at = True, time.monotonic() + grace
                    await_until = time.monotonic() + init_wait
                    yield {"type": "bg_notice", "owed": pending}
                events, end = await self._translate(msg, seen)
                for event in events:
                    yield event
                if end:
                    in_turn, group, quiet_at = False, True, time.monotonic() + grace
                    if pending:                                  # a turn owed after one that ended: it follows at once or the notices merged
                        await_until = time.monotonic() + grace
        except OSError as exc:
            if not self._interrupted:
                yield {"type": "error", "code": "engine_exited",
                       "message": f"{type(exc).__name__}: {exc} {proc.stderr_tail()}".strip()}

    async def answer_permission(self, request_id: str, allow: bool, reason: str, tool_input: Any = None) -> None:
        await self._proc.write(stream.permission_reply(request_id, allow, tool_input, reason))

    async def interrupt(self) -> None:
        self._interrupted = True
        if self._proc.alive:
            try:
                await self._proc.write(stream.interrupt_request(f"stop-{uuid.uuid4().hex[:8]}"))
            except OSError:
                pass

    async def close(self) -> None:
        await self._proc.terminate()

    def stderr_tail(self) -> str:
        return self._proc.stderr_tail()


class ClaudeCodeAdapter:
    kind = "claude_code"

    def __init__(self, owner: Any) -> None:
        """`owner` has `launch()`, `claude_bin` and `session_token` (the chat engine or the scheduler runner); they
        are read at open time, so a test or an update that swaps them is honoured."""
        self.owner = owner

    def caps(self) -> EngineCaps:
        return {"hooks": True, "os_sandbox": False, "native_subagents": True, "resume": True,
                "images": True, "mcp": True}

    def new_ref(self) -> str:
        return str(uuid.uuid4())

    async def open(self, ctx: EngineContext) -> ClaudeCodeSession:
        try:
            spec = self.owner.launch()
        except Exception as exc:                         # IntegrityError and any failure while preparing settings
            from hokma.kit.launch import IntegrityError

            code = "integrity_check" if isinstance(exc, IntegrityError) else "integrity"
            raise EngineError(code, f"{type(exc).__name__}: {exc}") from exc
        try:
            env = filtered_env()                     # allow list: no provider keys, no Hokma token, no market keys
        except Exception as exc:                     # the PATH or the environment could not be built (WinError 448, bug 1095)
            raise EngineError("env_failed", f"{type(exc).__name__}: {exc}") from exc
        env.update(spec.env)
        spec_args = list(spec.args)
        conn = ctx.connection
        if conn and conn.get("auth_kind") not in (None, "subscription"):
            from hokma.engines import service

            extra_env, helper = service.claude_env_for(conn)
            env.update(extra_env)
            if helper:
                spec_args = _with_key_helper(spec_args, helper)
        env.update(ctx.extra.get("env") or {})
        env.update(modes.work_env(ctx.cwd, ctx.conversation_id, ctx.task_id, ctx.agent or ""))
        claude = getattr(self.owner, "claude_bin", None) or find_claude(env)
        if not claude:
            raise EngineError("claude_not_found", "the `claude` command is not installed or not on PATH "
                              f"(PATH searched: {env.get('PATH', '')[:1500]})")
        ref = ctx.session_ref or self.new_ref()
        flags = ctx.extra.get("base_flags", BASE_FLAGS)
        argv = [claude, *spec_args, *flags, "--permission-mode",
                modes.flag_for(ctx.mode), "--model", TIER_ALIAS.get(ctx.model, ctx.model)]
        if ctx.agent:
            argv += ["--agent", ctx.agent]
        if ctx.instructions:                         # Hokma's own text; `--setting-sources user` never loads the folder's
            from hokma.core.config import claude_config_dir
            from hokma.kit import instructions

            try:
                argv += ["--append-system-prompt-file",
                         instructions.write_file(ctx.instructions, claude_config_dir() / "instructions")]
            except OSError as exc:
                raise EngineError("instructions", f"cannot write the instructions file: {exc}") from exc
        if not ctx.extra.get("no_session_flags"):
            argv += ["--resume", ref] if ctx.resume else ["--session-id", ref]
        try:
            proc = await ClaudeProcess.spawn(argv, env, ctx.cwd)
        except OSError as exc:
            raise EngineError("claude_not_found", str(exc)) from exc
        return ClaudeCodeSession(proc, ref, partial="--include-partial-messages" in flags)

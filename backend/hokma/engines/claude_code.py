"""Claude Code as an engine: `claude -p` over stream-json. This is what `engine/session.py` did on its own before
0.0.9, moved behind the contract without a change in behaviour. Hooks stay active (guard and hardblock)."""
from __future__ import annotations

import uuid
from typing import Any, AsyncIterator

from hokma.engine import stream
from hokma.engine.process import ClaudeProcess, find_claude
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

    @property
    def alive(self) -> bool:
        return self._proc.alive

    @property
    def pid(self) -> int:
        return self._proc._proc.pid

    async def send(self, blocks: list[dict[str, Any]]) -> AsyncIterator[EngineEvent]:
        proc = self._proc
        text_seen = False
        try:
            await proc.write(stream.user_message(blocks))
            while True:
                msg = await proc.read()
                if msg is None:
                    break
                kind = msg["type"]
                if kind == "system" and msg.get("subtype") == "init":
                    yield {"type": "session", "ref": self.session_ref or ""}
                elif kind == "stream_event":
                    if stream.is_message_start(msg) and text_seen:
                        yield {"type": "message_break"}
                    text = stream.text_delta(msg)
                    if text:
                        text_seen = True
                        yield {"type": "text", "text": text}
                elif kind == "assistant":
                    if not self._partial:
                        whole = stream.assistant_text(msg)
                        if whole:
                            if text_seen:
                                yield {"type": "message_break"}
                            text_seen = True
                            yield {"type": "text", "text": whole}
                    for block in stream.tool_uses(msg):
                        yield {"type": "tool_use", "id": block.get("id", ""), "name": block.get("name", ""),
                               "input": block.get("input") or {}}
                elif kind == "user":
                    for res in stream.tool_results(msg):
                        yield {"type": "tool_result", "id": res.get("tool_use_id", ""),
                               "is_error": bool(res.get("is_error")), "text": str(res.get("text") or "")}
                elif kind == "control_request":
                    request = msg.get("request") or {}
                    request_id = str(msg.get("request_id", ""))
                    if request.get("subtype") != "can_use_tool":
                        await proc.write(stream.control_error(request_id, "unsupported request"))
                        continue
                    yield {"type": "permission_request", "request_id": request_id,
                           "id": str(request.get("tool_use_id", "")), "tool": str(request.get("tool_name", "")),
                           "input": request.get("input") or {}}
                elif kind == "control_cancel_request":
                    yield {"type": "permission_request", "request_id": str(msg.get("request_id", "")), "tool": "",
                           "input": {"cancelled": True}}
                elif kind == "result":
                    if stream.is_aborted(msg) or self._interrupted:
                        yield {"type": "done", "status": "stopped"}
                    elif msg.get("is_error"):
                        yield {"type": "error", "code": "engine_error",
                               "message": str(msg.get("result") or msg.get("subtype"))}
                    else:
                        yield {"type": "done", "status": "complete", "usage": stream.usage_of(msg),
                               "result": "" if text_seen else str(msg.get("result") or "")}
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
            raise EngineError("integrity", f"{type(exc).__name__}: {exc}") from exc
        env = filtered_env()                         # allow list: no provider keys, no Hokma token, no market keys
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
            raise EngineError("claude_not_found", "the `claude` command is not installed or not on PATH")
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

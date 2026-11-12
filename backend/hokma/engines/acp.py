"""Gemini CLI (free with a Google account, or an API key) through the open Agent Client Protocol: JSON-RPC over stdio.

The CLI runs its own tools and asks Hokma for permission on each one (`session/request_permission`). The broker
answers first; in "auto" mode Hokma then accepts by itself, in "ask" mode the owner is asked. Hokma never starts the
CLI in a no-approvals mode.

Verified against gemini-cli 0.67.0 (--acp flag, GEMINI_CLI_HOME, session/* and authenticate names); login not exercised.
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

from hokma.core.config import hokma_data_dir, hokma_home
from hokma.engines import secrets
from hokma.engine.process import find_cli
from hokma.engines.base import EngineCaps, EngineContext, EngineError, EngineEvent, user_text
from hokma.engines.codex import RpcProcess
from hokma.engines.envfilter import filtered_env
from hokma.permissions import modes
from hokma.permissions.broker import BrokerCtx, check

PROTOCOL_VERSION = 1


def acp_home_dir() -> Path:
    return hokma_data_dir() / "acp-home"


_PATH_KEYS = {"absolutepath", "filepath", "path", "dirpath"}
_DEST_KEYS = {"destination", "newpath", "to", "movepath", "dest", "targetpath", "renameto"}
_CWD_KEYS = {"cwd", "dir", "directory", "workdir", "workingdirectory", "workingdir"}
_NEEDS_PATH = ("read", "edit", "search", "delete", "move", "rename")
INVALID = [("acp_invalid", {})]            # an unknown tool name: the broker refuses it


class _Bad(Exception):
    pass


def _norm(raw: dict[str, Any]) -> dict[str, Any]:
    """Keys compared without case and without underscores or hyphens. Two keys that collapse to the same name are
    refused: which one counts would be a guess."""
    out: dict[str, Any] = {}
    for k, v in raw.items():
        key = str(k).lower().replace("_", "").replace("-", "")
        if key in out:
            raise _Bad()
        out[key] = v
    return out


def _strings(raw: dict[str, Any], keys: set[str]) -> list[str]:
    """The string values of the named keys. A key present with a value that is not a string is refused."""
    out = []
    for k, v in raw.items():
        if k in keys and v not in ("",):
            if not isinstance(v, str):
                raise _Bad()
            out.append(v)
    return out


def _locations(call: dict[str, Any], raw: dict[str, Any]) -> list[str]:
    out = []
    for loc in list(call.get("locations") or []) + list(raw.get("locations") or []):
        if not isinstance(loc, dict) or not isinstance(loc.get("path"), str):
            raise _Bad()
        if loc["path"]:
            out.append(loc["path"])
    return out


def paths_of(call: dict[str, Any]) -> list[str]:
    """Every path the call names: the path fields of its input, a `paths` list and its `locations`. Raises on a shape
    it cannot read."""
    raw = _norm(call.get("rawInput") if isinstance(call.get("rawInput"), dict) else {})
    found = _strings(raw, _PATH_KEYS)
    if "paths" in raw:
        if not isinstance(raw["paths"], list) or not all(isinstance(x, str) for x in raw["paths"]):
            raise _Bad()
        found += [x for x in raw["paths"] if x]
    found += _locations(call, raw)
    return list(dict.fromkeys(found))


def to_requests(kind: str, call: dict[str, Any], cwd: str | None = None) -> list[tuple[str, dict[str, Any]]]:
    """The tool call the CLI wants to make, as the requests the protection rules understand (all must pass).
    Closed by default: a shape that cannot be read, a read, edit, search or delete without a path, a fetch without a
    URL, a move without a destination, an execute in another folder and unknown kinds are all refused. Delete and move
    remove the source (the delete rule); every destination and every location is checked as a write."""
    try:
        raw = _norm(call.get("rawInput") if isinstance(call.get("rawInput"), dict) else {})
        paths = paths_of(call)
        dests = _strings(raw, _DEST_KEYS)
        locs = _locations(call, raw)
        urls = _strings(raw, {"url"})
        folders = _strings(raw, _CWD_KEYS)
    except _Bad:
        return INVALID
    if kind in _NEEDS_PATH and not paths:
        return INVALID
    if kind in ("move", "rename") and not dests:
        return INVALID
    if kind == "fetch":
        return [("web_fetch", {"url": urls[0]})] if urls else INVALID
    if kind == "execute":
        if cwd and any(os.path.realpath(os.path.join(cwd, f)) != os.path.realpath(cwd) for f in folders):
            return INVALID
        return [("Bash", {"command": str(raw.get("command") or call.get("title") or "")})]
    writes = [("Write", {"file_path": x}) for x in dict.fromkeys(dests + locs)]
    if kind == "edit":
        content = {"content": raw["content"]} if isinstance(raw.get("content"), str) else {}
        return [("Write", {"file_path": x, **content}) for x in paths] + writes
    if kind in ("delete", "move", "rename"):
        return [("Bash", {"command": f"rm {shlex.quote(x)}"}) for x in paths] + writes
    if kind == "read":
        return [("Read", {"file_path": x}) for x in paths]
    if kind == "search":
        pattern = str(raw.get("pattern") or raw.get("query") or ".")
        return [("Grep", {"pattern": pattern, "path": x}) for x in paths]
    return [(f"acp_{kind}", {})]


def _acp_mcp(ctx: EngineContext) -> list[dict[str, Any]]:
    from hokma.engines.hokma_mcp import server_spec

    spec = server_spec(ctx)
    return [{"name": "hokma", "command": spec["command"], "args": spec["args"],
             "env": [{"name": k, "value": v} for k, v in spec["env"].items()]}]


class AcpSession:
    def __init__(self, proc: RpcProcess, ctx: EngineContext, bctx: BrokerCtx) -> None:
        self._proc, self.ctx, self.bctx = proc, ctx, bctx
        self.session_ref: str | None = None
        self._next_id = 0
        self._ready = False
        self._answers: dict[str, asyncio.Future] = {}
        self._calls: dict[str, dict[str, Any]] = {}
        self._interrupted = False
        self._prompt_id: int | None = None

    @property
    def alive(self) -> bool:
        return self._proc.alive

    @property
    def pid(self) -> int:
        return self._proc._proc.pid

    async def _request(self, method: str, params: dict[str, Any]) -> int:
        self._next_id += 1
        await self._proc.write({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params})
        return self._next_id

    async def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        rid = await self._request(method, params)
        while True:
            msg = await self._proc.read()
            if msg is None:
                raise EngineError("engine_exited", f"gemini exited: {self._proc.stderr_tail()}")
            if msg.get("id") == rid and "method" not in msg:
                if "error" in msg:
                    raise EngineError("auth" if "auth" in json.dumps(msg["error"]).lower() else "engine_error",
                                      json.dumps(msg["error"])[:500])
                return msg.get("result") or {}

    async def _start(self) -> None:
        init = await self._call("initialize", {"protocolVersion": PROTOCOL_VERSION, "clientCapabilities": {}})
        methods = [m.get("id") for m in init.get("authMethods") or [] if isinstance(m, dict)]
        wanted = "gemini-api-key" if (self.ctx.connection or {}).get("auth_kind") == "api_key" else "oauth-personal"
        if wanted in methods:
            await self._call("authenticate", {"methodId": wanted})
        servers = self.ctx.extra.get("acp_mcp") or _acp_mcp(self.ctx)
        if "acp_mcp" not in self.ctx.extra and ((init.get("agentCapabilities") or {}).get("mcpCapabilities") or {}).get("http"):
            from hokma.engines.remote_mcp import acp_servers

            servers = servers + await asyncio.to_thread(acp_servers)   # remote servers, token in a header over stdin
        result = await self._call("session/new", {"cwd": self.ctx.cwd, "mcpServers": servers})
        self.session_ref = str(result.get("sessionId") or "")
        self._ready = True

    async def send(self, blocks: list[dict[str, Any]]) -> AsyncIterator[EngineEvent]:
        text = user_text(blocks)
        if self.ctx.instructions and not self._ready:
            text = f"{self.ctx.instructions}\n\n{text}"                       # first block of the session
        if self.ctx.history and not self.ctx.resume:
            earlier = "\n".join(f"{h['role']}: {h['text']}" for h in self.ctx.history)
            text = f"Earlier in this conversation:\n{earlier}\n\n{text}"
        try:
            if not self._ready:
                await self._start()
            yield {"type": "session", "ref": self.session_ref or ""}
            self._prompt_id = await self._request("session/prompt", {"sessionId": self.session_ref,
                                                                      "prompt": [{"type": "text", "text": text}]})
        except EngineError as exc:
            yield {"type": "error", "code": exc.code, "message": exc.message}
            return
        except OSError as exc:
            yield {"type": "error", "code": "engine_exited", "message": f"{type(exc).__name__}: {exc}"}
            return
        while True:
            msg = await self._proc.read()
            if msg is None:
                if self._interrupted:
                    yield {"type": "done", "status": "stopped"}
                else:
                    yield {"type": "error", "code": "engine_exited",
                           "message": f"gemini exited: {self._proc.stderr_tail()}".strip()}
                return
            method, params = str(msg.get("method") or ""), msg.get("params") or {}
            if method == "session/update":
                for ev in self._update(params.get("update") or {}):
                    yield ev
            elif method and "id" in msg:
                async for ev in self._on_request(msg["id"], method, params):
                    yield ev
            elif msg.get("id") == self._prompt_id:
                if "error" in msg:
                    yield {"type": "error", "code": "auth" if "auth" in json.dumps(msg["error"]).lower() else "engine_error",
                           "message": json.dumps(msg["error"])[:500]}
                elif self._interrupted or (msg.get("result") or {}).get("stopReason") == "cancelled":
                    yield {"type": "done", "status": "stopped"}
                else:
                    yield {"type": "done", "status": "complete",
                           "usage": {"tokens_in": 0, "tokens_out": 0, "cache_read": 0, "cost_usd": 0.0}}
                return

    def _update(self, update: dict[str, Any]) -> list[EngineEvent]:
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            return [{"type": "text", "text": str(content.get("text") or "")}] if content.get("text") else []
        if kind == "tool_call":
            cid = str(update.get("toolCallId") or "")
            self._calls[cid] = update
            raw = update.get("rawInput") if isinstance(update.get("rawInput"), dict) else {}
            return [{"type": "tool_use", "id": cid, "name": to_requests(str(update.get("kind") or ""), update)[0][0],
                     "input": raw or {"title": update.get("title", "")}}]
        if kind == "tool_call_update" and update.get("status") in ("completed", "failed"):
            parts = [str((c.get("content") or {}).get("text") or "") for c in update.get("content") or []
                     if isinstance(c, dict)]
            return [{"type": "tool_result", "id": str(update.get("toolCallId") or ""),
                     "is_error": update.get("status") == "failed", "text": "\n".join(p for p in parts if p)}]
        return []

    async def _on_request(self, rid: Any, method: str, params: dict[str, Any]) -> AsyncIterator[EngineEvent]:
        if method != "session/request_permission":
            await self._proc.write({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "unsupported"}})
            return
        call = params.get("toolCall") or {}
        known = self._calls.get(str(call.get("toolCallId") or ""), {})
        merged = {**known, **{k: v for k, v in call.items() if v is not None}}
        options = params.get("options") or []

        def pick(*kinds: str) -> str:
            for o in options:
                if o.get("kind") in kinds:
                    return str(o.get("optionId"))
            return ""

        async def reply(allow: bool) -> None:
            option = pick("allow_once") if allow else pick("reject_once", "reject_always")
            outcome = {"outcome": "selected", "optionId": option} if option else {"outcome": "cancelled"}
            await self._proc.write({"jsonrpc": "2.0", "id": rid, "result": {"outcome": outcome}})

        requests = to_requests(str(merged.get("kind") or ""), merged, self.ctx.cwd)
        name, request = requests[0]
        for req_name, req_input in requests:
            if not check(req_name, req_input, cwd=self.ctx.cwd, ctx=self.bctx).allowed:
                await reply(False)
                return
        if self.ctx.mode != "auto":
            request_id = uuid.uuid4().hex[:12]
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._answers[request_id] = fut
            yield {"type": "permission_request", "request_id": request_id, "id": str(merged.get("toolCallId") or ""),
                   "tool": name, "input": request}
            allow, _ = await fut
            self._answers.pop(request_id, None)
            await reply(allow)
            return
        await reply(True)

    async def answer_permission(self, request_id: str, allow: bool, reason: str, tool_input: Any = None) -> None:
        fut = self._answers.get(request_id)
        if fut is not None and not fut.done():
            fut.set_result((allow, reason))

    async def interrupt(self) -> None:
        self._interrupted = True
        for fut in list(self._answers.values()):
            if not fut.done():
                fut.set_result((False, "The owner stopped the turn."))
        if self._proc.alive and self.session_ref:
            try:
                await self._proc.write({"jsonrpc": "2.0", "method": "session/cancel",
                                        "params": {"sessionId": self.session_ref}})
            except OSError:
                pass

    async def close(self) -> None:
        await self._proc.terminate()


class AcpAdapter:
    kind = "gemini_cli"
    binary_name = "gemini"
    args = ["--acp"]
    config_var = "GEMINI_CLI_HOME"             # verified in gemini-cli 0.67.0
    key_var = "GEMINI_API_KEY"

    def __init__(self, owner: Any = None) -> None:
        self.owner = owner

    def caps(self) -> EngineCaps:
        return {"hooks": False, "os_sandbox": False, "native_subagents": False, "resume": False, "images": False,
                "mcp": True}

    def new_ref(self) -> str | None:
        return None

    async def open(self, ctx: EngineContext) -> AcpSession:
        binary = getattr(self.owner, "gemini_bin", None) or find_cli(self.binary_name)
        if not binary:
            raise EngineError("gemini_not_found", "the `gemini` command is not installed or not on PATH")
        home = acp_home_dir()
        home.mkdir(parents=True, exist_ok=True)
        env = filtered_env()
        env[self.config_var] = str(home)
        env.update(modes.work_env(ctx.cwd, ctx.conversation_id, ctx.task_id, ctx.agent or ""))
        conn = ctx.connection or {}
        if conn.get("auth_kind") == "api_key" and conn.get("id") is not None:
            key = secrets.get(conn["id"])
            if key:
                env[self.key_var] = key
        if ctx.model:
            env["GEMINI_MODEL"] = ctx.model
        bctx = ctx.extra.get("broker") or BrokerCtx(
            data=str(hokma_data_dir()), roots=[str(hokma_home()), ctx.cwd],
            protect=[str(p) for p in _protected()], conversation_id=ctx.conversation_id, task_id=ctx.task_id,
            shell="PowerShell" if sys.platform == "win32" else "Bash")
        try:
            proc = await RpcProcess.spawn([binary, *self.args], env, ctx.cwd)
        except OSError as exc:
            raise EngineError("gemini_not_found", str(exc)) from exc
        return AcpSession(proc, ctx, bctx)


def _protected() -> list[Path]:
    from hokma.kit.settings import protected_paths

    return protected_paths()

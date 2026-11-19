"""Codex (ChatGPT account or OpenAI key) through `codex app-server`: JSON-RPC over stdio, one JSON object per line.

The CLI runs its own tools. Hokma answers every approval the CLI asks for, so the broker sees every command and every
file change. The Hokma "auto" mode never becomes the CLI's no-approvals policy: the CLI keeps asking and Hokma
answers by itself (after the broker). `config.toml` and `AGENTS.md` are regenerated at every start inside a folder
the agent cannot write.

Verified against codex-cli 0.167.0 (generated app-server JSON schema): thread/start, thread/resume, turn/start,
turn/interrupt, item/*/requestApproval, approvalPolicy on-request (untrusted before 0.170), read-only sandbox. Login not exercised.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import sys
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

from hokma.core.proc import spawn_kwargs
from hokma.core.config import codex_home_dir, hokma_data_dir, hokma_home
from hokma.engine.process import ClaudeProcess
from hokma.engines import catalog, diagnose, secrets
from hokma.engine.process import find_cli
from hokma.engines.base import EngineCaps, EngineContext, EngineError, EngineEvent, user_text
from hokma.engines.envfilter import filtered_env
from hokma.permissions import modes
from hokma.permissions.broker import BrokerCtx, check, file_change_payloads

EFFORT = {"fast": "low", "balanced": "medium", "deep": "high"}
CLIENT = {"name": "hokma", "title": "Hokma", "version": "0.0.9"}


_SH = {"bash", "sh", "zsh", "dash", "ksh"}
_PS = {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}


def command_request(command: Any) -> tuple[str, dict[str, Any]]:
    """A command as the CLI reports it (text or an argv list) -> the broker request. An argv list is joined with
    shlex.join; a `bash -c` or `sh -c` wrapper hands the broker the script inside, and a PowerShell -Command wrapper
    hands it the inner command in PowerShell mode."""
    if not isinstance(command, list):
        return "Bash", {"command": str(command)}
    argv = [str(c) for c in command]
    name = os.path.basename(argv[0]).lower() if argv else ""
    if name in _SH and len(argv) == 3 and argv[1] in ("-c", "-lc"):          # exactly [sh, -c|-lc, script]
        return "Bash", {"command": argv[2]}
    if name in _PS and len(argv) == 3 and argv[1].lower() in ("-command", "-c"):   # exactly [ps, -Command, script]
        return "PowerShell", {"command": argv[-1]}
    return "Bash", {"command": shlex.join(argv)}



def scrub_home(text: str) -> str:
    """The user's home folder (any slash style) replaced by "~" in text that goes to the chat or engine.log."""
    home = str(Path.home())
    flags = re.I if sys.platform == "win32" else 0
    for variant in {home, home.replace("\\", "/"), home.replace("/", "\\"), json.dumps(home)[1:-1]}:
        if len(variant) > 1:
            text = re.sub(re.escape(variant), "~", text, flags=flags)
    return text


def _failure(raw: str) -> tuple[str, str]:
    """Bug 1175: Codex's refusal as a code the panel and the chat translate (a Business workspace without Codex,
    device code sign-in off, an expired session, the usage limit), with its real text in engine.log."""
    raw = scrub_home(raw)
    message = raw[:500]
    code = diagnose.classify_codex(raw)
    from hokma.engine import errlog

    errlog.log(f"codex: {code}: {message}", scope="engines")
    return code, message

def _failure_exit(stderr: str) -> tuple[str, str]:
    """The app-server ended (a configuration it refused to load ends it): its stderr is logged and classified; with
    nothing recognisable the code stays engine_exited."""
    text = (stderr or "").strip()
    code, message = _failure(text or "codex exited")
    return ("engine_exited" if code == "engine_error" else code), f"codex exited: {message}".strip()


class RpcProcess(ClaudeProcess):
    """A child process that speaks JSON lines with no `type` field (JSON-RPC)."""

    async def read(self) -> dict[str, Any] | None:
        stdout = self._proc.stdout
        while stdout is not None:
            raw = await stdout.readline()
            if not raw:
                return None
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if isinstance(msg, dict):
                return msg
        return None


_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


def toml_key(k: str) -> str:
    """A TOML key: bare when it is only [A-Za-z0-9_-], else a quoted string (ProgramFiles(x86) on Windows)."""
    k = str(k)
    return k if _BARE_KEY.fullmatch(k) else json.dumps(k)


# Codex 0.170 removed approval_policy "untrusted" (openai docs, agent-approvals-security: "Migrate from the retired
# untrusted approval policy"). on-request lets every command the sandbox allows run WITHOUT asking, so it is not
# equivalent. The documented way to keep the stricter rule is to OMIT approval_policy and mark the project
# `trust_level = "untrusted"` in config.toml: commands then require approval (requestApproval, which Hokma's broker
# answers) unless an execpolicy rule allows them (that is the old safe list). approval_policy "granular" only filters
# which prompts surface, it does not make every command ask. The sandbox stays read-only as a second layer, so a
# write or edit that escapes the untrusted rule still needs an escalation.
LEGACY_BEFORE = (0, 170)


def parse_version(text: str) -> tuple[int, ...] | None:
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    return tuple(int(g) for g in m.groups() if g is not None) if m else None


def policy_for(version: tuple[int, ...] | None) -> tuple[str | None, str]:
    """(approval_policy, sandbox); approval_policy None = not written, the untrusted project decides. An unknown
    version gets the current (strict) values."""
    if version is not None and version[:2] < LEGACY_BEFORE:
        return "untrusted", "workspace-write"
    return None, "read-only"


async def codex_version(binary: str) -> tuple[int, ...] | None:
    """`codex --version`, or None when it cannot be read."""
    try:
        proc = await asyncio.create_subprocess_exec(binary, "--version", stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT, **spawn_kwargs())
        out, _ = await asyncio.wait_for(proc.communicate(), 10)
    except (OSError, asyncio.TimeoutError):
        return None
    return parse_version(out.decode("utf-8", "replace"))


def write_config(ctx: EngineContext, mcp: dict[str, Any] | None = None,
                 version: tuple[int, ...] | None = None) -> Path:
    """Rebuild config.toml and AGENTS.md from the package template. Never a no-approvals policy."""
    home = codex_home_dir()
    home.mkdir(parents=True, exist_ok=True)
    approval, sandbox = policy_for(version)
    lines = ([f'approval_policy = "{approval}"'] if approval else []) + [
        f'sandbox_mode = "{sandbox}"', f'model_reasoning_effort = "{EFFORT.get(ctx.tier, "medium")}"', ""]
    cwd = getattr(ctx, "cwd", None)
    if not approval and cwd:            # every command asks (requestApproval) unless an execpolicy rule allows it
        lines += [f"[projects.{toml_key(cwd)}]", 'trust_level = "untrusted"', ""]
    lines += ["[shell_environment_policy]", 'exclude = ["HOKMA_MCP_TOKEN_*"]', ""]   # remote MCP tokens stay in Codex
    for name, spec in (mcp or {}).items():
        lines.append(f"[mcp_servers.{toml_key(name)}]")
        if spec.get("url"):                  # a remote server; the token is in the Codex environment, not in this file
            lines.append(f"url = {json.dumps(spec['url'])}")
            if spec.get("bearer_token_env_var"):
                lines.append(f"bearer_token_env_var = {json.dumps(spec['bearer_token_env_var'])}")
            lines.append("")
            continue
        lines.append(f"command = {json.dumps(spec['command'])}")
        lines.append("args = " + json.dumps(spec.get("args", [])))
        if spec.get("env"):
            lines.append(f"[mcp_servers.{toml_key(name)}.env]")
            lines += [f"{toml_key(k)} = {json.dumps(v)}" for k, v in spec["env"].items()]
        lines.append("")
    (home / "config.toml").write_text("\n".join(lines), encoding="utf-8")
    (home / "AGENTS.md").write_text(ctx.instructions or "", encoding="utf-8")
    return home


class CodexSession:
    def __init__(self, proc: RpcProcess, ctx: EngineContext, bctx: BrokerCtx,
                 version: tuple[int, ...] | None = None) -> None:
        self._proc, self.ctx, self.bctx = proc, ctx, bctx
        self._policy = policy_for(version)
        self.session_ref: str | None = ctx.session_ref
        self._next_id = 0
        self._ready = False
        self._thread: str | None = ctx.session_ref
        self._turn: str | None = None
        self._answers: dict[str, asyncio.Future] = {}
        self._items: dict[str, dict[str, Any]] = {}
        self._interrupted = False

    @property
    def alive(self) -> bool:
        return self._proc.alive

    @property
    def pid(self) -> int:
        return self._proc._proc.pid

    # ------------------------------------------------------------------ rpc

    async def _request(self, method: str, params: dict[str, Any]) -> int:
        self._next_id += 1
        await self._proc.write({"id": self._next_id, "method": method, "params": params})
        return self._next_id

    async def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a request and wait for its response, ignoring notifications (only used before a turn)."""
        rid = await self._request(method, params)
        while True:
            msg = await self._proc.read()
            if msg is None:
                raise EngineError(*_failure_exit(self._proc.stderr_tail()))
            if msg.get("id") == rid and "method" not in msg:
                if "error" in msg:
                    raise EngineError(*_failure(json.dumps(msg["error"])))
                return msg.get("result") or {}

    async def _start(self) -> None:
        await self._call("initialize", {"clientInfo": CLIENT})
        await self._proc.write({"method": "initialized", "params": {}})
        base = {"cwd": self.ctx.cwd, "sandbox": self._policy[1]}
        if self._policy[0]:
            base["approvalPolicy"] = self._policy[0]
        if self.ctx.model:
            base["model"] = self.ctx.model
        if self._thread and self.ctx.resume:
            result = await self._call("thread/resume", {"threadId": self._thread, **base})
        else:
            result = await self._call("thread/start", base)
        thread = result.get("thread") or {}
        self._thread = str(thread.get("id") or result.get("threadId") or self._thread or "")
        self.session_ref = self._thread
        self._ready = True

    # ----------------------------------------------------------------- turn

    async def send(self, blocks: list[dict[str, Any]]) -> AsyncIterator[EngineEvent]:
        text = user_text(blocks)
        if not self.ctx.resume and self.ctx.history:
            earlier = "\n".join(f"{h['role']}: {h['text']}" for h in self.ctx.history)
            text = f"Earlier in this conversation:\n{earlier}\n\nNow:\n{text}"
        try:
            if not self._ready:
                await self._start()
            yield {"type": "session", "ref": self.session_ref or ""}
            params: dict[str, Any] = {"threadId": self._thread, "input": [{"type": "text", "text": text}]}
            if self.ctx.model:
                params["model"] = self.ctx.model
            params["effort"] = EFFORT.get(self.ctx.tier, "medium")
            await self._request("turn/start", params)
        except EngineError as exc:
            yield {"type": "error", "code": exc.code, "message": exc.message}
            return
        except OSError as exc:
            yield {"type": "error", "code": "engine_exited", "message": scrub_home(f"{type(exc).__name__}: {exc}")}
            return
        usage = {"tokens_in": 0, "tokens_out": 0, "cache_read": 0, "cost_usd": 0.0}
        text_seen = False
        while True:
            try:
                msg = await self._proc.read()
            except OSError as exc:
                yield {"type": "error", "code": "engine_exited", "message": scrub_home(str(exc))}
                return
            if msg is None:
                if self._interrupted:
                    yield {"type": "done", "status": "stopped"}
                else:
                    code, message = _failure_exit(self._proc.stderr_tail())
                    yield {"type": "error", "code": code, "message": message}
                return
            method = str(msg.get("method") or "")
            params = msg.get("params") or {}
            if "id" in msg and method:                                   # a request from the CLI to us
                async for ev in self._on_request(msg["id"], method, params):
                    yield ev
            elif "id" in msg and "error" in msg:                         # the answer to turn/start failed
                code, message = _failure(json.dumps(msg["error"]))
                yield {"type": "error", "code": code, "message": message}
                return
            elif method.endswith("agentMessage/delta"):
                delta = str(params.get("delta") or "")
                if delta:
                    text_seen = True
                    yield {"type": "text", "text": delta}
            elif method.endswith("item/started"):
                ev = self._tool_use(params.get("item") or {})
                if ev:
                    yield ev
            elif method.endswith("item/completed"):
                item = params.get("item") or {}
                if item.get("type") == "agentMessage" and not text_seen and item.get("text"):
                    text_seen = True
                    yield {"type": "text", "text": str(item["text"])}
                ev = self._tool_result(item)
                if ev:
                    yield ev
            elif method.endswith("tokenUsage/updated"):
                total = (params.get("tokenUsage") or {}).get("total") or {}
                usage = {"tokens_in": int(total.get("inputTokens") or 0), "tokens_out": int(total.get("outputTokens") or 0),
                         "cache_read": int(total.get("cachedInputTokens") or 0), "cost_usd": 0.0}
            elif method.endswith("turn/completed"):
                turn = params.get("turn") or {}
                status = str(turn.get("status") or "completed")
                if status == "interrupted" or self._interrupted:
                    yield {"type": "done", "status": "stopped"}
                elif status in ("failed", "error"):
                    err = json.dumps(turn.get("error") or params.get("error") or status)
                    code, message = _failure(err)
                    yield {"type": "error", "code": code, "message": message}
                else:
                    yield {"type": "done", "status": "complete", "usage": usage}
                return
            elif method == "error" or method.endswith("/error"):
                if not params.get("willRetry"):
                    err = json.dumps(params.get("error") or params)[:500]
                    code, message = _failure(err)
                    yield {"type": "error", "code": code, "message": message}
                    return

    # ---------------------------------------------------------------- items

    def _tool_use(self, item: dict[str, Any]) -> EngineEvent | None:
        kind, iid = item.get("type"), str(item.get("id") or "")
        self._items[iid] = item
        if kind == "commandExecution":
            return {"type": "tool_use", "id": iid, "name": "Bash", "input": {"command": item.get("command", "")}}
        if kind == "fileChange":
            return {"type": "tool_use", "id": iid, "name": "Edit",
                    "input": {"file_path": ", ".join(str(c.get("path", "")) for c in item.get("changes") or [])[:400]}}
        if kind == "mcpToolCall":
            return {"type": "tool_use", "id": iid, "name": f"mcp__{item.get('server', '')}__{item.get('tool', '')}",
                    "input": item.get("arguments") or {}}
        return None

    @staticmethod
    def _tool_result(item: dict[str, Any]) -> EngineEvent | None:
        kind, iid = item.get("type"), str(item.get("id") or "")
        if kind == "commandExecution":
            code = item.get("exitCode")
            return {"type": "tool_result", "id": iid, "is_error": bool(code) or item.get("status") == "declined",
                    "text": str(item.get("aggregatedOutput") or "")}
        if kind == "fileChange":
            return {"type": "tool_result", "id": iid, "is_error": item.get("status") in ("declined", "failed"),
                    "text": str(item.get("status") or "")}
        if kind == "mcpToolCall":
            return {"type": "tool_result", "id": iid, "is_error": item.get("status") == "failed",
                    "text": json.dumps(item.get("result") or item.get("error") or "")[:4000]}
        return None

    # ------------------------------------------------------------ approvals

    async def _on_request(self, rid: Any, method: str, params: dict[str, Any]) -> AsyncIterator[EngineEvent]:
        """The CLI asks to run a command or change files. Broker first, then the owner (in ask mode)."""
        if method not in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            await self._proc.write({"id": rid, "error": {"code": -32601, "message": "unsupported request"}})
            return
        item = self._items.get(str(params.get("itemId") or ""), {})
        requests: list[tuple[str, dict[str, Any]]]
        if "commandExecution" in method:
            command = params.get("command") or item.get("command") or ""
            requests = [command_request(command)]
            tool, shown = "Bash", {"command": shlex.join(command) if isinstance(command, list) else str(command)}
        else:
            requests = file_change_payloads(list(item.get("changes") or []))
            if not requests:                                               # nothing we can read = nothing we allow
                requests = [("unknown_change_kind", {})]
            tool, shown = "Edit", {"file_path": ", ".join(str(c.get("path", "")) for c in item.get("changes") or [])}
        for name, request in requests:
            if not check(name, request, cwd=self.ctx.cwd, ctx=self.bctx).allowed:
                await self._proc.write({"id": rid, "result": {"decision": "decline"}})
                return
        if self.ctx.mode != "auto":
            request_id = uuid.uuid4().hex[:12]
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._answers[request_id] = fut
            yield {"type": "permission_request", "request_id": request_id, "id": str(item.get("id") or ""),
                   "tool": tool, "input": shown}
            allow, _ = await fut
            self._answers.pop(request_id, None)
            await self._proc.write({"id": rid, "result": {"decision": "accept" if allow else "decline"}})
            return
        await self._proc.write({"id": rid, "result": {"decision": "accept"}})

    async def answer_permission(self, request_id: str, allow: bool, reason: str, tool_input: Any = None) -> None:
        fut = self._answers.get(request_id)
        if fut is not None and not fut.done():
            fut.set_result((allow, reason))

    async def interrupt(self) -> None:
        self._interrupted = True
        for fut in list(self._answers.values()):
            if not fut.done():
                fut.set_result((False, "The owner stopped the turn."))
        if self._proc.alive and self._thread:
            try:
                await self._request("turn/interrupt", {"threadId": self._thread})
            except OSError:
                pass

    async def close(self) -> None:
        await self._proc.terminate()


class CodexAdapter:
    kind = "codex"

    def __init__(self, owner: Any = None) -> None:
        self.owner = owner

    def caps(self) -> EngineCaps:
        return {"hooks": False, "os_sandbox": sys.platform != "win32", "native_subagents": False, "resume": True,
                "images": False, "mcp": True}

    def new_ref(self) -> str | None:
        return None

    async def open(self, ctx: EngineContext) -> CodexSession:
        binary = getattr(self.owner, "codex_bin", None) or find_cli("codex")
        if not binary:
            raise EngineError("codex_not_found", "the `codex` command is not installed or not on PATH")
        from hokma.engines.hokma_mcp import server_spec
        from hokma.engines.remote_mcp import codex_servers

        remote, remote_env = ({}, {}) if ctx.extra.get("mcp_servers") else await asyncio.to_thread(codex_servers)
        version = await codex_version(binary)
        write_config(ctx, ctx.extra.get("mcp_servers") or {**remote, "hokma": server_spec(ctx)}, version)
        env = filtered_env()
        env.update(remote_env)
        env["CODEX_HOME"] = str(codex_home_dir())
        env.update(modes.work_env(ctx.cwd, ctx.conversation_id, ctx.task_id, ctx.agent or ""))
        conn = ctx.connection or {}
        if conn.get("auth_kind") == "api_key" and conn.get("id") is not None:
            key = secrets.get(conn["id"])
            if key:
                env["OPENAI_API_KEY"] = key               # the only way the CLI accepts a key at run time
        bctx = ctx.extra.get("broker") or BrokerCtx(
            data=str(hokma_data_dir()), roots=[str(hokma_home()), ctx.cwd],
            protect=[str(p) for p in _protected()], conversation_id=ctx.conversation_id, task_id=ctx.task_id,
            shell="PowerShell" if sys.platform == "win32" else "Bash")
        try:
            proc = await RpcProcess.spawn([binary, "app-server"], env, ctx.cwd)
        except OSError as exc:
            raise EngineError("codex_not_found", str(exc)) from exc
        return CodexSession(proc, ctx, bctx, version)


def _protected() -> list[Path]:
    from hokma.kit.settings import protected_paths

    return protected_paths()


_ = (catalog, os)

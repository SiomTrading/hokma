"""Hokma's own MCP server (`kit/trash_mcp.py`) for the engines that do not read Claude Code's `--mcp-config`.

* Direct API engine: a small stdio MCP client (standard library) starts the same server and offers its tools to the
  model under their plain names (`task_list`, `journal_write`...).
* Codex and ACP engines: `server_spec` gives the command, arguments and an explicit environment for their config.

The caller identity (agent, task, conversation) goes in the environment of the server process, set here by Hokma.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from typing import Any

from hokma.core.config import hokma_data_dir, hokma_home
from hokma.core.proc import spawn_kwargs
from hokma.engines.base import EngineContext
from hokma.engines.envfilter import filtered_env
from hokma.kit.launch import TRASH_MCP
from hokma.permissions import modes

PREFIX = "mcp__hokma__"
TIMEOUT = 60


def server_spec(ctx: EngineContext) -> dict[str, Any]:
    from hokma.kit.settings import protected_paths

    args = ["-I", str(TRASH_MCP), "--data", str(hokma_data_dir()), "--workdir", str(hokma_home())]
    for p in protected_paths():
        args += ["--protect", str(p)]
    env = filtered_env()
    env.update(modes.work_env(ctx.cwd, ctx.conversation_id, ctx.task_id, ctx.agent or ""))
    env["HOKMA_DATA_DIR"] = str(hokma_data_dir())
    return {"command": sys.executable, "args": args, "env": env}


class McpClient:
    """Blocking JSON-RPC over stdio. One request at a time (a lock); call it from a worker thread."""

    def __init__(self, spec: dict[str, Any], cwd: str) -> None:
        self.proc = subprocess.Popen([spec["command"], *spec["args"]], cwd=cwd, env=spec["env"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                     **spawn_kwargs())
        self._lock = threading.Lock()
        self._id = 0
        self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "hokma", "version": "0.0.9"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._id += 1
            self._send({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
            assert self.proc.stdout is not None
            timer = threading.Timer(TIMEOUT, self.close)
            timer.start()
            try:
                while True:
                    line = self.proc.stdout.readline()
                    if not line:
                        raise OSError("the Hokma tool server ended")
                    msg = json.loads(line)
                    if msg.get("id") == self._id:
                        if "error" in msg:
                            raise ValueError(json.dumps(msg["error"])[:300])
                        return msg.get("result") or {}
            finally:
                timer.cancel()

    def close(self) -> None:
        try:
            self.proc.kill()
        except OSError:
            pass


class HokmaToolBox:
    """ToolBox of the direct engine: the tools of the `hokma` server."""

    def __init__(self, ctx: EngineContext) -> None:
        self.client = McpClient(server_spec(ctx), ctx.cwd)
        self._tools = {t["name"]: t for t in self.client.request("tools/list", {}).get("tools", [])}

    def definitions(self) -> list[dict[str, Any]]:
        return [{"name": n, "description": t.get("description", ""), "schema": t.get("inputSchema", {})}
                for n, t in self._tools.items()]

    def owns(self, name: str) -> bool:
        return name in self._tools

    def broker_request(self, name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        return PREFIX + name, args

    def run(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        try:
            result = self.client.request("tools/call", {"name": name, "arguments": args})
        except (OSError, ValueError) as exc:
            return f"{type(exc).__name__}: {exc}", True
        text = "".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict))
        return text, bool(result.get("isError"))

    def close(self) -> None:
        self.client.close()


def hokma_toolbox(ctx: EngineContext):
    """Hokma's tools plus the remote MCP servers the student connected in the panel."""
    from hokma.engines.remote_mcp import CompositeToolBox, remote_toolboxes

    boxes: list = []
    try:
        boxes.append(HokmaToolBox(ctx))
    except Exception:                                # the model still works with its native tools
        pass
    try:
        boxes += remote_toolboxes()
    except Exception:
        pass
    if not boxes:
        return None
    return boxes[0] if len(boxes) == 1 else CompositeToolBox(boxes)

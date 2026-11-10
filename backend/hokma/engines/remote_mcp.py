"""Remote MCP servers the student added in the panel, handed to every engine.

Only servers that are ready go out: open ones (no sign-in) and signed-in ones. A server that still needs a sign-in is
left out, so no engine starts an OAuth flow of its own. A server the panel has not probed yet (approved from an agent
request) goes out as it is, like before 0.0.15; the panel probes it the next time it lists the servers.

How the access token reaches each engine (never in the database, logs or a config file on disk):
* Claude Code: `headersHelper` in --mcp-config. Claude runs `python -I kit/engine_key.py --mcp <name>`, which reads
  the token from the credential store and prints the Authorization header. The file only holds that command. Claude
  re-runs the helper on a 401, and the server's refresher keeps the stored token fresh.
* Codex: `bearer_token_env_var` in config.toml names a variable set only in the Codex process environment; the shell
  policy excludes it from the commands the agent runs. Residual risk, the same as the OpenAI key Codex already gets by
  environment: a command that reads the environment of the Codex process itself (`/proc/<pid>/environ` on Linux)
  could see it. The token is short-lived and limited to the scopes the student accepted.
* Gemini (ACP): HTTP headers in `session/new`, sent over the stdin pipe.
* Direct API: Hokma's own streamable HTTP client (`RemoteToolBox`), which asks for a fresh token on every call.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
from pathlib import Path
from typing import Any

from hokma import __version__
from hokma.kit import extensions

ENGINE_KEY = Path(__file__).resolve().parent.parent / "kit" / "engine_key.py"
TIMEOUT = 60


def ready_servers() -> list[dict[str, Any]]:
    """[{name, url, auth}] for remote servers that are open or signed in, and that answered as an MCP server at this
    address (`verified_url`, task 3152): a web page saved by mistake never reaches an engine. A server nobody has
    probed or confirmed yet (approved from an agent's request, or signed in with 0.0.15) is checked here once."""
    from hokma.status import mcp_auth, mcp_oauth

    data, _ = extensions.load()
    out = []
    for name, cfg in sorted(data["mcpServers"].items()):
        if not isinstance(cfg, dict) or cfg.get("type") != "http" or name == "hokma":
            continue
        mcp_auth.ensure_checked(name, cfg["url"])
        entry = mcp_oauth.load_state().get(name, {})
        if not _same_url(entry.get("url"), cfg["url"]) or not _same_url(entry.get("verified_url"), cfg["url"]):
            continue                      # the address changed or never answered as MCP: the token never follows a name
        if entry.get("auth") is False:
            out.append({"name": name, "url": cfg["url"], "auth": False})
        elif entry.get("auth") and mcp_oauth.has_tokens(name):
            out.append({"name": name, "url": cfg["url"], "auth": True})
    return out


def _same_url(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and a.rstrip("/").lower() == b.rstrip("/").lower()


def token(name: str) -> str | None:
    from hokma.status import mcp_oauth

    return mcp_oauth.access_token(name)


def env_var(name: str) -> str:
    """Unique per exact name (`a-b` and `a_b` do not share a variable)."""
    return ("HOKMA_MCP_TOKEN_" + re.sub(r"[^A-Z0-9]", "_", name.upper()) + "_"
            + hashlib.sha256(name.encode()).hexdigest()[:8].upper())


# ----------------------------------------------------------------- Claude Code

def headers_helper(name: str) -> str:
    return f'"{sys.executable}" -I "{ENGINE_KEY}" --mcp {name}'


def claude_entry(server: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {"type": "http", "url": server["url"]}
    if server["auth"]:
        entry["headersHelper"] = headers_helper(server["name"])
    return entry


# ----------------------------------------------------------------- Codex

def codex_servers() -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """(config entries for write_config, environment for the Codex process)."""
    specs: dict[str, dict[str, Any]] = {}
    env: dict[str, str] = {}
    for server in ready_servers():
        spec: dict[str, Any] = {"url": server["url"]}
        if server["auth"]:
            value = token(server["name"])
            if not value:
                continue
            spec["bearer_token_env_var"] = env_var(server["name"])
            env[spec["bearer_token_env_var"]] = value
        specs[server["name"]] = spec
    return specs, env


# ----------------------------------------------------------------- Gemini (ACP)

def acp_servers() -> list[dict[str, Any]]:
    out = []
    for server in ready_servers():
        headers = []
        if server["auth"]:
            value = token(server["name"])
            if not value:
                continue
            headers.append({"name": "Authorization", "value": f"Bearer {value}"})
        out.append({"type": "http", "name": server["name"], "url": server["url"], "headers": headers})
    return out


# ----------------------------------------------------------------- direct API

class RemoteClient:
    """Streamable HTTP MCP client (JSON or SSE answers), one request at a time."""

    def __init__(self, server: dict[str, Any]) -> None:
        self.name, self.url, self.auth = server["name"], server["url"], server["auth"]
        self.session: str | None = None
        self.version = None
        self._id = 0
        self._lock = threading.Lock()
        result = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                             "clientInfo": {"name": "hokma", "version": __version__}})
        self.version = result.get("protocolVersion") or "2025-06-18"
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.version:
            headers["MCP-Protocol-Version"] = self.version
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        if self.auth:
            value = token(self.name)
            if not value:
                raise OSError(f"{self.name}: not signed in")
            headers["Authorization"] = f"Bearer {value}"
        return headers

    def _post(self, message: dict[str, Any]):
        from hokma.status.mcp_oauth import http

        resp = http("POST", self.url, headers=self._headers(), data=json.dumps(message).encode(), timeout=TIMEOUT)
        if resp.headers.get("mcp-session-id"):
            self.session = resp.headers["mcp-session-id"]
        return resp

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._id += 1
            rid = self._id
            resp = self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            if resp.status >= 300:
                raise OSError(f"{self.name}: HTTP {resp.status}")
            for msg in _messages(resp):
                if msg.get("id") == rid:
                    if "error" in msg:
                        raise ValueError(json.dumps(msg["error"])[:300])
                    return msg.get("result") or {}
            raise OSError(f"{self.name}: no answer")

    def close(self) -> None:
        pass


def _messages(resp) -> list[dict[str, Any]]:
    from hokma.status.mcp_oauth import rpc_messages

    return rpc_messages(resp)


class RemoteToolBox:
    """Tools of one remote server for the direct engine, named `<server>__<tool>`; the broker sees `mcp__<server>__<tool>`."""

    def __init__(self, server: dict[str, Any]) -> None:
        self.client = RemoteClient(server)
        self.prefix = server["name"] + "__"
        tools = self.client.request("tools/list", {}).get("tools", [])
        self._tools = {self.prefix + t["name"]: t for t in tools if isinstance(t, dict) and t.get("name")}

    def definitions(self) -> list[dict[str, Any]]:
        return [{"name": n, "description": t.get("description", ""), "schema": t.get("inputSchema", {})}
                for n, t in self._tools.items()]

    def owns(self, name: str) -> bool:
        return name in self._tools

    def broker_request(self, name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        return "mcp__" + name, args

    def run(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        try:
            result = self.client.request("tools/call", {"name": self._tools[name]["name"], "arguments": args})
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}", True
        text = "".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict))
        return text, bool(result.get("isError"))

    def close(self) -> None:
        self.client.close()


class CompositeToolBox:
    def __init__(self, boxes: list[Any]) -> None:
        self.boxes = boxes

    def _owner(self, name: str):
        return next((b for b in self.boxes if b.owns(name)), None)

    def definitions(self) -> list[dict[str, Any]]:
        return [d for b in self.boxes for d in b.definitions()]

    def owns(self, name: str) -> bool:
        return self._owner(name) is not None

    def broker_request(self, name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        return self._owner(name).broker_request(name, args)

    def run(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        return self._owner(name).run(name, args)

    def close(self) -> None:
        for b in self.boxes:
            if hasattr(b, "close"):
                b.close()


def remote_toolboxes() -> list[RemoteToolBox]:
    boxes = []
    for server in ready_servers():
        try:
            boxes.append(RemoteToolBox(server))
        except Exception:                          # an unreachable server only loses its tools
            continue
    return boxes

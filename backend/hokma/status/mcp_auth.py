"""Remote MCP servers added and connected from the panel, for every engine (Claude Code, Codex, Gemini, direct API).

Since 0.0.15 Hokma signs in by itself (`mcp_oauth`): Connect opens the system browser on the authorization page, the
browser comes back to this server's `/oauth/mcp/callback` and the panel sees the server as connected. No terminal, no
pasted address, no `claude mcp login`. The tokens live only in the system credential store.

Servers registered with `claude mcp add --scope user` by 0.0.10-0.0.14 are removed from Claude's own config when they
are added or connected here, so Claude Code reads them only from Hokma's --mcp-config.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.parse

from hokma.core.config import claude_config_dir, hokma_data_dir
from hokma.core.proc import spawn_kwargs
from hokma.db import Actor
from hokma.i18n import t
from hokma.kit import extensions
from hokma.status import mcp_oauth
from hokma.status.extensions_service import LOCAL_MARKETPLACE, ServiceError, _db, _owner, sync_table

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
RESERVED = ("hokma",)
CMD_TIMEOUT = 10
REFRESH_EVERY = 60


def normalize_url(url: str) -> str:
    """Trim, lowercase scheme and host, drop the fragment and a trailing slash after a path."""
    url = (url or "").strip()
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() != "https" or not parts.hostname or parts.username or parts.password:
        raise ServiceError(400, t("status.mcp.bad_url"))
    netloc = parts.hostname.lower() + (f":{parts.port}" if parts.port and parts.port != 443 else "")
    path = parts.path.rstrip("/") if parts.path not in ("", "/") else ""
    return urllib.parse.urlunsplit(("https", netloc, path, parts.query, ""))


def _server(name: str) -> dict:
    data, _ = extensions.load()
    entry = data["mcpServers"].get(name)
    if not isinstance(entry, dict) or entry.get("type") != "http":
        raise ServiceError(404, t("status.mcp.unknown", name=name))
    return entry


def _forget_claude_registration(name: str) -> None:
    """Best effort: drop a server 0.0.10-0.0.14 registered in Claude's user config, so --mcp-config is the only one."""
    try:
        data = json.loads((claude_config_dir() / ".claude.json").read_text(encoding="utf-8"))
        if name not in (data.get("mcpServers") or {}):
            return
    except (OSError, ValueError, AttributeError):
        return
    from hokma.setup.claude import claude_path

    path = claude_path()
    if path is None:
        return
    try:
        subprocess.run([path, "mcp", "remove", "--scope", "user", name], capture_output=True, timeout=CMD_TIMEOUT,
                       stdin=subprocess.DEVNULL, cwd=str(hokma_data_dir()),
                       env={**os.environ, "CLAUDE_CONFIG_DIR": str(claude_config_dir())},
                       **spawn_kwargs())
    except (OSError, subprocess.SubprocessError):
        pass


# ---------------------------------------------------------------- add

def add_server(name: str, url: str) -> dict:
    name = (name or "").strip()
    if not NAME.match(name) or name in RESERVED:
        raise ServiceError(400, t("status.mcp.bad_name"))
    url = normalize_url(url)
    if len(url) > 1000:
        raise ServiceError(400, t("status.mcp.bad_url"))
    data, _ = extensions.load()
    if name in data["mcpServers"]:
        raise ServiceError(409, t("status.mcp.exists", name=name))
    db = _db()
    with db.write() as unit:
        owner = _owner(unit)
        request = unit.extension_requests.create("install", "mcp", name, LOCAL_MARKETPLACE,
                                                 f"dashboard: add remote MCP {name}", actor=Actor.human(owner))
        unit.extension_requests.approve(request.id, decided_by=owner)
        try:
            warnings = extensions.set_mcp(name, {"type": "http", "url": url})
        except ValueError as exc:
            unit.extension_requests.mark_failed(request.id, str(exc))
            raise ServiceError(400, str(exc)) from exc
        data, _ = extensions.load()
        sync_table(unit, data)
        ext = unit.extensions.find(LOCAL_MARKETPLACE, name)
        unit.extension_requests.mark_applied(request.id, ext.id)
    mcp_oauth.forget(name)                                 # a server added again starts clean, no old token
    _forget_claude_registration(name)
    _probe_unknown(name, url)
    return {**server_state(name), "warnings": warnings}


# ---------------------------------------------------------------- state

def server_state(name: str) -> dict:
    """needs_auth | connected | error. Read from the plain state file and the credential store, no network."""
    entry = _server(name)
    oauth = mcp_oauth.load_state().get(name, {})
    info = {"name": name, "url": entry.get("url"), "state": "needs_auth", "message": "",
            "auth": oauth.get("auth", True), "pending": mcp_oauth.pending(name)}
    if oauth.get("auth") is False:
        info["state"] = "connected"
    elif (oauth.get("url") or "").rstrip("/").lower() != str(entry.get("url") or "").rstrip("/").lower():
        pass                                              # the address changed: the old sign-in does not count
    elif mcp_oauth.has_tokens(name):
        expires = oauth.get("expires_at")
        expired = bool(expires) and expires <= time.time()
        has_refresh = bool(mcp_oauth.secret_get(f"{name}:refresh"))
        info["state"] = "needs_auth" if expired and not has_refresh else "connected"
    if oauth.get("error") and info["state"] != "connected":
        info["state"] = "error"
        info["message"] = str(oauth["error"])[:300]
    return info


def _probe_unknown(name: str, url: str) -> None:
    """A server approved from an agent request has never been probed: find out once whether it needs a sign-in."""
    try:
        meta = mcp_oauth.discover(url)
        mcp_oauth.update_entry(name, url=url, auth=meta["auth"], error=None)
    except mcp_oauth.OAuthError as exc:
        mcp_oauth.update_entry(name, url=url, auth=True, error=exc.message)


def servers_state() -> list[dict]:
    data, _ = extensions.load()
    known = mcp_oauth.load_state()
    for name, entry in data["mcpServers"].items():
        if isinstance(entry, dict) and entry.get("type") == "http" and "auth" not in known.get(name, {}):
            _probe_unknown(name, entry["url"])
    return [server_state(n) for n, e in sorted(data["mcpServers"].items())
            if isinstance(e, dict) and e.get("type") == "http"]


# ---------------------------------------------------------------- connect

def redirect_uri(port: int) -> str:
    return f"http://127.0.0.1:{int(port)}{mcp_oauth.CALLBACK_PATH}"


def start_connect(name: str, port: int) -> dict:
    """Returns {auth_url} to open in the system browser, or {auth_url: None} when the server needs no sign-in."""
    from hokma.connections.store import StoreUnavailable

    entry = _server(name)
    if not _store_ready():
        raise ServiceError(409, t("status.mcp.no_store"))
    _forget_claude_registration(name)
    try:
        url = mcp_oauth.start(name, entry["url"], redirect_uri(port))
    except mcp_oauth.OAuthError as exc:
        key = "status.mcp.no_registration" if exc.code == "no_registration" else "status.mcp.connect_failed"
        message = t(key, output=exc.message)
        mcp_oauth.update_entry(name, url=entry["url"], error=message)
        raise ServiceError(400, message) from exc
    except StoreUnavailable as exc:
        raise ServiceError(409, t("status.mcp.no_store")) from exc
    return {"auth_url": url or None, "state": server_state(name)}


def _store_ready() -> bool:
    from hokma.engines import secrets

    return secrets.available()


def open_in_browser(name: str, url: str) -> bool:
    """For the desktop app, whose window cannot open a new tab: the server opens the system browser itself. Only the
    authorization page of a sign-in in progress for this server is accepted, never an arbitrary address."""
    _server(name)
    endpoint = mcp_oauth.load_state().get(name, {}).get("authorization_endpoint") or ""
    if not endpoint or not mcp_oauth.pending(name) or not url.startswith(endpoint + ("&" if "?" in endpoint else "?")):
        raise ServiceError(409, t("status.mcp.no_login"))
    import webbrowser

    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:
        return False


def cancel_connect(name: str) -> None:
    mcp_oauth.cancel(name)


def finish_connect(params: dict[str, str]) -> tuple[bool, str]:
    """The browser callback. (ok, server name or error message). Confirms the token against the server once."""
    try:
        name = mcp_oauth.finish(params)
    except mcp_oauth.OAuthError as exc:
        return False, exc.message
    from hokma.connections.store import StoreUnavailable

    try:
        token = mcp_oauth.access_token(name)
        entry = mcp_oauth.load_state().get(name, {})
        if token and entry.get("url"):
            resp = mcp_oauth.probe(entry["url"], token)
            if resp.status in (401, 403):
                message = t("status.mcp.rejected", status=resp.status)
                mcp_oauth.forget_tokens(name)
                mcp_oauth.update_entry(name, error=message)
                return False, message
    except mcp_oauth.OAuthError:
        pass                                                  # network hiccup: keep the token, the engine retries
    except StoreUnavailable:
        return False, t("status.mcp.no_store")
    return True, name


# ---------------------------------------------------------------- revoke

def revoke(name: str) -> dict:
    _server(name)
    mcp_oauth.revoke(name)
    mcp_oauth.update_entry(name, error=None)
    return server_state(name)


# ---------------------------------------------------------------- background refresh

_loop_started = False
_loop_lock = threading.Lock()


def start_refresher() -> None:
    """One daemon thread per process: refresh tokens before they end, so every engine finds a valid one."""
    global _loop_started
    with _loop_lock:
        if _loop_started:
            return
        _loop_started = True

    def run() -> None:
        while True:
            time.sleep(REFRESH_EVERY)
            try:
                mcp_oauth.refresh_due()
            except Exception:                              # never take the server down; the next pass retries
                pass

    threading.Thread(target=run, daemon=True, name="hokma-mcp-refresh").start()

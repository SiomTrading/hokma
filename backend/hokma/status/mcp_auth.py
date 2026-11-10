"""Remote MCP servers added and connected from the panel, for every engine (Claude Code, Codex, Gemini, direct API).

Since 0.0.15 Hokma signs in by itself (`mcp_oauth`): Connect opens the system browser on the authorization page, the
browser comes back to this server's `/oauth/mcp/callback` and the panel sees the server as connected. No terminal, no
pasted address, no `claude mcp login`. The tokens live only in the system credential store.

Servers registered with `claude mcp add --scope user` by 0.0.10-0.0.14 are removed from Claude's own config when they
are added or connected here, and at every start (`cleanup_legacy_registrations`), so Claude Code reads them only from
Hokma's --mcp-config.

Since 0.0.16 (task 3152) an address is saved only when a POST `initialize` gets a real MCP answer (2xx with the
JSON-RPC result) or a sign-in request (401 with `WWW-Authenticate: Bearer`), and a server shows Connected only when it
answered as an MCP server at its current address (`verified_url`): open, or with a valid token. A web page saved by
mistake (https://lauz.io/app/mcp) is an error with its reason, never Connected. Each server can be removed and its
address edited from the panel.
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
CHECK_EVERY = 120                   # seconds between two automatic checks of a server not confirmed yet


def _same_url(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and str(a).rstrip("/").lower() == str(b).rstrip("/").lower()


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


def claude_registrations() -> set[str]:
    """Names in the user scope of Claude's own config (`claude mcp add --scope user`), in Hokma's isolated config dir."""
    try:
        data = json.loads((claude_config_dir() / ".claude.json").read_text(encoding="utf-8"))
        servers = data.get("mcpServers") or {}
        return {str(name) for name in servers} if isinstance(servers, dict) else set()
    except (OSError, ValueError, AttributeError):
        return set()


def _forget_claude_registration(name: str) -> None:
    """Best effort: drop a server 0.0.10-0.0.14 registered in Claude's user config, so --mcp-config is the only one."""
    if name not in claude_registrations():
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


def cleanup_legacy_registrations() -> list[str]:
    """At every start (so also right after an update): 0.0.10-0.0.12 registered each remote server added from the
    panel also with `claude mcp add --scope user`. That copy keeps the address it had then and wins over Hokma's
    --mcp-config, so Claude Code could still call a web page (task 3152). Every such copy of a remote server Hokma
    manages is removed; servers the student registered by hand under other names stay. Returns the names removed."""
    data, _ = extensions.load()
    ours = {n for n, e in data["mcpServers"].items() if isinstance(e, dict) and e.get("type") == "http"}
    found = sorted(claude_registrations() & ours)
    for name in found:
        _forget_claude_registration(name)
    return [name for name in found if name not in claude_registrations()]


# ---------------------------------------------------------------- address check

def _address_error(exc: mcp_oauth.OAuthError) -> str:
    if exc.code == "network":
        return t("status.mcp.unreachable", output=exc.message)
    if exc.code == "not_mcp":
        return t("status.mcp.not_mcp", output=exc.message)
    return t("status.mcp.connect_failed", output=exc.message)


def check_address(url: str) -> dict:
    """Before an address is saved: POST `initialize`. Only a real MCP answer (2xx with the JSON-RPC result) or a sign-in
    request (401 with `WWW-Authenticate: Bearer`) is accepted. {'auth': bool}; raises ServiceError(400) otherwise."""
    try:
        resp = mcp_oauth.probe(url)
    except mcp_oauth.OAuthError as exc:
        raise ServiceError(400, _address_error(exc)) from exc
    if mcp_oauth.is_mcp_answer(resp):
        return {"auth": False}
    if mcp_oauth.asks_for_sign_in(resp):
        return {"auth": True}
    raise ServiceError(400, _address_error(mcp_oauth.not_mcp(resp)))


def _remember_check(name: str, url: str, auth: bool) -> None:
    mcp_oauth.update_entry(name, url=url, auth=auth, error=None, verified_url=None if auth else url,
                           checked_at=int(time.time()))


def _clean_name(name: str) -> str:
    name = (name or "").strip()
    if not NAME.match(name) or name in RESERVED:
        raise ServiceError(400, t("status.mcp.bad_name"))
    return name


def _clean_url(url: str) -> str:
    url = normalize_url(url)
    if len(url) > 1000:
        raise ServiceError(400, t("status.mcp.bad_url"))
    return url


# ---------------------------------------------------------------- add

def add_server(name: str, url: str) -> dict:
    name = _clean_name(name)
    url = _clean_url(url)
    data, _ = extensions.load()
    if name in data["mcpServers"]:
        raise ServiceError(409, t("status.mcp.exists", name=name))
    check = check_address(url)                            # nothing is saved for an address that is not MCP
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
    _remember_check(name, url, check["auth"])
    _forget_claude_registration(name)
    return {**server_state(name), "warnings": warnings}


# ---------------------------------------------------------------- remove and edit

def remove_server(name: str) -> dict:
    """Quitar: revoke the access at the provider (best effort), forget everything about it and take it out of every
    engine, Claude's old user registration included."""
    _server(name)
    data, _ = extensions.load()
    db = _db()
    with db.write() as unit:
        owner = _owner(unit)
        sync_table(unit, data)
        row = unit.extensions.find(LOCAL_MARKETPLACE, name) or unit.extensions.add(name, LOCAL_MARKETPLACE, "mcp")
        request = unit.extension_requests.create("uninstall", "mcp", name, LOCAL_MARKETPLACE,
                                                 f"dashboard: remove remote MCP {name}", actor=Actor.human(owner))
        unit.extension_requests.approve(request.id, decided_by=owner)
        warnings = extensions.remove_mcp(name)
        unit.extensions.set_enabled(row.id, False)
        unit.extension_requests.mark_applied(request.id, row.id)
    mcp_oauth.forget(name)
    _forget_claude_registration(name)
    return {"name": name, "removed": True, "warnings": warnings}


def update_server_url(name: str, url: str) -> dict:
    """Editar URL: the new address goes through the same check as a new server. A sign-in belongs to the address it was
    made for, so a different address starts without one (the old access is revoked)."""
    entry = _server(name)
    url = _clean_url(url)
    check = check_address(url)
    if _same_url(url, entry.get("url")):                  # same address: check it again, keep a valid sign-in
        if check["auth"]:
            mcp_oauth.update_entry(name, url=url, auth=True, error=None, checked_at=None)
            ensure_checked(name, url)
        else:
            _remember_check(name, url, False)
        return server_state(name)
    data, _ = extensions.load()
    db = _db()
    with db.write() as unit:
        owner = _owner(unit)
        sync_table(unit, data)
        request = unit.extension_requests.create("install", "mcp", name, LOCAL_MARKETPLACE,
                                                 f"dashboard: change the address of remote MCP {name}",
                                                 actor=Actor.human(owner))
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
    mcp_oauth.forget(name)
    _remember_check(name, url, check["auth"])
    _forget_claude_registration(name)
    return {**server_state(name), "warnings": warnings}


# ---------------------------------------------------------------- state

def server_state(name: str) -> dict:
    """needs_auth | connected | error, from the plain state file and the credential store (no network).

    Connected needs an MCP answer at the current address (`verified_url`): open, or with the token of this address.
    `signed_in` says there is a token to revoke."""
    entry = _server(name)
    url = str(entry.get("url") or "")
    oauth = mcp_oauth.load_state().get(name, {})
    same = _same_url(oauth.get("url"), url)                # the address changed: the old sign-in does not count
    verified = same and _same_url(oauth.get("verified_url"), url)
    signed_in = same and oauth.get("auth") is not False and mcp_oauth.has_tokens(name)
    info = {"name": name, "url": url, "state": "needs_auth", "message": "",
            "auth": oauth.get("auth", True), "pending": mcp_oauth.pending(name), "signed_in": signed_in}
    if oauth.get("auth") is False and verified:
        info["state"] = "connected"
    elif signed_in and verified:
        expires = oauth.get("expires_at")
        expired = bool(expires) and expires <= time.time()
        has_refresh = bool(mcp_oauth.secret_get(f"{name}:refresh"))
        info["state"] = "needs_auth" if expired and not has_refresh else "connected"
    if oauth.get("error") and info["state"] != "connected":
        info["state"] = "error"
        info["message"] = str(oauth["error"])[:300]
    return info


def probe_unknown(name: str, url: str) -> None:
    """A server approved from an agent request has never been probed: find out once whether it needs a sign-in and
    whether it is an MCP server at all."""
    try:
        resp = mcp_oauth.probe(url)
    except mcp_oauth.OAuthError as exc:
        mcp_oauth.update_entry(name, url=url, auth=False, error=_address_error(exc), checked_at=int(time.time()))
        return
    if mcp_oauth.is_mcp_answer(resp):
        _remember_check(name, url, False)
    elif mcp_oauth.asks_for_sign_in(resp):
        _remember_check(name, url, True)
    else:
        mcp_oauth.update_entry(name, url=url, auth=False, error=_address_error(mcp_oauth.not_mcp(resp)),
                               checked_at=int(time.time()))


def _verify(name: str, url: str, entry: dict) -> None:
    """Confirm with a POST `initialize` (with the token when it needs one) that this address answers as MCP."""
    token = None
    if entry.get("auth"):
        token = mcp_oauth.access_token(name)
        if not token:
            return
    mcp_oauth.update_entry(name, checked_at=int(time.time()))
    try:
        resp = mcp_oauth.probe(url, token)
    except mcp_oauth.OAuthError as exc:
        mcp_oauth.update_entry(name, error=_address_error(exc))
        return
    if mcp_oauth.is_mcp_answer(resp):
        mcp_oauth.update_entry(name, verified_url=url, error=None)
    elif token and resp.status in (401, 403):
        mcp_oauth.forget_tokens(name)                       # the token is no longer valid: Connect again
        mcp_oauth.update_entry(name, error=None)
    elif not token and mcp_oauth.asks_for_sign_in(resp):
        mcp_oauth.update_entry(name, auth=True, error=None)  # it asks for a sign-in now
    else:
        mcp_oauth.update_entry(name, error=_address_error(mcp_oauth.not_mcp(resp)))


def ensure_checked(name: str, url: str) -> None:
    """Probe a server never probed, and confirm one not confirmed at this address yet (0.0.15 marked a server open
    on any 2xx, a web page included), at most every CHECK_EVERY seconds. A changed address is left alone: Connect
    starts over there, and the old token never follows the name."""
    entry = mcp_oauth.load_state().get(name, {})
    if "auth" not in entry:
        probe_unknown(name, url)
        return
    if not _same_url(entry.get("url"), url) or _same_url(entry.get("verified_url"), url):
        return
    if time.time() - float(entry.get("checked_at") or 0) < CHECK_EVERY:
        return
    if entry.get("auth") is False or mcp_oauth.has_tokens(name):
        _verify(name, url, entry)


def servers_state() -> list[dict]:
    data, _ = extensions.load()
    remote = {n: e for n, e in sorted(data["mcpServers"].items()) if isinstance(e, dict) and e.get("type") == "http"}
    for name, entry in remote.items():
        ensure_checked(name, entry["url"])
    return [server_state(name) for name in remote]


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
        if exc.code == "no_registration":
            message = t("status.mcp.no_registration", output=exc.message)
        else:
            message = _address_error(exc)
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
            if not mcp_oauth.is_mcp_answer(resp):           # signed in, but the address does not answer as MCP
                message = _address_error(mcp_oauth.not_mcp(resp))
                mcp_oauth.forget_tokens(name)
                mcp_oauth.update_entry(name, error=message)
                return False, message
            mcp_oauth.update_entry(name, verified_url=entry["url"], checked_at=int(time.time()))
    except mcp_oauth.OAuthError:
        pass                                    # network hiccup: keep the token, ensure_checked confirms it later
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


def start_legacy_cleanup() -> None:
    """At the start of the engine, off the request path: `cleanup_legacy_registrations` may call `claude` once."""
    def run() -> None:
        try:
            cleanup_legacy_registrations()
        except Exception:                                  # best effort; the next start tries again
            pass

    threading.Thread(target=run, daemon=True, name="hokma-mcp-legacy").start()

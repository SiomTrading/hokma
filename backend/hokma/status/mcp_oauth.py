"""OAuth 2.1 client for remote MCP servers, done by Hokma itself (any engine, no `claude mcp login`).

Flow, following the MCP authorization spec:
1. Probe: POST `initialize` to the server. A 401 with `WWW-Authenticate: Bearer resource_metadata=...` (or the well-known
   fallbacks) means it needs a sign-in; a 2xx carrying the JSON-RPC result of `initialize` means it is open. Anything
   else (a web page, a redirect, an error) is not an MCP server and is refused (task 3152).
2. Discovery: the protected resource metadata (RFC 9728) names the authorization server; its metadata is read with the
   RFC 8414 path insertion (`/.well-known/oauth-authorization-server/<issuer path>`), then the appended and OpenID
   variants. The issuer in the document must match.
3. Dynamic client registration (RFC 7591) with the loopback callback of this Hokma server as redirect_uri. A new port
   means a new registration.
4. Authorization code with PKCE S256, `state` (one use, ten minutes), `resource` (RFC 8707), the resource scopes and
   `offline_access` when the authorization server offers it. The browser comes back to `/oauth/mcp/callback`.
5. Token and refresh token go ONLY to the system credential store (service `hokma-engine-mcp`, already covered by the
   hardblock rule for `hokma-engine`). Long values are split in chunks because Windows Credential Manager caps one entry
   at 2560 bytes. `<data>/mcp-oauth.json` keeps what is not secret: endpoints, client_id, expiry, last error.

Nothing here logs, stores in the database or returns a token. Error texts carry only the OAuth `error` code and
`error_description`, never a response body.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets as pysecrets
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from hokma import __version__
from hokma.core.config import hokma_data_dir

SERVICE = "hokma-engine-mcp"
STATE_FILE = "mcp-oauth.json"
CALLBACK_PATH = "/oauth/mcp/callback"
FLOW_LIFETIME = 600                 # seconds a started sign-in waits for the browser
REFRESH_MARGIN = 300                # refresh when less than this is left
HTTP_TIMEOUT = 15
MAX_BODY = 1_000_000
CHUNK = 1000                        # characters per credential entry (UTF-16 on Windows: 2000 bytes < 2560)
PROTOCOL_VERSION = "2025-06-18"
_BEARER_PARAM = re.compile(r'([A-Za-z_]+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s,]+))')

# Tests serve the fake authorization server on http://127.0.0.1; the real code only talks https.
allow_insecure_loopback = False


class OAuthError(Exception):
    """A step of the flow failed. `message` is safe to show: it never contains a token or a response body."""

    def __init__(self, message: str, code: str = "oauth"):
        super().__init__(message)
        self.message = message
        self.code = code


# ----------------------------------------------------------------- HTTP

def _check_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return
    if allow_insecure_loopback and parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost"):
        return
    raise OAuthError(f"not an https address: {parts.scheme}://{parts.hostname or ''}", "bad_url")


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):                 # never follow a redirect with a token or a code
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def http(method: str, url: str, *, headers: dict[str, str] | None = None, data: bytes | None = None,
         timeout: float = HTTP_TIMEOUT) -> Response:
    _check_url(url)
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": f"Hokma/{__version__}", **(headers or {})})
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read(MAX_BODY))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(MAX_BODY)
        except OSError:
            body = b""
        return Response(exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, body)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        raise OAuthError(f"{urllib.parse.urlsplit(url).hostname}: {reason}", "network") from None


def _get_json(url: str) -> dict | None:
    resp = http("GET", url, headers={"Accept": "application/json"})
    data = resp.json() if resp.status == 200 else None
    return data if isinstance(data, dict) else None


def _form(url: str, fields: dict[str, str], client: dict) -> dict:
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    fields = {k: v for k, v in fields.items() if v}
    secret = client.get("client_secret")
    if secret and client.get("auth_method") == "client_secret_basic":
        raw = f"{urllib.parse.quote(client['client_id'], safe='')}:{urllib.parse.quote(secret, safe='')}"
        headers["Authorization"] = "Basic " + base64.b64encode(raw.encode()).decode()
    else:
        fields["client_id"] = client["client_id"]
        if secret:
            fields["client_secret"] = secret
    resp = http("POST", url, headers=headers, data=urllib.parse.urlencode(fields).encode())
    data = resp.json()
    if resp.status != 200 or not isinstance(data, dict) or not data.get("access_token"):
        raise OAuthError(_oauth_error(resp, data), "invalid_grant" if isinstance(data, dict)
                         and data.get("error") == "invalid_grant" else "token")
    if str(data.get("token_type", "bearer")).lower() != "bearer":
        raise OAuthError("token_type is not Bearer", "token")
    return data


def _oauth_error(resp: Response, data: Any) -> str:
    if isinstance(data, dict) and isinstance(data.get("error"), str):
        desc = data.get("error_description")
        text = data["error"][:80] + (f": {str(desc)[:200]}" if isinstance(desc, str) and desc else "")
        return f"HTTP {resp.status} {text}"
    return f"HTTP {resp.status}"


# ----------------------------------------------------------------- probe and discovery

def parse_www_authenticate(value: str) -> dict[str, str]:
    """Parameters of the Bearer challenge (resource_metadata, scope, error)."""
    if not value:
        return {}
    low = value.lower()
    start = low.find("bearer")
    if start < 0:
        return {}
    return {m.group(1).lower(): (m.group(2) if m.group(2) is not None else m.group(3)).replace('\\"', '"')
            for m in _BEARER_PARAM.finditer(value[start + 6:])}


def _initialize_body() -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                  "clientInfo": {"name": "hokma", "version": __version__}}}).encode()


def probe(url: str, token: str | None = None) -> Response:
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": PROTOCOL_VERSION}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return http("POST", url, headers=headers, data=_initialize_body())


def rpc_messages(resp: Response) -> list[dict]:
    """JSON-RPC messages of an MCP answer: a JSON body (one message or a batch) or Server-Sent Events `data:` lines."""
    text = resp.body.decode("utf-8", "replace")
    if "text/event-stream" in resp.headers.get("content-type", "").lower():
        out, data = [], []
        for line in text.splitlines() + [""]:
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif not line.strip() and data:
                try:
                    msg = json.loads("\n".join(data))
                    out.extend(msg if isinstance(msg, list) else [msg])
                except ValueError:
                    pass
                data = []
        return [m for m in out if isinstance(m, dict)]
    try:
        msg = json.loads(text)
    except ValueError:
        return []
    return [m for m in (msg if isinstance(msg, list) else [msg]) if isinstance(m, dict)]


def is_mcp_answer(resp: Response) -> bool:
    """A 2xx that really comes from an MCP server: JSON or an event stream carrying the JSON-RPC result of
    `initialize`. A web page (HTML), an empty body or any other JSON is not one (task 3152)."""
    if not 200 <= resp.status < 300:
        return False
    kind = resp.headers.get("content-type", "").lower()
    if "application/json" not in kind and "text/event-stream" not in kind:
        return False
    return any(m.get("jsonrpc") == "2.0" and m.get("id") == 1 and isinstance(m.get("result"), dict)
               for m in rpc_messages(resp))


def asks_for_sign_in(resp: Response) -> bool:
    """A 401 with a Bearer challenge in `WWW-Authenticate`: an MCP server that needs a sign-in."""
    return resp.status == 401 and "bearer" in resp.headers.get("www-authenticate", "").lower()


def not_mcp(resp: Response) -> OAuthError:
    """The error for an address that answered, but not as an MCP server. Names the status and type, never the body."""
    kind = resp.headers.get("content-type", "").split(";")[0].strip() or "no content type"
    return OAuthError(f"HTTP {resp.status} {kind}", "not_mcp")


def _well_known(base: str, name: str) -> list[str]:
    """RFC 8414 / RFC 9728 path insertion first, then the root, then the appended legacy form."""
    parts = urllib.parse.urlsplit(base)
    root = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.rstrip("/")
    out = [f"{root}/.well-known/{name}{path}"] if path else []
    out.append(f"{root}/.well-known/{name}")
    if path:
        out.append(f"{root}{path}/.well-known/{name}")
    return list(dict.fromkeys(out))


def _same(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and a.rstrip("/") == b.rstrip("/")


def authorization_server_metadata(issuer: str) -> dict:
    _check_url(issuer)
    candidates = _well_known(issuer, "oauth-authorization-server") + _well_known(issuer, "openid-configuration")
    for candidate in dict.fromkeys(candidates):
        data = _get_json(candidate)
        if data and data.get("authorization_endpoint") and data.get("token_endpoint"):
            if not _same(data.get("issuer"), issuer):
                raise OAuthError(f"issuer mismatch in {candidate}", "discovery")
            for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint", "revocation_endpoint"):
                if data.get(key):
                    _check_url(data[key])
            return data
    raise OAuthError(f"no authorization server metadata for {issuer}", "discovery")


def discover(url: str) -> dict:
    """{'auth': False} for an open server, else everything needed to sign in (no secrets)."""
    resp = probe(url)
    if is_mcp_answer(resp):
        return {"auth": False}
    if not asks_for_sign_in(resp):
        raise not_mcp(resp)                # a web page, a redirect, an error page: never "connected" (task 3152)
    challenge = parse_www_authenticate(resp.headers.get("www-authenticate", ""))
    prm = None
    if challenge.get("resource_metadata"):
        _check_url(challenge["resource_metadata"])
        prm = _get_json(challenge["resource_metadata"])
    if prm is None:
        for candidate in _well_known(url, "oauth-protected-resource"):
            prm = _get_json(candidate)
            if prm:
                break
    if not prm or not prm.get("authorization_servers"):
        raise OAuthError("the server asks for a sign-in but publishes no authorization server", "discovery")
    resource = prm.get("resource") or url
    if not _same(urllib.parse.urlsplit(resource).netloc, urllib.parse.urlsplit(url).netloc):
        raise OAuthError("the protected resource metadata names another host", "discovery")
    issuer = str(prm["authorization_servers"][0])
    meta = authorization_server_metadata(issuer)
    if "S256" not in (meta.get("code_challenge_methods_supported") or []):
        raise OAuthError("the authorization server does not offer PKCE S256", "discovery")
    scopes = challenge.get("scope", "").split() or list(prm.get("scopes_supported") or [])
    if "offline_access" in (meta.get("scopes_supported") or []) and "offline_access" not in scopes:
        scopes.append("offline_access")
    return {"auth": True, "resource": resource, "issuer": issuer, "scopes": scopes,
            "authorization_endpoint": meta["authorization_endpoint"], "token_endpoint": meta["token_endpoint"],
            "registration_endpoint": meta.get("registration_endpoint"),
            "revocation_endpoint": meta.get("revocation_endpoint"),
            "auth_methods": list(meta.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]),
            "iss_parameter": bool(meta.get("authorization_response_iss_parameter_supported"))}


def register_client(meta: dict, redirect_uri: str) -> dict:
    """RFC 7591. Returns {client_id, auth_method} plus client_secret when the server issues one."""
    if not meta.get("registration_endpoint"):
        raise OAuthError("the authorization server does not allow automatic registration", "no_registration")
    method = "none" if "none" in meta["auth_methods"] else (
        "client_secret_post" if "client_secret_post" in meta["auth_methods"] else "client_secret_basic")
    body = {"client_name": "Hokma", "redirect_uris": [redirect_uri], "grant_types": ["authorization_code",
            "refresh_token"], "response_types": ["code"], "token_endpoint_auth_method": method,
            "software_id": "hokma", "software_version": __version__}
    # No `scope` here: it is optional (RFC 7591) and some servers (Lauz) refuse values they list as supported. The
    # scopes go in the authorization request instead.
    resp = http("POST", meta["registration_endpoint"], data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json", "Accept": "application/json"})
    data = resp.json()
    if resp.status not in (200, 201) or not isinstance(data, dict) or not data.get("client_id"):
        raise OAuthError("registration failed: " + _oauth_error(resp, data), "registration")
    out = {"client_id": str(data["client_id"]), "auth_method": data.get("token_endpoint_auth_method") or method}
    if data.get("client_secret"):
        out["client_secret"] = str(data["client_secret"])
    return out


def pkce_pair() -> tuple[str, str]:
    verifier = pysecrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    return verifier, challenge


# ----------------------------------------------------------------- secret store (keyring, chunked)

def _keyring():
    import keyring
    from hokma.connections.store import StoreUnavailable, _usable

    try:
        usable = _usable(keyring.get_keyring())
    except Exception:
        usable = False
    if not usable:
        raise StoreUnavailable()
    return keyring


def secret_get(account: str) -> str | None:
    from keyring.errors import KeyringError
    from hokma.connections.store import StoreUnavailable

    try:
        kr = _keyring()
        head = kr.get_password(SERVICE, account)
        if head is None or not head.startswith("chunks:"):
            return head
        parts = [kr.get_password(SERVICE, f"{account}#{i}") for i in range(int(head[7:]))]
        return None if any(p is None for p in parts) else "".join(parts)
    except (StoreUnavailable, KeyringError, ValueError):
        return None


def secret_delete(account: str) -> None:
    from keyring.errors import KeyringError, PasswordDeleteError
    from hokma.connections.store import StoreUnavailable

    try:
        kr = _keyring()
        head = kr.get_password(SERVICE, account)
        count = int(head[7:]) if head and head.startswith("chunks:") else 0
        for name in [account, *(f"{account}#{i}" for i in range(count))]:
            try:
                kr.delete_password(SERVICE, name)
            except PasswordDeleteError:
                pass
    except (StoreUnavailable, KeyringError, ValueError):
        pass


def secret_put(account: str, value: str) -> None:
    from keyring.errors import KeyringError
    from hokma.connections.store import StoreUnavailable

    kr = _keyring()
    secret_delete(account)
    try:
        if len(value) <= CHUNK:
            kr.set_password(SERVICE, account, value)
            return
        parts = [value[i:i + CHUNK] for i in range(0, len(value), CHUNK)]
        for i, part in enumerate(parts):
            kr.set_password(SERVICE, f"{account}#{i}", part)
        kr.set_password(SERVICE, account, f"chunks:{len(parts)}")
    except KeyringError as exc:
        raise StoreUnavailable() from exc


def _accounts(name: str) -> tuple[str, str, str]:
    return f"{name}:access", f"{name}:refresh", f"{name}:client"


# ----------------------------------------------------------------- non-secret state file

_file_lock = threading.RLock()


def _state_path():
    return hokma_data_dir() / STATE_FILE


def load_state() -> dict[str, dict]:
    with _file_lock:
        try:
            data = json.loads(_state_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def save_entry(name: str, entry: dict | None) -> None:
    banned = ("access_token", "refresh_token", "client_secret", "code_verifier")
    if entry is not None and any(k in entry for k in banned):
        raise ValueError("secret field in the plain state file")
    with _file_lock:
        data = load_state()
        if entry is None:
            data.pop(name, None)
        else:
            data[name] = entry
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".mcp-oauth-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def update_entry(name: str, **changes: Any) -> dict:
    with _file_lock:
        entry = {**load_state().get(name, {}), **changes}
        entry = {k: v for k, v in entry.items() if v is not None}
        save_entry(name, entry)
        return entry


# ----------------------------------------------------------------- sign-in flows

@dataclass
class Flow:
    name: str
    verifier: str
    redirect_uri: str
    issuer: str
    created: float = field(default_factory=time.monotonic)


_flows: dict[str, Flow] = {}
_flows_lock = threading.Lock()


def _drop_expired() -> None:
    now = time.monotonic()
    for state in [s for s, f in _flows.items() if now - f.created > FLOW_LIFETIME]:
        _flows.pop(state, None)


def start(name: str, url: str, redirect_uri: str) -> str:
    """Discover, register when needed and return the authorization URL for the system browser."""
    old = load_state().get(name, {})
    if old.get("url") and not _same(old.get("url"), url):
        _wipe(name)                        # a token is bound to the address it was issued for, never to the name
    meta = discover(url)
    if not meta["auth"]:
        update_entry(name, url=url, auth=False, error=None, verified_url=url)
        return ""
    entry = load_state().get(name, {})
    if entry.get("issuer") and not _same(entry.get("issuer"), meta["issuer"]):
        _wipe(name)
        entry = {}
    client = None
    if entry.get("client_id") and entry.get("redirect_uri") == redirect_uri and entry.get("issuer") == meta["issuer"]:
        client = {"client_id": entry["client_id"], "auth_method": entry.get("auth_method", "none")}
    if client is None:
        client = register_client(meta, redirect_uri)
        _, _, client_account = _accounts(name)
        if client.get("client_secret"):
            secret_put(client_account, client.pop("client_secret"))
        else:
            secret_delete(client_account)
    update_entry(name, url=url, auth=True, resource=meta["resource"], issuer=meta["issuer"], scopes=meta["scopes"],
                 authorization_endpoint=meta["authorization_endpoint"], token_endpoint=meta["token_endpoint"],
                 revocation_endpoint=meta.get("revocation_endpoint"), iss_parameter=meta["iss_parameter"],
                 client_id=client["client_id"], auth_method=client["auth_method"], redirect_uri=redirect_uri)
    verifier, challenge = pkce_pair()
    state = pysecrets.token_urlsafe(32)
    with _flows_lock:
        _drop_expired()
        for old in [s for s, f in _flows.items() if f.name == name]:
            _flows.pop(old, None)                                 # a new Connect replaces the previous one
        _flows[state] = Flow(name, verifier, redirect_uri, meta["issuer"])
    query = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": redirect_uri,
             "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
             "resource": meta["resource"]}
    if meta["scopes"]:
        query["scope"] = " ".join(meta["scopes"])
    sep = "&" if urllib.parse.urlsplit(meta["authorization_endpoint"]).query else "?"
    return meta["authorization_endpoint"] + sep + urllib.parse.urlencode(query)


def pending(name: str) -> bool:
    with _flows_lock:
        _drop_expired()
        return any(f.name == name for f in _flows.values())


def cancel(name: str) -> None:
    with _flows_lock:
        for state in [s for s, f in _flows.items() if f.name == name]:
            _flows.pop(state, None)


def _client(entry: dict, name: str) -> dict:
    client = {"client_id": entry.get("client_id", ""), "auth_method": entry.get("auth_method", "none")}
    secret = secret_get(_accounts(name)[2])
    if secret:
        client["client_secret"] = secret
    return client


def _store_tokens(name: str, data: dict, keep_refresh: bool) -> None:
    access, refresh, _ = _accounts(name)
    secret_put(access, str(data["access_token"]))
    if data.get("refresh_token"):
        secret_put(refresh, str(data["refresh_token"]))
    elif not keep_refresh:
        secret_delete(refresh)
    try:
        lifetime = int(data.get("expires_in") or 0)
    except (TypeError, ValueError):
        lifetime = 0
    update_entry(name, expires_at=int(time.time()) + lifetime if lifetime > 0 else None, error=None,
                 connected_at=int(time.time()), granted_scope=str(data.get("scope") or "") or None)


def finish(params: dict[str, str]) -> str:
    """The browser came back. Returns the server name; raises OAuthError. `state` is used once, whatever happens."""
    state = params.get("state") or ""
    with _flows_lock:
        _drop_expired()
        flow = _flows.pop(state, None)
    if flow is None:
        raise OAuthError("unknown or expired sign-in", "state")
    if params.get("error"):
        desc = params.get("error_description") or ""
        message = params["error"][:80] + (f": {desc[:200]}" if desc else "")
        update_entry(flow.name, error=message)
        raise OAuthError(message, "denied")
    entry = load_state().get(flow.name, {})
    if params.get("iss") and not _same(params["iss"], flow.issuer):
        update_entry(flow.name, error="issuer mismatch")
        raise OAuthError("issuer mismatch", "iss")
    if entry.get("iss_parameter") and not params.get("iss"):
        update_entry(flow.name, error="missing iss")
        raise OAuthError("missing iss", "iss")
    code = params.get("code") or ""
    if not code:
        raise OAuthError("no authorization code", "code")
    try:
        data = _form(entry["token_endpoint"], {"grant_type": "authorization_code", "code": code,
                                               "redirect_uri": flow.redirect_uri, "code_verifier": flow.verifier,
                                               "resource": entry.get("resource", "")}, _client(entry, flow.name))
    except OAuthError as exc:
        update_entry(flow.name, error=exc.message)
        raise
    _store_tokens(flow.name, data, keep_refresh=False)
    return flow.name


_refresh_lock = threading.Lock()


def refresh(name: str) -> bool:
    """Exchange the refresh token. False when there is none or the server refused it (the student must reconnect)."""
    with _refresh_lock:
        entry = load_state().get(name, {})
        token = secret_get(_accounts(name)[1])
        if not token or not entry.get("token_endpoint"):
            return False
        try:
            data = _form(entry["token_endpoint"], {"grant_type": "refresh_token", "refresh_token": token,
                                                   "resource": entry.get("resource", "")}, _client(entry, name))
        except OAuthError as exc:
            if exc.code == "invalid_grant":
                forget_tokens(name)
                update_entry(name, error=None)
            else:
                update_entry(name, error=exc.message)
            return False
        _store_tokens(name, data, keep_refresh=True)
        return True


def access_token(name: str) -> str | None:
    """A valid access token, refreshed first when it is close to expiry. Never logged by the callers."""
    entry = load_state().get(name, {})
    if not entry.get("auth"):
        return None
    expires = entry.get("expires_at")
    if expires and expires - time.time() < REFRESH_MARGIN:
        if not refresh(name) and expires - time.time() <= 0:
            return None
    return secret_get(_accounts(name)[0])


def has_tokens(name: str) -> bool:
    return bool(secret_get(_accounts(name)[0]))


def forget_tokens(name: str) -> None:
    access, refresh_account, _ = _accounts(name)
    secret_delete(access)
    secret_delete(refresh_account)
    update_entry(name, expires_at=None, connected_at=None, granted_scope=None, verified_url=None)


def revoke(name: str) -> None:
    """RFC 7009 at the server when it offers it (best effort), then forget the tokens locally."""
    entry = load_state().get(name, {})
    access, refresh_account, _ = _accounts(name)
    endpoint = entry.get("revocation_endpoint")
    if endpoint:
        client = _client(entry, name)
        for account, hint in ((refresh_account, "refresh_token"), (access, "access_token")):
            token = secret_get(account)
            if not token:
                continue
            fields = {"token": token, "token_type_hint": hint}
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            if client.get("client_secret") and client["auth_method"] == "client_secret_basic":
                raw = f"{client['client_id']}:{client['client_secret']}"
                headers["Authorization"] = "Basic " + base64.b64encode(raw.encode()).decode()
            else:
                fields["client_id"] = client["client_id"]
            try:
                http("POST", endpoint, headers=headers, data=urllib.parse.urlencode(fields).encode())
            except OAuthError:
                pass
    cancel(name)
    forget_tokens(name)


def _wipe(name: str) -> None:
    """Drop tokens, client secret and the plain entry WITHOUT calling any server (the endpoints may be new)."""
    cancel(name)
    for account in _accounts(name):
        secret_delete(account)
    save_entry(name, None)


def forget(name: str) -> None:
    """Everything about a server: tokens, client secret and the plain entry."""
    revoke(name)
    secret_delete(_accounts(name)[2])
    save_entry(name, None)


def refresh_due(margin: int = REFRESH_MARGIN * 2) -> None:
    """Called by the background loop: refresh every token that ends within `margin` seconds."""
    for name, entry in load_state().items():
        expires = entry.get("expires_at")
        if entry.get("auth") and expires and expires - time.time() < margin:
            refresh(name)

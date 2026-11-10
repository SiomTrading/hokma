"""Remote MCP servers with Hokma's own OAuth client, against a fake authorization server and MCP server on loopback.

Covers discovery (resource metadata and RFC 8414 issuer path), dynamic registration, PKCE S256, one-use state,
callback, refresh, revocation, delivery to each engine, and that no token ever lands in the database, the logs, a
response or a plain file.
"""
import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import keyring
import pytest
from fastapi.testclient import TestClient
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from dbsupport import db, env  # noqa: F401  (fixtures)
from hokma.core.app import create_app
from hokma.kit import engine_key, extensions, launch
from hokma.rpc.ws import SESSION_TOKEN
from hokma.status import mcp_auth, mcp_oauth

HEADERS = {"X-Hokma-Token": SESSION_TOKEN}


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.store:
            raise PasswordDeleteError(username)
        del self.store[(service, username)]


class FakeAuth:
    """Authorization server + protected MCP resource. Records everything it saw."""

    def __init__(self, *, registration=True, long_tokens=False):
        self.registration = registration
        self.long_tokens = long_tokens
        self.clients: dict[str, dict] = {}
        self.codes: dict[str, dict] = {}
        self.access: set[str] = set()
        self.refresh: set[str] = set()
        self.revoked: list[str] = []
        self.token_requests: list[dict] = []
        self.issued: list[str] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, status, body, headers=None):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _body(self):
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def do_GET(self):
                path, _, query = self.path.partition("?")
                if path == "/.well-known/oauth-protected-resource/mcp":
                    return self._json(200, {"resource": fake.base + "/mcp", "authorization_servers": [fake.issuer],
                                            "scopes_supported": ["accounts:read", "trades:read"]})
                if path == "/.well-known/oauth-authorization-server/oauth":
                    meta = {"issuer": fake.issuer, "authorization_endpoint": fake.issuer + "/auth",
                            "token_endpoint": fake.issuer + "/token", "revocation_endpoint": fake.issuer + "/revoke",
                            "scopes_supported": ["accounts:read", "trades:read", "offline_access"],
                            "code_challenge_methods_supported": ["S256"],
                            "token_endpoint_auth_methods_supported": ["none"],
                            "authorization_response_iss_parameter_supported": True}
                    if fake.registration:
                        meta["registration_endpoint"] = fake.issuer + "/reg"
                    return self._json(200, meta)
                if path == "/oauth/auth":
                    q = dict(urllib.parse.parse_qsl(query))
                    code = secrets.token_urlsafe(16)
                    fake.codes[code] = q
                    loc = q["redirect_uri"] + "?" + urllib.parse.urlencode(
                        {"code": code, "state": q["state"], "iss": fake.issuer})
                    self.send_response(302)
                    self.send_header("Location", loc)
                    self.end_headers()
                    return
                self._json(404, {})

            def _issue(self):
                access = "AT-" + secrets.token_urlsafe(2000 if fake.long_tokens else 24)
                refresh = "RT-" + secrets.token_urlsafe(24)
                fake.access.add(access)
                fake.refresh.add(refresh)
                fake.issued += [access, refresh]
                return self._json(200, {"access_token": access, "refresh_token": refresh, "token_type": "Bearer",
                                        "expires_in": 3600, "scope": "accounts:read trades:read offline_access"})

            def do_POST(self):
                body = self._body()
                if self.path == "/oauth/reg":
                    data = json.loads(body)
                    client_id = "client-" + secrets.token_hex(4)
                    fake.clients[client_id] = data
                    return self._json(201, {"client_id": client_id, **data})
                if self.path == "/oauth/token":
                    form = dict(urllib.parse.parse_qsl(body.decode()))
                    fake.token_requests.append(form)
                    if form.get("grant_type") == "authorization_code":
                        q = fake.codes.pop(form.get("code", ""), None)
                        if q is None:
                            return self._json(400, {"error": "invalid_grant"})
                        challenge = base64.urlsafe_b64encode(hashlib.sha256(
                            form["code_verifier"].encode()).digest()).rstrip(b"=").decode()
                        if (challenge != q["code_challenge"] or form["redirect_uri"] != q["redirect_uri"]
                                or form["client_id"] != q["client_id"] or form.get("resource") != q["resource"]):
                            return self._json(400, {"error": "invalid_grant", "error_description": "pkce"})
                        return self._issue()
                    if form.get("grant_type") == "refresh_token":
                        if form.get("refresh_token") not in fake.refresh:
                            return self._json(400, {"error": "invalid_grant"})
                        fake.refresh.discard(form["refresh_token"])
                        return self._issue()
                    return self._json(400, {"error": "unsupported_grant_type"})
                if self.path == "/oauth/revoke":
                    form = dict(urllib.parse.parse_qsl(body.decode()))
                    fake.revoked.append(form.get("token_type_hint", ""))
                    fake.access.discard(form.get("token", ""))
                    fake.refresh.discard(form.get("token", ""))
                    return self._json(200, {})
                if self.path == "/mcp":
                    auth = self.headers.get("Authorization", "")
                    if auth[7:] not in fake.access:
                        meta = fake.base + "/.well-known/oauth-protected-resource/mcp"
                        return self._json(401, {"error": "invalid_token"}, {
                            "WWW-Authenticate": f'Bearer error="invalid_token", resource_metadata="{meta}"'})
                    msg = json.loads(body)
                    if "id" not in msg:
                        self.send_response(202)
                        self.end_headers()
                        return
                    if msg["method"] == "initialize":
                        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                                  "serverInfo": {"name": "fake"}}
                    elif msg["method"] == "tools/list":
                        result = {"tools": [{"name": "accounts", "description": "List accounts",
                                             "inputSchema": {"type": "object"}}]}
                    else:
                        result = {"content": [{"type": "text", "text": "two accounts"}]}
                    data = ("event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                                                   "result": result}) + "\n\n").encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Mcp-Session-Id", "sess-1")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self._json(404, {})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.issuer = self.base + "/oauth"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def authorize(self, auth_url: str) -> str:
        """What the student's browser does: open the page, get redirected back. Returns the callback path + query."""
        class Stop(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        try:
            urllib.request.build_opener(Stop).open(auth_url)
        except urllib.error.HTTPError as exc:
            loc = exc.headers["Location"]
        parts = urllib.parse.urlsplit(loc)
        return parts.path + "?" + parts.query

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def store(monkeypatch):
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    monkeypatch.setattr(mcp_oauth, "allow_insecure_loopback", True)
    yield backend
    keyring.set_keyring(previous)


@pytest.fixture
def fake():
    server = FakeAuth()
    yield server
    server.close()


@pytest.fixture
def client(db, store, monkeypatch):  # noqa: F811
    monkeypatch.setattr(mcp_auth, "normalize_url", lambda url: url.strip())   # the fake speaks http on loopback
    monkeypatch.setattr(mcp_auth, "_forget_claude_registration", lambda name: None)
    from hokma.kit import mcp_config
    monkeypatch.setattr(mcp_config, "HTTPS_URL", __import__("re").compile(r"^https?://[^\s]+$"))
    return TestClient(create_app())


def connect(client, fake, name="lauz"):
    r = client.post("/api/extensions/servers", json={"name": name, "url": fake.base + "/mcp"}, headers=HEADERS)
    assert r.status_code == 201, r.text
    assert r.json()["state"] == "needs_auth"
    r = client.post(f"/api/extensions/servers/{name}/connect", headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()["auth_url"]


def everything_on_disk(root) -> str:
    out = []
    for folder, _, files in os.walk(root):
        for f in files:
            try:
                with open(os.path.join(folder, f), "rb") as handle:
                    out.append(handle.read().decode("utf-8", "replace"))
            except OSError:
                pass
    return "\n".join(out)


# ----------------------------------------------------------------- the whole flow

def test_full_flow_discovery_registration_pkce_state_callback(client, fake, store, env, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    auth_url = connect(client, fake)
    parts = urllib.parse.urlsplit(auth_url)
    q = dict(urllib.parse.parse_qsl(parts.query))
    assert parts.path == "/oauth/auth"
    assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) == 43
    assert q["resource"] == fake.base + "/mcp"
    assert q["scope"].split() == ["accounts:read", "trades:read", "offline_access"]
    assert q["redirect_uri"].startswith("http://127.0.0.1:") and q["redirect_uri"].endswith("/oauth/mcp/callback")
    reg = fake.clients[q["client_id"]]
    assert reg["redirect_uris"] == [q["redirect_uri"]] and reg["token_endpoint_auth_method"] == "none"
    assert client.get("/api/extensions/servers", headers=HEADERS).json()["servers"][0]["pending"] is True

    callback = fake.authorize(auth_url)
    page = client.get(callback)                                   # no session token: the state authorizes it
    assert page.status_code == 200 and "close this tab" in page.text
    servers = client.get("/api/extensions/servers", headers=HEADERS).json()["servers"]
    assert servers[0]["state"] == "connected" and servers[0]["pending"] is False

    again = client.get(callback)                                  # state is used once
    assert again.status_code == 400

    tokens = fake.issued
    assert store.get_password("hokma-engine-mcp", "lauz:access") in tokens
    seen = everything_on_disk(env) + caplog.text + page.text + json.dumps(servers)
    assert not any(t in seen for t in tokens)                     # not in the db, state file, logs or responses
    state = json.loads((env / "data" / "mcp-oauth.json").read_text())
    assert state["lauz"]["client_id"] == q["client_id"] and "access_token" not in json.dumps(state)


def test_long_tokens_are_chunked_in_the_store(client, store, env):  # noqa: F811
    server = FakeAuth(long_tokens=True)
    try:
        client.get(server.authorize(connect(client, server)))
        head = store.get_password("hokma-engine-mcp", "lauz:access")
        assert head.startswith("chunks:") and all(len(v) <= mcp_oauth.CHUNK for v in store.store.values())
        assert mcp_oauth.access_token("lauz") in server.access
    finally:
        server.close()


def test_unknown_and_expired_state_are_refused(client, fake, monkeypatch):
    assert client.get("/oauth/mcp/callback?code=x&state=nope").status_code == 400
    callback = fake.authorize(connect(client, fake))
    monkeypatch.setattr(mcp_oauth, "FLOW_LIFETIME", -1)
    assert client.get(callback).status_code == 400
    assert client.get("/api/extensions/servers", headers=HEADERS).json()["servers"][0]["state"] != "connected"


def test_denied_sign_in_shows_error_and_retry_works(client, fake):
    auth_url = connect(client, fake)
    state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(auth_url).query))["state"]
    r = client.get(f"/oauth/mcp/callback?error=access_denied&state={state}")
    assert r.status_code == 400 and "access_denied" in r.text
    server = client.get("/api/extensions/servers", headers=HEADERS).json()["servers"][0]
    assert server["state"] == "error"
    auth_url = client.post("/api/extensions/servers/lauz/connect", headers=HEADERS).json()["auth_url"]
    assert client.get(fake.authorize(auth_url)).status_code == 200
    assert client.get("/api/extensions/servers", headers=HEADERS).json()["servers"][0]["state"] == "connected"


def test_wrong_iss_is_refused(client, fake):
    callback = fake.authorize(connect(client, fake)).replace("iss=", "iss=https%3A%2F%2Fevil.test&x=")
    assert client.get(callback).status_code == 400


def test_open_url_only_accepts_the_pending_sign_in(client, fake, monkeypatch):
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url, new=0: opened.append(url) or True)
    auth_url = connect(client, fake)
    bad = client.post("/api/extensions/servers/lauz/open", json={"url": "https://evil.test/x"}, headers=HEADERS)
    assert bad.status_code == 409
    ok = client.post("/api/extensions/servers/lauz/open", json={"url": auth_url}, headers=HEADERS)
    assert ok.status_code == 200 and opened == [auth_url]


def test_routes_need_the_session_token_except_the_callback(client, fake):
    assert client.post("/api/extensions/servers/lauz/connect").status_code in (401, 403)
    assert client.get("/oauth/mcp/callback?state=x").status_code == 400


# ----------------------------------------------------------------- refresh and revoke

def test_refresh_before_expiry_and_invalid_grant_needs_a_new_sign_in(client, fake):
    client.get(fake.authorize(connect(client, fake)))
    first = mcp_oauth.access_token("lauz")
    mcp_oauth.update_entry("lauz", expires_at=int(time.time()) + 30)
    second = mcp_oauth.access_token("lauz")
    assert second != first and second in fake.access
    assert fake.token_requests[-1]["grant_type"] == "refresh_token"
    assert fake.token_requests[-1]["resource"] == fake.base + "/mcp"

    fake.refresh.clear()                                          # the server forgot the refresh token
    mcp_oauth.update_entry("lauz", expires_at=int(time.time()) - 10)
    assert mcp_oauth.access_token("lauz") is None
    assert client.get("/api/extensions/servers", headers=HEADERS).json()["servers"][0]["state"] == "needs_auth"


def test_background_refresh_renews_tokens_close_to_expiry(client, fake):
    client.get(fake.authorize(connect(client, fake)))
    old = mcp_oauth.secret_get("lauz:access")
    mcp_oauth.update_entry("lauz", expires_at=int(time.time()) + 200)
    mcp_oauth.refresh_due()
    assert mcp_oauth.secret_get("lauz:access") != old


def test_revoke_calls_the_server_and_forgets_the_tokens(client, fake, store):
    client.get(fake.authorize(connect(client, fake)))
    r = client.post("/api/extensions/servers/lauz/logout", headers=HEADERS)
    assert r.status_code == 200 and r.json()["state"] == "needs_auth"
    assert fake.revoked == ["refresh_token", "access_token"]
    assert not [k for k in store.store if k[1].startswith("lauz:access") or k[1].startswith("lauz:refresh")]


# ----------------------------------------------------------------- discovery edge cases

def test_open_server_needs_no_sign_in(client, monkeypatch):
    monkeypatch.setattr(mcp_oauth, "probe", lambda url, token=None: mcp_oauth.Response(200, {}, b"{}"))
    r = client.post("/api/extensions/servers", json={"name": "open", "url": "http://127.0.0.1:1/mcp"},
                    headers=HEADERS)
    assert r.json()["state"] == "connected" and r.json()["auth"] is False


def test_without_dynamic_registration_the_error_says_so(client, store):
    server = FakeAuth(registration=False)
    try:
        client.post("/api/extensions/servers", json={"name": "lauz", "url": server.base + "/mcp"}, headers=HEADERS)
        r = client.post("/api/extensions/servers/lauz/connect", headers=HEADERS)
        assert r.status_code == 400 and "dynamic registration" in r.json()["detail"]
    finally:
        server.close()


def test_issuer_path_variant_is_found(fake, store):
    meta = mcp_oauth.authorization_server_metadata(fake.issuer)
    assert meta["token_endpoint"] == fake.issuer + "/token"


def test_www_authenticate_parsing():
    params = mcp_oauth.parse_www_authenticate(
        'Bearer error="invalid_token", error_description="Missing Authorization header", '
        'resource_metadata="https://mcp.lauz.io/.well-known/oauth-protected-resource/mcp", scope="a b"')
    assert params["resource_metadata"].endswith("/oauth-protected-resource/mcp") and params["scope"] == "a b"


def test_url_normalization():
    assert mcp_auth.normalize_url("  HTTPS://MCP.Lauz.IO/mcp/#x ") == "https://mcp.lauz.io/mcp"
    with pytest.raises(mcp_auth.ServiceError):
        mcp_auth.normalize_url("http://mcp.lauz.io/mcp")


# ----------------------------------------------------------------- delivery to the engines

def test_claude_gets_a_headers_helper_and_no_token_on_disk(client, fake, capsys):
    client.get(fake.authorize(connect(client, fake)))
    path = launch.write_mcp_config()
    text = open(path, encoding="utf-8").read()
    entry = json.loads(text)["mcpServers"]["lauz"]
    assert entry["type"] == "http" and "--mcp lauz" in entry["headersHelper"] and "engine_key.py" in entry["headersHelper"]
    assert not any(t in text for t in fake.issued)
    assert engine_key.main(["--mcp", "lauz"]) == 0
    header = json.loads(capsys.readouterr().out)
    assert header["Authorization"][7:] in fake.access


def test_server_that_needs_a_sign_in_is_left_out_of_every_engine(client, fake):
    connect(client, fake)
    assert "lauz" not in json.loads(open(launch.write_mcp_config()).read())["mcpServers"]
    from hokma.engines import remote_mcp
    assert remote_mcp.ready_servers() == [] and remote_mcp.acp_servers() == []


def test_codex_gets_the_token_by_environment_only(client, fake, monkeypatch, tmp_path):
    client.get(fake.authorize(connect(client, fake)))
    from hokma.engines import codex, remote_mcp
    from hokma.engines.base import EngineContext
    specs, env = remote_mcp.codex_servers()
    var = specs["lauz"]["bearer_token_env_var"]
    assert var.startswith("HOKMA_MCP_TOKEN_LAUZ_") and env[var] in fake.access
    assert remote_mcp.env_var("a-b") != remote_mcp.env_var("a_b")
    monkeypatch.setattr(codex, "codex_home_dir", lambda: tmp_path / "codex")
    ctx = EngineContext.__new__(EngineContext)
    ctx.tier, ctx.instructions = "balanced", ""
    home = codex.write_config(ctx, specs)
    config = (home / "config.toml").read_text()
    assert f'bearer_token_env_var = "{var}"' in config and 'exclude = ["HOKMA_MCP_TOKEN_*"]' in config
    assert not any(t in config for t in fake.issued)


def test_the_token_does_not_follow_the_name_to_another_address(client, fake):
    client.get(fake.authorize(connect(client, fake)))
    extensions.set_mcp("lauz", {"type": "http", "url": "http://127.0.0.1:1/other"})   # e.g. an approved agent request
    from hokma.engines import remote_mcp
    assert remote_mcp.ready_servers() == []
    assert "lauz" not in json.loads(open(launch.write_mcp_config()).read())["mcpServers"]
    assert mcp_auth.server_state("lauz")["state"] == "needs_auth"
    revoked = list(fake.revoked)
    with pytest.raises(mcp_oauth.OAuthError):          # Connect against the new address: the old token is wiped first
        mcp_oauth.start("lauz", "http://127.0.0.1:1/other", "http://127.0.0.1:80/oauth/mcp/callback")
    assert not mcp_oauth.has_tokens("lauz") and fake.revoked == revoked


def test_gemini_gets_an_authorization_header(client, fake):
    client.get(fake.authorize(connect(client, fake)))
    from hokma.engines import remote_mcp
    [server] = remote_mcp.acp_servers()
    assert server["type"] == "http" and server["headers"][0]["value"][7:] in fake.access


def test_direct_engine_calls_remote_tools_through_the_broker_name(client, fake):
    client.get(fake.authorize(connect(client, fake)))
    from hokma.engines import remote_mcp
    [box] = remote_mcp.remote_toolboxes()
    assert [d["name"] for d in box.definitions()] == ["lauz__accounts"]
    assert box.broker_request("lauz__accounts", {}) == ("mcp__lauz__accounts", {})
    assert box.run("lauz__accounts", {}) == ("two accounts", False)
    assert box.client.session == "sess-1"


# ----------------------------------------------------------------- live smoke test (discovery + registration only)

@pytest.mark.skipif(os.environ.get("HOKMA_LAUZ_SMOKE") != "1", reason="set HOKMA_LAUZ_SMOKE=1 to reach mcp.lauz.io")
def test_lauz_discovery_and_registration_live():
    meta = mcp_oauth.discover("https://mcp.lauz.io/mcp")
    assert meta["auth"] and meta["issuer"] == "https://api.lauz.io/oauth"
    assert meta["registration_endpoint"] == "https://api.lauz.io/oauth/reg"
    assert "offline_access" in meta["scopes"]
    client = mcp_oauth.register_client(meta, "http://127.0.0.1:8770/oauth/mcp/callback")
    assert client["client_id"] and client["auth_method"] == "none"

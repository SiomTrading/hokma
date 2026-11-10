"""Remote MCP servers with OAuth: add, authorize on a pseudo-terminal, states, revoke. `claude` is simulated."""
import json
import os
import stat
import sys

import pytest
from fastapi.testclient import TestClient

from dbsupport import db, env  # noqa: F401  (fixtures)
from hokma.core.app import create_app
from hokma.kit import extensions, launch
from hokma.rpc.ws import SESSION_TOKEN
from hokma.setup import claude as setup_claude
from hokma.status import mcp_auth

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX pty fake; Windows uses pywinpty")

HEADERS = {"X-Hokma-Token": SESSION_TOKEN}
URL = "https://mcp.example.test/mcp"
SECRET_CODE = "SECRETCODE0123456789abcdefghijklmnopqrstuvwxyz"
REDIRECT = f"http://localhost:51234/callback?code={SECRET_CODE}&state=STATE123"

FAKE = """#!{python}
import json, os, sys
cfg = os.environ["CLAUDE_CONFIG_DIR"]
state = os.path.join(cfg, "fake-state")
args = sys.argv[1:]
if args[:2] == ["mcp", "add"]:
    name, url = args[-2], args[-1]
    json.dump({{"mcpServers": {{name: {{"type": "http", "url": url}}}}}}, open(os.path.join(cfg, ".claude.json"), "w"))
elif args[:2] == ["mcp", "get"]:
    on = os.path.exists(state)
    print("Status: " + ("\\u2713 Connected" if on else "! Needs authentication"))
elif args[:2] == ["mcp", "login"]:
    if not sys.stdin.isatty():
        print("stdin isn't a terminal"); sys.exit(1)
    print("Visit this URL to authorize:")
    print("  \\x1b]8;;https://auth.example.test/oauth?state=ZZZ\\x1b\\\\https://auth.example.test/oauth?state=ZZZ\\x1b]8;;\\x1b\\\\")
    sys.stdout.flush()
    line = sys.stdin.readline().strip()
    if "code=" in line:
        open(state, "w").write("on"); sys.exit(0)
    print("Invalid redirect " + line); sys.exit(1)
elif args[:2] == ["mcp", "logout"]:
    if os.path.exists(state):
        os.remove(state)
"""


@pytest.fixture
def client(db, tmp_path, monkeypatch):  # noqa: F811
    os.makedirs(os.environ["HOKMA_HOME"], exist_ok=True)
    fake = tmp_path / "bin" / "claude"
    fake.parent.mkdir()
    fake.write_text(FAKE.format(python=sys.executable), encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(mcp_auth, "claude_path", lambda: str(fake))
    monkeypatch.setattr(mcp_auth, "LINK_WAIT", 8)
    monkeypatch.setattr(mcp_auth, "LOGIN_WAIT", 8)
    yield TestClient(create_app(), headers=HEADERS)
    mcp_auth.stop_login()


def add(client, name="lauz", url=URL):
    return client.post("/api/extensions/servers", json={"name": name, "url": url})


def test_add_registers_the_server_and_records_an_approval(client, db):  # noqa: F811
    res = add(client)
    assert res.status_code == 201
    assert extensions.load()[0]["mcpServers"]["lauz"] == {"type": "http", "url": URL}
    with db.read() as u:
        assert [(r.name, r.status) for r in u.extension_requests.list(status="applied")] == [("lauz", "applied")]
    assert client.get("/api/extensions/servers").json()["servers"][0]["state"] == "needs_auth"


@pytest.mark.parametrize("name,url", [("hokma", URL), ("Bad Name", URL), ("-x", URL), ("lauz", "http://x.test/mcp"),
                                      ("lauz", "https://a b")])
def test_add_rejects_bad_input(client, name, url):
    assert add(client, name, url).status_code in (400, 422)
    assert "lauz" not in extensions.load()[0]["mcpServers"]


def test_duplicate_name_is_refused(client):
    assert add(client).status_code == 201
    assert add(client).status_code == 409


def test_registered_remote_server_is_left_out_of_the_mcp_config(client):
    add(client)
    servers = json.loads(open(launch.write_mcp_config(), encoding="utf-8").read())["mcpServers"]
    assert "lauz" not in servers and "hokma" in servers


def test_login_returns_a_clean_link_then_the_pasted_redirect_connects(client, db):  # noqa: F811
    add(client)
    link = client.post("/api/extensions/servers/lauz/login").json()["auth_url"]
    assert link == "https://auth.example.test/oauth?state=ZZZ"          # one link, no terminal hyperlink codes
    done = client.post("/api/extensions/servers/lauz/login/code", json={"pasted": REDIRECT})
    assert done.status_code == 200 and done.json()["state"] == "connected"
    with db.read() as u:                                                 # the token never reaches the database
        assert SECRET_CODE not in repr(u.extension_requests.list()) + repr(u.extensions.list())
    assert SECRET_CODE not in done.text


def test_garbage_gives_an_error_without_secrets_and_retry_works(client):
    add(client)
    client.post("/api/extensions/servers/lauz/login")
    bad = client.post("/api/extensions/servers/lauz/login/code", json={"pasted": "garbage"})
    assert bad.status_code == 400 and "garbage" in bad.json()["detail"]
    client.post("/api/extensions/servers/lauz/login")
    ok = client.post("/api/extensions/servers/lauz/login/code", json={"pasted": REDIRECT})
    assert ok.json()["state"] == "connected"


def test_pasting_without_a_login_in_progress_is_refused(client):
    add(client)
    res = client.post("/api/extensions/servers/lauz/login/code", json={"pasted": REDIRECT})
    assert res.status_code == 409


def test_revoke_returns_to_needs_authorization(client):
    add(client)
    client.post("/api/extensions/servers/lauz/login")
    client.post("/api/extensions/servers/lauz/login/code", json={"pasted": REDIRECT})
    res = client.post("/api/extensions/servers/lauz/logout")
    assert res.status_code == 200 and res.json()["state"] == "needs_auth"
    assert "lauz" in extensions.load()[0]["mcpServers"]


def test_unknown_server_and_bad_name_in_the_path(client):
    assert client.post("/api/extensions/servers/nope/login").status_code == 404
    assert client.post("/api/extensions/servers/NOPE!/login").status_code == 422


def test_routes_need_the_token(client):
    bare = TestClient(create_app())
    assert bare.get("/api/extensions/servers").status_code in (401, 403)


def test_clean_output_hides_redirect_secrets():
    shown = setup_claude._clean_output(f"Invalid {REDIRECT} token {SECRET_CODE}")
    assert SECRET_CODE not in shown and "STATE123" not in shown


def test_the_module_never_logs_or_stores_tokens():
    source = open(mcp_auth.__file__, encoding="utf-8").read()
    assert "logging" not in source and "print(" not in source and ".credentials" not in source

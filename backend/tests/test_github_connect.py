"""GitHub connection (P16): device flow, token in the credential store, private repository. No real network:
`hokma.update.github._open` is replaced by a scripted fake."""
import json
import urllib.error

import keyring
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hokma.github_connect import flow
from hokma.github_connect.api import router
from hokma.i18n import catalog
from hokma.rpc.ws import SESSION_TOKEN
from hokma.update import github
from live_support import MemoryKeyring

AUTH = {"X-Hokma-Token": SESSION_TOKEN}
CODE = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"
USER = "https://api.github.com/user"
SECRET = "gho_secret_value"


class Resp:
    def __init__(self, body, status=200):
        self.body, self.status = json.dumps(body).encode(), status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return self.body


class Net:
    """Answers by (method, url); a list is consumed in order; an int is an HTTP error status."""

    def __init__(self, routes):
        self.routes, self.requests = routes, []

    def __call__(self, request):
        self.requests.append(request)
        answer = self.routes[(request.get_method(), request.full_url)]
        if isinstance(answer, list):
            answer = answer.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, int):
            raise urllib.error.HTTPError(request.full_url, answer, "err", {}, None)
        return Resp(answer)


@pytest.fixture(autouse=True)
def store(monkeypatch):
    previous = keyring.get_keyring()
    memory = MemoryKeyring()
    keyring.set_keyring(memory)
    flow._pending.clear()
    yield memory
    keyring.set_keyring(previous)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def net(monkeypatch, routes):
    fake = Net(routes)
    monkeypatch.setattr(github, "_open", fake)
    return fake


DEVICE = {"device_code": "dc", "user_code": "ABCD-1234", "verification_uri": "https://github.com/login/device",
          "expires_in": 900, "interval": 5}


def test_routes_need_the_session_token(client):
    assert client.post("/api/github/connect").status_code == 401
    assert client.get("/api/github/connect").status_code == 401
    assert client.get("/api/github").status_code == 401
    assert client.delete("/api/github").status_code == 401


def test_start_returns_the_code_and_sends_the_public_client_id(client, monkeypatch):
    fake = net(monkeypatch, {("POST", CODE): DEVICE})
    body = client.post("/api/github/connect", headers=AUTH).json()
    assert body == {"user_code": "ABCD-1234", "verification_uri": DEVICE["verification_uri"], "interval": 5,
                    "expires_in": 900}
    sent = fake.requests[0].data.decode()
    assert "client_id=Ov23ctVfAiSnfjdeYKpW" in sent and "scope=repo" in sent


def test_poll_without_start_is_none(client):
    assert client.get("/api/github/connect", headers=AUTH).json() == {"status": "none"}


def test_pending_then_connected_saves_the_token_and_never_echoes_it(client, monkeypatch, store):
    net(monkeypatch, {("POST", CODE): DEVICE,
                      ("POST", TOKEN_URL): [{"error": "authorization_pending"}, {"error": "slow_down", "interval": 10},
                                            {"access_token": SECRET}],
                      ("GET", USER): {"login": "ana"}})
    client.post("/api/github/connect", headers=AUTH)
    assert client.get("/api/github/connect", headers=AUTH).json() == {"status": "pending", "interval": 5}
    assert client.get("/api/github/connect", headers=AUTH).json() == {"status": "pending", "interval": 10}
    done = client.get("/api/github/connect", headers=AUTH)
    assert done.json() == {"status": "connected", "user": "ana"} and SECRET not in done.text
    assert store.items[("hokma-market", "github")] == SECRET
    assert github.github_token() == SECRET                      # the update source now uses it first
    assert client.get("/api/github/connect", headers=AUTH).json() == {"status": "none"}


@pytest.mark.parametrize("error,status", [("access_denied", "denied"), ("expired_token", "expired")])
def test_denied_and_expired_end_the_flow(client, monkeypatch, store, error, status):
    net(monkeypatch, {("POST", CODE): DEVICE, ("POST", TOKEN_URL): {"error": error}})
    client.post("/api/github/connect", headers=AUTH)
    assert client.get("/api/github/connect", headers=AUTH).json() == {"status": status}
    assert not store.items


def test_status_connected_and_disconnect(client, monkeypatch, store):
    store.items[("hokma-market", "github")] = SECRET
    fake = net(monkeypatch, {("GET", USER): {"login": "ana"}})
    assert client.get("/api/github", headers=AUTH).json() == {"connected": True, "user": "ana", "store_available": True, "offline": False, "pending": None}
    assert fake.requests[0].get_header("Authorization") == f"Bearer {SECRET}"
    assert client.delete("/api/github", headers=AUTH).json() == {"connected": False}
    assert not store.items
    assert client.get("/api/github", headers=AUTH).json()["connected"] is False


def test_status_says_offline_when_a_token_exists_but_there_is_no_network(client, monkeypatch, store):
    store.items[("hokma-market", "github")] = SECRET
    net(monkeypatch, {("GET", USER): urllib.error.URLError("down")})
    body = client.get("/api/github", headers=AUTH).json()
    assert body["connected"] is False and body["offline"] is True
    assert store.items                                       # the token is kept: it is the network that failed


def test_status_without_token_and_without_network_is_just_not_connected(client, monkeypatch, store):
    net(monkeypatch, {})
    body = client.get("/api/github", headers=AUTH).json()
    assert body["connected"] is False and body["offline"] is False


def test_a_revoked_token_is_dropped(client, monkeypatch, store):
    store.items[("hokma-market", "github")] = SECRET
    net(monkeypatch, {("GET", USER): 401})
    assert client.get("/api/github", headers=AUTH).json()["connected"] is False
    assert not store.items


def test_unreachable_github_is_a_catalog_error(client, monkeypatch):
    net(monkeypatch, {("POST", CODE): urllib.error.URLError("down")})
    response = client.post("/api/github/connect", headers=AUTH)
    assert response.status_code == 502
    assert response.json()["detail"] == {"code": "github.unreachable", "message": catalog("es")["github.unreachable"]}


def test_ensure_private_repo_creates_when_missing(monkeypatch, store):
    store.items[("hokma-market", "github")] = SECRET
    fake = net(monkeypatch, {("GET", USER): {"login": "ana"},
                             ("GET", "https://api.github.com/repos/ana/mi-proyecto"): 404,
                             ("POST", "https://api.github.com/user/repos"): {"full_name": "ana/mi-proyecto",
                                                                             "private": True}})
    assert flow.ensure_private_repo("mi-proyecto")["full_name"] == "ana/mi-proyecto"
    assert json.loads(fake.requests[-1].data) == {"name": "mi-proyecto", "private": True}


def test_ensure_private_repo_reuses_the_existing_one(monkeypatch, store):
    store.items[("hokma-market", "github")] = SECRET
    fake = net(monkeypatch, {("GET", USER): {"login": "ana"},
                             ("GET", "https://api.github.com/repos/ana/x"): {"full_name": "ana/x", "private": True}})
    assert flow.ensure_private_repo("x")["full_name"] == "ana/x"
    assert [r.get_method() for r in fake.requests] == ["GET", "GET"]


@pytest.mark.parametrize("body", [{"full_name": "ana/x", "private": False}, {"full_name": "ana/x"}])
def test_ensure_private_repo_refuses_an_existing_repository_that_is_not_private(monkeypatch, store, body):
    store.items[("hokma-market", "github")] = SECRET
    net(monkeypatch, {("GET", USER): {"login": "ana"}, ("GET", "https://api.github.com/repos/ana/x"): body})
    with pytest.raises(flow.GitHubError) as info:
        flow.ensure_private_repo("x")
    assert info.value.code == "github.repo_failed"


def test_ensure_private_repo_needs_a_connection(monkeypatch):
    net(monkeypatch, {})
    with pytest.raises(flow.GitHubError) as caught:
        flow.ensure_private_repo("x")
    assert caught.value.code == "github.not_connected"


def test_the_app_mounts_the_routes():
    from hokma.core.app import create_app
    paths = {r.path for r in create_app().routes}
    assert {"/api/github", "/api/github/connect"} <= paths


def test_the_catalogs_carry_every_message():
    for code in ("github.not_connected", "github.unreachable", "github.store_unavailable", "github.repo_failed"):
        assert code in catalog("es") and code in catalog("en")


@pytest.mark.parametrize("status", [500, 502, 503, 403, 429])
def test_a_github_failure_is_unreachable_and_the_connection_stays(client, monkeypatch, store, status):
    store.items[("hokma-market", "github")] = SECRET
    net(monkeypatch, {("GET", USER): status})
    with pytest.raises(flow.GitHubError) as caught:
        flow.user()
    assert caught.value.code == "github.unreachable"
    assert store.items                                       # the token is kept: only a 401 means disconnected
    body = client.get("/api/github", headers=AUTH).json()
    assert body["connected"] is False and body["offline"] is True


@pytest.mark.parametrize("status", [500, 403, 429])
def test_ensure_private_repo_says_unreachable_not_connect_first_when_github_fails(monkeypatch, store, status):
    store.items[("hokma-market", "github")] = SECRET
    net(monkeypatch, {("GET", USER): status})
    with pytest.raises(flow.GitHubError) as caught:
        flow.ensure_private_repo("x")
    assert caught.value.code == "github.unreachable" and store.items


def test_a_401_is_the_only_status_that_disconnects(monkeypatch, store):
    store.items[("hokma-market", "github")] = SECRET
    net(monkeypatch, {("GET", USER): 401})
    assert flow.user() is None and not store.items
    with pytest.raises(flow.GitHubError) as caught:
        flow.ensure_private_repo("x")
    assert caught.value.code == "github.not_connected"


def test_the_open_flow_is_exposed_so_a_reload_does_not_lose_the_code(client, monkeypatch, store):
    net(monkeypatch, {("POST", CODE): DEVICE})
    assert client.get("/api/github", headers=AUTH).json()["pending"] is None
    client.post("/api/github/connect", headers=AUTH)
    pending = client.get("/api/github", headers=AUTH).json()["pending"]
    assert pending["user_code"] == "ABCD-1234" and pending["verification_uri"] == DEVICE["verification_uri"]
    assert pending["interval"] == 5 and 0 < pending["expires_in"] <= 900
    client.delete("/api/github", headers=AUTH)
    assert client.get("/api/github", headers=AUTH).json()["pending"] is None


def test_an_expired_flow_is_not_exposed(client, monkeypatch, store):
    net(monkeypatch, {("POST", CODE): DEVICE})
    client.post("/api/github/connect", headers=AUTH)
    flow._pending["expires"] = 0
    assert client.get("/api/github", headers=AUTH).json()["pending"] is None


def test_errors_follow_the_language_of_the_panel(client, monkeypatch):
    net(monkeypatch, {("POST", CODE): urllib.error.URLError("down")})
    english = client.post("/api/github/connect", headers={**AUTH, "Accept-Language": "en"})
    spanish = client.post("/api/github/connect", headers={**AUTH, "Accept-Language": "es-CO,es;q=0.9"})
    assert english.json()["detail"]["message"] == catalog("en")["github.unreachable"]
    assert spanish.json()["detail"]["message"] == catalog("es")["github.unreachable"]


def test_the_scope_is_documented_as_the_smallest_possible():
    assert flow.SCOPE == "repo"

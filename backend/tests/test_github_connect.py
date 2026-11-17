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
SECRET = "ghu_secret_value"
REPO = "ana/hokma-respaldo"


def connect(store, repo=None):
    """A connection made by the GitHub App (the only kind that is kept), optionally with its repository picked."""
    store.items[("hokma-market", "github")] = SECRET
    store.items[("hokma-market", "github-app")] = flow.CLIENT_ID
    if repo:
        store.items[("hokma-market", "github-repo")] = repo


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
    assert f"client_id={flow.CLIENT_ID}" in sent and "scope" not in sent      # a GitHub App takes no scope


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
    assert store.items[("hokma-market", "github-app")] == flow.CLIENT_ID
    assert github.github_token() == SECRET                      # the update source now uses it first
    assert client.get("/api/github/connect", headers=AUTH).json() == {"status": "none"}


@pytest.mark.parametrize("error,status", [("access_denied", "denied"), ("expired_token", "expired")])
def test_denied_and_expired_end_the_flow(client, monkeypatch, store, error, status):
    net(monkeypatch, {("POST", CODE): DEVICE, ("POST", TOKEN_URL): {"error": error}})
    client.post("/api/github/connect", headers=AUTH)
    assert client.get("/api/github/connect", headers=AUTH).json() == {"status": status}
    assert not store.items


def test_status_connected_and_disconnect(client, monkeypatch, store):
    connect(store)
    fake = net(monkeypatch, {("GET", USER): {"login": "ana"}})
    assert client.get("/api/github", headers=AUTH).json() == {"connected": True, "user": "ana", "repo": None, "legacy": False, "store_available": True, "offline": False, "pending": None, "install_url": flow.INSTALL_URL, "new_repo_url": flow.NEW_REPO_URL, "legacy_url": flow.LEGACY_APP_URL}
    assert fake.requests[0].get_header("Authorization") == f"Bearer {SECRET}"
    assert client.delete("/api/github", headers=AUTH).json() == {"connected": False}
    assert not store.items
    assert client.get("/api/github", headers=AUTH).json()["connected"] is False


def test_status_says_offline_when_a_token_exists_but_there_is_no_network(client, monkeypatch, store):
    connect(store)
    net(monkeypatch, {("GET", USER): urllib.error.URLError("down")})
    body = client.get("/api/github", headers=AUTH).json()
    assert body["connected"] is False and body["offline"] is True
    assert store.items                                       # the token is kept: it is the network that failed


def test_status_without_token_and_without_network_is_just_not_connected(client, monkeypatch, store):
    net(monkeypatch, {})
    body = client.get("/api/github", headers=AUTH).json()
    assert body["connected"] is False and body["offline"] is False


def test_a_revoked_token_is_dropped(client, monkeypatch, store):
    connect(store)
    net(monkeypatch, {("GET", USER): 401})
    assert client.get("/api/github", headers=AUTH).json()["connected"] is False
    assert not store.items


def test_unreachable_github_is_a_catalog_error(client, monkeypatch):
    net(monkeypatch, {("POST", CODE): urllib.error.URLError("down")})
    response = client.post("/api/github/connect", headers=AUTH)
    assert response.status_code == 502
    assert response.json()["detail"] == {"code": "github.unreachable", "message": catalog("es")["github.unreachable"]}


INSTALLS = "https://api.github.com/user/installations?per_page=100&page=1"
LISTING = "https://api.github.com/user/installations/7/repositories?per_page=100&page=1"
SELECTED = {"installations": [{"id": 7}]}
REPOS = {"repositories": [{"full_name": "ana/hokma-respaldo", "private": True},
                          {"full_name": "ana/publico", "private": False}]}


def test_repos_lists_only_the_private_repositories_of_the_installation(client, monkeypatch, store):
    connect(store)
    fake = net(monkeypatch, {("GET", INSTALLS): SELECTED, ("GET", LISTING): REPOS})
    assert client.get("/api/github/repos", headers=AUTH).json() == [{"full_name": REPO, "private": True}]
    assert fake.requests[0].get_header("Authorization") == f"Bearer {SECRET}"


def _listing(installation, page, repos):
    return ("GET", f"https://api.github.com/user/installations/{installation}/repositories?per_page=100&page={page}"), \
        {"repositories": [{"full_name": r, "private": True} for r in repos]}


def test_repos_joins_several_installations_without_duplicates(client, monkeypatch, store):
    connect(store)
    routes = {("GET", INSTALLS): {"installations": [{"id": 7}, {"id": 9}]}}
    for key, body in (_listing(7, 1, ["ana/b", "ana/a"]), _listing(9, 1, ["org/c", "ana/a"])):
        routes[key] = body
    net(monkeypatch, routes)
    assert [r["full_name"] for r in client.get("/api/github/repos", headers=AUTH).json()] == ["ana/a", "ana/b", "org/c"]


def test_repos_reads_every_page_of_repositories(client, monkeypatch, store):
    connect(store)
    routes = {("GET", INSTALLS): SELECTED}
    first = [f"ana/r{i:03d}" for i in range(100)]
    for key, body in (_listing(7, 1, first), _listing(7, 2, ["ana/zz-last"])):
        routes[key] = body
    fake = net(monkeypatch, routes)
    names = [r["full_name"] for r in client.get("/api/github/repos", headers=AUTH).json()]
    assert len(names) == 101 and names[-1] == "ana/zz-last"
    assert fake.requests[-1].full_url.endswith("/repositories?per_page=100&page=2")


def test_repos_reads_every_page_of_installations(client, monkeypatch, store):
    connect(store)
    routes = {("GET", INSTALLS): {"installations": [{"id": i} for i in range(1, 101)]},
              ("GET", "https://api.github.com/user/installations?per_page=100&page=2"): {"installations": [{"id": 101}]}}
    for i in range(1, 102):
        key, body = _listing(i, 1, ["ana/last"] if i == 101 else [])
        routes[key] = body
    net(monkeypatch, routes)
    assert [r["full_name"] for r in client.get("/api/github/repos", headers=AUTH).json()] == ["ana/last"]


def test_repos_with_a_rejected_token_disconnects_and_asks_to_connect(client, monkeypatch, store):
    connect(store, REPO)
    net(monkeypatch, {("GET", INSTALLS): 401})
    response = client.get("/api/github/repos", headers=AUTH)
    assert response.status_code == 409 and response.json()["detail"]["code"] == "github.not_connected"
    assert not store.items


def test_repos_says_unreachable_when_github_fails(client, monkeypatch, store):
    connect(store)
    net(monkeypatch, {("GET", INSTALLS): 503})
    assert client.get("/api/github/repos", headers=AUTH).status_code == 502


def test_repos_is_empty_until_the_app_is_installed(client, monkeypatch, store):
    connect(store)
    net(monkeypatch, {("GET", INSTALLS): {"installations": []}})
    assert client.get("/api/github/repos", headers=AUTH).json() == []


def test_pick_a_repository_saves_it_and_the_state_shows_it(client, monkeypatch, store):
    connect(store)
    net(monkeypatch, {("GET", INSTALLS): SELECTED, ("GET", LISTING): REPOS, ("GET", USER): {"login": "ana"}})
    assert client.put("/api/github/repo", json={"full_name": REPO}, headers=AUTH).json() == {"repo": REPO}
    assert store.items[("hokma-market", "github-repo")] == REPO
    assert client.get("/api/github", headers=AUTH).json()["repo"] == REPO


@pytest.mark.parametrize("name", ["ana/publico", "ana/otro-repo", "victima/privado"])
def test_a_repository_outside_the_installation_cannot_be_picked(client, monkeypatch, store, name):
    connect(store)
    net(monkeypatch, {("GET", INSTALLS): SELECTED, ("GET", LISTING): REPOS})
    response = client.put("/api/github/repo", json={"full_name": name}, headers=AUTH)
    assert response.status_code == 400 and response.json()["detail"]["code"] == "github.repo_failed"
    assert ("hokma-market", "github-repo") not in store.items


def test_change_repository_clears_only_the_repository(client, store):
    connect(store, REPO)
    assert client.delete("/api/github/repo", headers=AUTH).json() == {"repo": None}
    assert ("hokma-market", "github-repo") not in store.items and ("hokma-market", "github") in store.items


def test_repo_routes_need_the_session_token(client):
    assert client.get("/api/github/repos").status_code == 401
    assert client.put("/api/github/repo", json={"full_name": REPO}).status_code == 401
    assert client.delete("/api/github/repo").status_code == 401


def test_a_new_connection_asks_for_the_repository_again(client, monkeypatch, store):
    connect(store, "ana/viejo")
    store.items[("hokma-market", "github-legacy")] = "1"
    net(monkeypatch, {("POST", CODE): DEVICE, ("POST", TOKEN_URL): {"access_token": SECRET},
                      ("GET", USER): {"login": "ana"}})
    client.post("/api/github/connect", headers=AUTH)
    client.get("/api/github/connect", headers=AUTH)
    assert ("hokma-market", "github-repo") not in store.items and ("hokma-market", "github-legacy") not in store.items


def test_an_old_all_repositories_token_is_dropped_and_the_student_is_told(client, monkeypatch, store):
    store.items[("hokma-market", "github")] = "gho_old_oauth_token"                 # no `github-app` marker
    store.items[("hokma-market", "github-repo")] = "ana/hokma-backup"
    fake = net(monkeypatch, {})
    body = client.get("/api/github", headers=AUTH).json()
    assert body["connected"] is False and body["legacy"] is True and body["repo"] is None
    assert body["legacy_url"] == flow.LEGACY_APP_URL
    assert ("hokma-market", "github") not in store.items and not fake.requests      # dropped without using it
    assert flow.token() is None
    with pytest.raises(flow.GitHubError) as caught:
        flow.connected_repo()
    assert caught.value.code == "github.not_connected"


def test_the_updater_never_receives_the_old_token(monkeypatch, store):
    store.items[("hokma-market", "github")] = "gho_old_oauth_token"
    monkeypatch.setattr(github, "_token_from_git", lambda: None)
    monkeypatch.setattr(github, "_token_from_gh", lambda: None)
    assert github.github_token() is None
    assert ("hokma-market", "github") not in store.items and ("hokma-market", "github-legacy") in store.items


def test_the_updater_gets_the_token_of_the_app(monkeypatch, store):
    connect(store)
    assert github.github_token() == SECRET


def test_start_up_drops_the_old_token(store):
    from hokma.core.app import create_app
    from fastapi.testclient import TestClient
    store.items[("hokma-market", "github")] = "gho_old_oauth_token"
    with TestClient(create_app(auto_update_check=False)):
        pass
    assert flow.token() is None and ("hokma-market", "github") not in store.items


def test_disconnect_forgets_the_repository_and_the_notice(store):
    connect(store, REPO)
    store.items[("hokma-market", "github-legacy")] = "1"
    flow.disconnect()
    assert not store.items


def test_connected_repo_returns_the_picked_private_repository(monkeypatch, store):
    connect(store, REPO)
    net(monkeypatch, {("GET", f"https://api.github.com/repos/{REPO}"): {"full_name": REPO, "private": True}})
    assert flow.connected_repo()["full_name"] == REPO


def test_connected_repo_never_creates_a_repository(monkeypatch, store):
    connect(store, REPO)
    fake = net(monkeypatch, {("GET", f"https://api.github.com/repos/{REPO}"): {"full_name": REPO, "private": True}})
    flow.connected_repo()
    assert [r.get_method() for r in fake.requests] == ["GET"]


def test_connected_repo_without_a_pick_asks_for_one(monkeypatch, store):
    connect(store)
    net(monkeypatch, {})
    with pytest.raises(flow.GitHubError) as caught:
        flow.connected_repo()
    assert caught.value.code == "github.no_repo"


def test_connected_repo_that_disappeared_says_so_and_keeps_the_connection(monkeypatch, store):
    connect(store, REPO)
    net(monkeypatch, {("GET", f"https://api.github.com/repos/{REPO}"): 404})
    with pytest.raises(flow.GitHubError) as caught:
        flow.connected_repo()
    assert caught.value.code == "github.repo_gone" and ("hokma-market", "github") in store.items


@pytest.mark.parametrize("body", [{"full_name": REPO, "private": False}, {"full_name": REPO}])
def test_connected_repo_refuses_a_repository_that_is_not_private(monkeypatch, store, body):
    connect(store, REPO)
    net(monkeypatch, {("GET", f"https://api.github.com/repos/{REPO}"): body})
    with pytest.raises(flow.GitHubError) as info:
        flow.connected_repo()
    assert info.value.code == "github.repo_failed"


def test_connected_repo_needs_a_connection(monkeypatch):
    net(monkeypatch, {})
    with pytest.raises(flow.GitHubError) as caught:
        flow.connected_repo()
    assert caught.value.code == "github.not_connected"


@pytest.mark.parametrize("status", [500, 403, 429])
def test_connected_repo_says_unreachable_when_github_fails(monkeypatch, store, status):
    connect(store, REPO)
    net(monkeypatch, {("GET", f"https://api.github.com/repos/{REPO}"): status})
    with pytest.raises(flow.GitHubError) as caught:
        flow.connected_repo()
    assert caught.value.code == "github.unreachable" and store.items


def test_the_app_mounts_the_routes():
    from hokma.core.app import create_app
    paths = {r.path for r in create_app().routes}
    assert {"/api/github", "/api/github/connect", "/api/github/repos", "/api/github/repo"} <= paths


def test_the_catalogs_carry_every_message():
    for code in ("github.not_connected", "github.unreachable", "github.store_unavailable", "github.repo_failed",
                 "github.no_repo", "github.repo_gone"):
        assert code in catalog("es") and code in catalog("en")


@pytest.mark.parametrize("status", [500, 502, 503, 403, 429])
def test_a_github_failure_is_unreachable_and_the_connection_stays(client, monkeypatch, store, status):
    connect(store)
    net(monkeypatch, {("GET", USER): status})
    with pytest.raises(flow.GitHubError) as caught:
        flow.user()
    assert caught.value.code == "github.unreachable"
    assert store.items                                       # the token is kept: only a 401 means disconnected
    body = client.get("/api/github", headers=AUTH).json()
    assert body["connected"] is False and body["offline"] is True


def test_a_401_is_the_only_status_that_disconnects(monkeypatch, store):
    connect(store)
    net(monkeypatch, {("GET", USER): 401})
    assert flow.user() is None and not store.items
    with pytest.raises(flow.GitHubError) as caught:
        flow.connected_repo()
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

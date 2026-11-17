"""GitHub connection of the backup: a GitHub App (device flow, no secret) installed by the student on ONE repository.
The user token of a GitHub App only reaches the repositories of the installation, so Hokma never sees the rest of the
account. The token lives only in the system credential store (`KeyStore`, entry `github`); the chosen repository
("owner/name") is kept next to it. Every request goes through `hokma.update.github._open`, the single network point."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from hokma.connections.store import KeyStore, StoreUnavailable
from hokma.update import github

# GitHub App "Hokma Respaldo" of SiomTrading (app id 5802690). Both values are public: the device flow of a GitHub App
# needs no client secret and no private key.
CLIENT_ID = "Iv23li6zbHcTgEEiHNP6"
APP_SLUG = "hokma-respaldo"
INSTALL_URL = f"https://github.com/apps/{APP_SLUG}/installations/new"       # GitHub's "Only select repositories" picker
NEW_REPO_URL = "https://github.com/new?name=hokma-respaldo&visibility=private"
LEGACY_APP_URL = "https://github.com/settings/connections/applications/Ov23ctVfAiSnfjdeYKpW"   # the old OAuth App
KEY = "github"
KEY_APP = "github-app"          # the client_id the token was issued to: any other token is the old all-repositories one
KEY_REPO = "github-repo"        # "owner/name" the student picked
KEY_LEGACY = "github-legacy"    # set when an old token was dropped, until the student connects again
_pending: dict = {}
PAGES = 5                       # 500 repositories is far beyond what a student installs


class GitHubError(Exception):
    """`code` is a catalog key (`github.*`)."""

    def __init__(self, code: str, status: int = 502):
        super().__init__(code)
        self.code, self.status = code, status


def _call(method: str, url: str, data: dict | None = None, token: str | None = None) -> tuple[int, dict]:
    body = urllib.parse.urlencode(data).encode() if data and url.startswith("https://github.com/") else \
        json.dumps(data).encode() if data else None
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Accept", "application/vnd.github+json" if token else "application/json")
    if data and body and not url.startswith("https://github.com/"):
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_unredirected_header("Authorization", f"Bearer {token}")
    try:
        with github._open(request) as response:
            return response.status, json.loads(response.read(github.MAX_API_BYTES) or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except (urllib.error.URLError, OSError, ValueError):
        raise GitHubError("github.unreachable") from None


def token() -> str | None:
    """The saved token, unless it is the old all-repositories one: that one is dropped here, for every caller."""
    store = KeyStore()
    saved = store.get(KEY)
    if saved and store.get(KEY_APP) != CLIENT_ID:
        _drop_legacy(store)
        return None
    return saved


def _drop_legacy(store: KeyStore) -> None:
    """The OAuth App token reached every repository of the account. It is deleted and the student is told to connect
    again and pick a repository (`legacy()`); the old authorization itself can only be revoked by them on GitHub."""
    store.delete(KEY)
    store.delete(KEY_REPO)
    store.set(KEY_LEGACY, "1")


def legacy() -> bool:
    return bool(KeyStore().get(KEY_LEGACY))


def repo_name() -> str | None:
    """"owner/name" the student picked, or None. Without a token there is no repository."""
    return KeyStore().get(KEY_REPO) if token() else None


def start() -> dict:
    status, body = _call("POST", "https://github.com/login/device/code", {"client_id": CLIENT_ID})
    if status != 200 or "device_code" not in body:
        raise GitHubError("github.unreachable")
    _pending.update(device_code=body["device_code"], interval=body.get("interval", 5),
                    expires=time.monotonic() + body.get("expires_in", 900),
                    user_code=body["user_code"], verification_uri=body["verification_uri"])
    return {"user_code": body["user_code"], "verification_uri": body["verification_uri"],
            "interval": _pending["interval"], "expires_in": body.get("expires_in", 900)}


def pending() -> dict | None:
    """The code the student still has to type, for a panel that was reloaded in the middle of the flow."""
    if not _pending:
        return None
    left = _pending["expires"] - time.monotonic()
    if left <= 0 or "user_code" not in _pending:
        return None
    return {"user_code": _pending["user_code"], "verification_uri": _pending["verification_uri"],
            "interval": _pending["interval"], "expires_in": int(left)}


def poll() -> dict:
    """One check. `status`: none (nothing started), pending, expired, denied, connected."""
    if not _pending:
        return {"status": "none"}
    if time.monotonic() > _pending["expires"]:
        _pending.clear()
        return {"status": "expired"}
    _, body = _call("POST", "https://github.com/login/oauth/access_token", {
        "client_id": CLIENT_ID, "device_code": _pending["device_code"],
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
    error = body.get("error")
    if error in ("authorization_pending", "slow_down"):
        _pending["interval"] = body.get("interval", _pending["interval"])
        return {"status": "pending", "interval": _pending["interval"]}
    if "access_token" not in body:
        _pending.clear()
        return {"status": "expired" if error == "expired_token" else "denied"}
    try:
        store = KeyStore()
        store.set(KEY, body["access_token"])
        store.set(KEY_APP, CLIENT_ID)
        store.delete(KEY_REPO)                              # a new connection always asks which repository
        store.delete(KEY_LEGACY)
    except StoreUnavailable:
        raise GitHubError("github.store_unavailable", 503) from None
    _pending.clear()
    return {"status": "connected", "user": user()}


def user() -> str | None:
    """Login of the connected account, or None. A token GitHub rejects (revoked) is dropped."""
    saved = token()
    if not saved:
        return None
    status, body = _call("GET", f"{github.API_BASE}/user", token=saved)
    if status == 401:                                       # only a 401 means "not connected"
        disconnect()
        return None
    if status >= 500 or status in (403, 429):               # GitHub is failing or limiting: the connection is intact
        raise GitHubError("github.unreachable")
    return body.get("login")


def disconnect() -> None:
    _pending.clear()
    store = KeyStore()
    for key in (KEY, KEY_APP, KEY_REPO, KEY_LEGACY):
        store.delete(key)


def _get(path: str, saved: str) -> dict:
    status, body = _call("GET", f"{github.API_BASE}{path}", token=saved)
    if status == 401:
        disconnect()
        raise GitHubError("github.not_connected", 409)
    if status >= 500 or status in (403, 429):
        raise GitHubError("github.unreachable")
    return body if status == 200 and isinstance(body, dict) else {}


def repos() -> list[dict]:
    """The private repositories the student gave the App on GitHub (`Only select repositories`), as
    `{"full_name", "private"}`. Empty until the App is installed."""
    saved = token()
    if not saved:
        raise GitHubError("github.not_connected", 409)
    found: dict[str, dict] = {}
    for page in range(1, PAGES + 1):
        installations = _get(f"/user/installations?per_page=100&page={page}", saved).get("installations") or []
        for installation in installations:
            if not isinstance(installation.get("id"), int):
                continue
            for number in range(1, PAGES + 1):
                listing = _get(f"/user/installations/{installation['id']}/repositories?per_page=100&page={number}",
                               saved).get("repositories") or []
                for repo in listing:
                    if isinstance(repo, dict) and repo.get("private") is True and isinstance(repo.get("full_name"), str):
                        found[repo["full_name"]] = {"full_name": repo["full_name"], "private": True}
                if len(listing) < 100:
                    break
        if len(installations) < 100:
            break
    return sorted(found.values(), key=lambda r: r["full_name"].lower())


def select_repo(full_name: str) -> str:
    """Keep `full_name` as the repository of the backup. Only a repository of the installation can be picked."""
    if full_name not in {r["full_name"] for r in repos()}:
        raise GitHubError("github.repo_failed", 400)
    try:
        KeyStore().set(KEY_REPO, full_name)
    except StoreUnavailable:
        raise GitHubError("github.store_unavailable", 503) from None
    return full_name


def clear_repo() -> None:
    KeyStore().delete(KEY_REPO)


def connected_repo() -> dict:
    """GitHub's object of the repository the student picked, which must still be reachable and private."""
    saved = token()
    if not saved:
        raise GitHubError("github.not_connected", 409)
    name = repo_name()
    if not name:
        raise GitHubError("github.no_repo", 409)
    status, repo = _call("GET", f"{github.API_BASE}/repos/{name}", token=saved)
    if status == 401:
        disconnect()
        raise GitHubError("github.not_connected", 409)
    if status >= 500 or status in (403, 429):
        raise GitHubError("github.unreachable")
    if status == 404:                                        # removed from the installation, deleted or renamed
        raise GitHubError("github.repo_gone", 409)
    if status != 200 or not isinstance(repo, dict) or repo.get("private") is not True:
        raise GitHubError("github.repo_failed")              # a public repository would hold the projects in the clear
    return repo

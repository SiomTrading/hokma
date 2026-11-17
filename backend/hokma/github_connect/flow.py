"""GitHub device flow and the private repository of the student. The token lives only in the system credential
store (`KeyStore`, entry `github`). Every request goes through `hokma.update.github._open`, the single network point."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from hokma.connections.store import KeyStore, StoreUnavailable
from hokma.update import github

CLIENT_ID = "Ov23ctVfAiSnfjdeYKpW"           # public: a device-flow OAuth App has no secret
# Scope decision (task 3248, nit 8): `repo` is the smallest scope that works. A device-flow OAuth App has no
# per-repository scope: `public_repo` cannot create or write a PRIVATE repository, and the fine-grained permissions
# exist only for GitHub Apps, whose tokens cannot create a repository under the user. So the token can reach every
# private repository of the account; the panel says so before connecting and Hokma only touches `hokma-backup`.
SCOPE = "repo"
KEY = "github"
_pending: dict = {}


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
    return KeyStore().get(KEY)


def start() -> dict:
    status, body = _call("POST", "https://github.com/login/device/code", {"client_id": CLIENT_ID, "scope": SCOPE})
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
        KeyStore().set(KEY, body["access_token"])
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
    KeyStore().delete(KEY)


def ensure_private_repo(name: str) -> dict:
    """The student's private repository `name`, created when missing. Returns GitHub's repository object."""
    saved, login = token(), user()
    if not saved or not login:
        raise GitHubError("github.not_connected", 409)
    status, repo = _call("GET", f"{github.API_BASE}/repos/{login}/{name}", token=saved)
    if status == 404:
        status, repo = _call("POST", f"{github.API_BASE}/user/repos", {"name": name, "private": True}, saved)
    if status >= 500 or status == 429:
        raise GitHubError("github.unreachable")
    if status not in (200, 201) or not isinstance(repo, dict) or repo.get("private") is not True:
        raise GitHubError("github.repo_failed")          # an existing public repository would hold the projects in the clear
    return repo

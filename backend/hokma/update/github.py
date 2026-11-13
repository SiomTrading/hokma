"""Default update source: the latest published Release of the Hokma repository on GitHub.

The repository is public, so no GitHub account is needed: requests are anonymous (60 per hour per address).
If the student happens to have a GitHub login, Hokma borrows it for a higher rate limit: first
`git credential fill` (the helper git itself uses), then `gh auth token`. A stored token that GitHub rejects
(401) is dropped and the request is repeated anonymously. The token

- is held in memory only: never written to disk, to argv, to the environment, to logs, to error messages,
  to `repr` or to `--json`;
- travels only in an `Authorization` header added with `add_unredirected_header`, so the redirect to the
  download CDN never receives it;
- is read-only for Hokma: it never calls `git credential approve` or `reject`.

The git and gh helpers run through `safe_exec` (fresh temporary cwd, cleaned environment, absolute program
path) so a repository in the working folder cannot steer them. Every HTTP request goes through `_open`, the
single point to mock in tests.
"""
import json
import re
import urllib.error
import urllib.request

from hokma.i18n import t
from hokma.update.safe_exec import run_isolated
from hokma.update.source import SHA_RE, UpdateError, VersionManifest, parse_version

RELEASE_REPO = "SiomTrading/hokma"
API_BASE = "https://api.github.com"
API_TIMEOUT_SECONDS = 30
CREDENTIAL_TIMEOUT_SECONDS = 15
MAX_API_BYTES = 1024 * 1024
MAX_SHA_FILE_BYTES = 4 * 1024
SHA_LINE = re.compile(r"^([0-9a-fA-F]{64})(?:\s+\*?\S+)?\s*$")


class _TokenRejected(Exception):
    """GitHub answered 401 to a request that carried a token: retry without it."""


class _Status(Exception):
    """An HTTP error status from the API. Carries no request data."""

    def __init__(self, code: int, rate_limited: bool = False):
        super().__init__(f"HTTP {code}")
        self.code = code
        self.rate_limited = rate_limited


def _open(request: urllib.request.Request):
    """The only place that talks to the network. Returns a response usable as a context manager."""
    return urllib.request.urlopen(request, timeout=API_TIMEOUT_SECONDS)


def _token_from_git() -> str | None:
    r = run_isolated("git", ["credential", "fill"], input_text="protocol=https\nhost=github.com\n\n",
                     timeout=CREDENTIAL_TIMEOUT_SECONDS)
    if r is None or r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        if line.startswith("password="):
            return line[len("password="):].strip() or None
    return None


def _token_from_gh() -> str | None:
    r = run_isolated("gh", ["auth", "token", "--hostname", "github.com"], timeout=CREDENTIAL_TIMEOUT_SECONDS)
    if r is None or r.returncode != 0:
        return None
    return r.stdout.strip() or None


def github_token() -> str | None:
    """The token Hokma saved on connecting GitHub (credential store), else the one from git or gh, else None.

    The detached updater runs on the bare base Python, where `keyring` is not installed: the store is imported here,
    not at module level, and its absence only means there is no saved token (bug 1111)."""
    try:
        from hokma.connections.store import KeyStore

        saved = KeyStore().get("github")
    except ImportError:
        saved = None
    return saved or _token_from_git() or _token_from_gh()


def _get(url: str, token: str | None, accept: str, limit: int) -> bytes:
    request = urllib.request.Request(url)
    request.add_header("Accept", accept)
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    if token:
        request.add_unredirected_header("Authorization", f"Bearer {token}")
    try:
        with _open(request) as response:
            data = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        remaining = exc.headers.get("X-RateLimit-Remaining") if exc.headers else None
        raise _Status(exc.code, rate_limited=exc.code == 403 and remaining == "0") from None
    except (urllib.error.URLError, OSError) as exc:      # DNS, refused, timeout
        reason = getattr(exc, "reason", None) or exc
        raise UpdateError(t("update.github.unreachable", error=reason)) from None
    if len(data) > limit:
        raise UpdateError(t("update.github.unreachable", error="response too large"))
    return data


def _failure(status: _Status, token: str | None) -> UpdateError:
    if status.code == 401 and token:
        raise _TokenRejected()
    if status.rate_limited:
        return UpdateError(t("update.github.rate_limited"))
    if status.code in (403, 404):
        return UpdateError(t("update.github.no_access"))
    return UpdateError(t("update.github.unreachable", error=f"HTTP {status.code}"))


class GitHubReleaseSource:
    """Reads the latest Release (published, not draft, not prerelease) and builds a VersionManifest."""

    def fetch(self) -> VersionManifest:
        token = github_token()
        if token:
            try:
                return self._fetch(token)
            except _TokenRejected:                       # stale credential: the repository is public anyway
                pass
        return self._fetch(None)

    def _fetch(self, token: str | None) -> VersionManifest:
        release = self._latest(token)

        tag = release.get("tag_name") if isinstance(release, dict) else None
        if not isinstance(tag, str):
            raise UpdateError(t("update.github.release_incomplete", version="?"))
        version = tag[1:] if tag.startswith("v") else tag
        parse_version(version)

        package = f"hokma-{version}.tar.gz"
        assets = {a.get("name"): a.get("url") for a in release.get("assets") or [] if isinstance(a, dict)}
        package_url, sha_url = assets.get(package), assets.get(package + ".sha256")
        incomplete = UpdateError(t("update.github.release_incomplete", version=version))
        for url in (package_url, sha_url):
            if not isinstance(url, str) or not url.startswith(API_BASE + "/"):
                raise incomplete

        try:
            sha_text = _get(sha_url, token, "application/octet-stream", MAX_SHA_FILE_BYTES).decode("ascii").strip()
        except _Status as status:
            raise _failure(status, token) from None
        except UnicodeDecodeError:
            raise incomplete from None
        match = SHA_LINE.match(sha_text)
        if not match or not SHA_RE.match(match.group(1).lower()):
            raise incomplete
        headers = {"Accept": "application/octet-stream"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return VersionManifest(version, package_url, match.group(1).lower(), headers=headers)

    def _latest(self, token: str | None) -> dict:
        accept = "application/vnd.github+json"
        try:
            body = _get(f"{API_BASE}/repos/{RELEASE_REPO}/releases/latest", token, accept, MAX_API_BYTES)
        except _Status as status:
            if status.code != 404:
                raise _failure(status, token) from None
            try:                                         # 404 means no release or no access: tell them apart
                _get(f"{API_BASE}/repos/{RELEASE_REPO}", token, accept, MAX_API_BYTES)
            except _Status as probe:
                if probe.code in (403, 404):
                    raise UpdateError(t("update.github.no_access")) from None
                raise _failure(probe, token) from None
            raise UpdateError(t("update.github.no_release")) from None
        try:
            return json.loads(body)
        except ValueError:
            raise UpdateError(t("update.github.release_incomplete", version="?")) from None

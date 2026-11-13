"""SPEC-011 P11: keep the encrypted `.hkb` backups as assets of a private GitHub Release (tag `hokma-backup`).

The student's own private repository holds one release; every backup is an asset of it. A file larger than
`PART_SIZE` is uploaded in numbered parts and glued back on download. Only the 7 most recent backups are kept.
The token is the one Hokma already uses for updates (`hokma.update.github.github_token`): held in memory only and
sent in an `Authorization` header that is never forwarded on redirects. Every HTTP request goes through `_open`,
the single point to mock in tests.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

from hokma.backup.bundle import BackupError
from hokma.i18n import t
from hokma.update import github

TAG = "hokma-backup"
KEEP = 7
PART_SIZE = 1_900_000_000                 # GitHub caps one release asset at 2 GB
API_BASE = github.API_BASE
UPLOAD_BASE = "https://uploads.github.com"
IO_TIMEOUT_SECONDS = 600
COPY_BLOCK = 1 << 20
PART_RE = re.compile(r"^(?P<name>.+\.hkb)(?:\.part(?P<n>\d{3}))?$")


class NotFound(BackupError):
    """GitHub answered 404."""


def _open(request: urllib.request.Request):
    """The only place that talks to the network."""
    return urllib.request.urlopen(request, timeout=IO_TIMEOUT_SECONDS)


class _Slice:
    """Read-only view of `length` bytes of a file from `start`, so a part uploads without being copied to disk."""

    def __init__(self, path: Path, start: int, length: int):
        self._fh = open(path, "rb")
        self._fh.seek(start)
        self._left = length

    def read(self, size: int = -1) -> bytes:
        size = self._left if size < 0 else min(size, self._left)
        data = self._fh.read(size)
        self._left -= len(data)
        return data

    def close(self) -> None:
        self._fh.close()


def _token(token: Optional[str]) -> str:
    token = token or github.github_token()
    if not token:
        raise BackupError(t("backup.cloud.no_token"))
    return token


def _call(method: str, url: str, token: str, *, body=None, length: Optional[int] = None,
          content_type: Optional[str] = None, accept: str = "application/vnd.github+json"):
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Accept", accept)
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_unredirected_header("Authorization", f"Bearer {token}")
    if content_type:
        request.add_header("Content-Type", content_type)
    if length is not None:
        request.add_header("Content-Length", str(length))
    try:
        return _open(request)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise BackupError(t("backup.cloud.no_access")) from None
        if exc.code == 404:
            raise NotFound(t("backup.cloud.no_access")) from None
        raise BackupError(t("backup.cloud.failed", error=f"HTTP {exc.code}")) from None
    except (urllib.error.URLError, OSError) as exc:
        raise BackupError(t("backup.cloud.unreachable", error=getattr(exc, "reason", None) or exc)) from None


def _json(method: str, url: str, token: str, payload: Optional[dict] = None):
    body = json.dumps(payload).encode() if payload is not None else None
    with _call(method, url, token, body=body, length=len(body) if body is not None else None,
               content_type="application/json" if body is not None else None) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


def _release(repo: str, token: str, create: bool) -> Optional[dict]:
    try:
        return _json("GET", f"{API_BASE}/repos/{repo}/releases/tags/{TAG}", token)
    except NotFound:
        pass
    _json("GET", f"{API_BASE}/repos/{repo}", token)       # a 404 here means the repo itself is unreachable
    if not create:
        return None
    return _json("POST", f"{API_BASE}/repos/{repo}/releases", token,
                 {"tag_name": TAG, "name": "Hokma backups", "body": "Encrypted Hokma backups.",
                  "draft": False, "prerelease": True})


def _assets(release: dict, token: str) -> List[dict]:
    """All assets, following pages of 100."""
    found, page = [], 1
    while True:
        chunk = _json("GET", f"{release['url']}/assets?per_page=100&page={page}", token) or []
        found += chunk
        if len(chunk) < 100:
            return found
        page += 1


def _group(assets: List[dict]) -> Dict[str, List[dict]]:
    """Assets by backup name, parts in order. Names carry a UTC timestamp, so sorting by name sorts by age."""
    groups: Dict[str, List[dict]] = {}
    for asset in assets:
        match = PART_RE.match(asset.get("name", ""))
        if match:
            groups.setdefault(match["name"], []).append(asset)
    for parts in groups.values():
        parts.sort(key=lambda a: a["name"])
    return dict(sorted(groups.items()))


def _delete(assets: List[dict], token: str) -> None:
    for asset in assets:
        try:
            _call("DELETE", asset["url"], token).close()
        except BackupError:
            pass                                          # a leftover asset is harmless; the next prune retries


def upload(bundle_path: Path, repo: str, *, token: Optional[str] = None, keep: int = KEEP,
           part_size: int = PART_SIZE) -> dict:
    """Upload a `.hkb` as an asset of the `hokma-backup` release of `repo` ("owner/name"), then prune to `keep`."""
    token = _token(token)
    bundle_path = Path(bundle_path)
    size = bundle_path.stat().st_size
    release = _release(repo, token, create=True)
    count = max(1, -(-size // part_size))
    uploaded: List[dict] = []
    try:
        for index in range(count):
            start = index * part_size
            length = min(part_size, size - start)
            name = bundle_path.name if count == 1 else f"{bundle_path.name}.part{index + 1:03d}"
            url = f"{UPLOAD_BASE}/repos/{repo}/releases/{release['id']}/assets?" + urllib.parse.urlencode({"name": name})
            reader = _Slice(bundle_path, start, length)
            try:
                with _call("POST", url, token, body=reader, length=length,
                           content_type="application/octet-stream") as response:
                    uploaded.append(json.loads(response.read()))
            finally:
                reader.close()
    except BaseException:
        _delete(uploaded, token)                          # never leave half a backup behind
        raise
    groups = _group(_assets(release, token))
    for name in list(groups)[:-keep] if keep > 0 else []:
        _delete(groups[name], token)
    return {"name": bundle_path.name, "size": size, "parts": count}


def list_backups(repo: str, *, token: Optional[str] = None) -> List[dict]:
    """Backups in the cloud, newest first: name, size in bytes, created_at, parts. Empty when there is none yet."""
    token = _token(token)
    release = _release(repo, token, create=False)
    if release is None:
        return []
    out = [{"name": name, "size": sum(a["size"] for a in parts), "created_at": parts[0].get("created_at"),
            "parts": len(parts)} for name, parts in _group(_assets(release, token)).items()]
    return out[::-1]


def download(repo: str, name: str, dest_dir: Path, *, token: Optional[str] = None) -> Path:
    """Download backup `name` into `dest_dir` (parts are joined) and return the `.hkb` path."""
    token = _token(token)
    release = _release(repo, token, create=False)
    parts = _group(_assets(release, token)).get(name) if release else None
    if not parts:
        raise BackupError(t("backup.cloud.not_found"))
    dest = Path(dest_dir) / name
    try:
        with open(dest, "wb") as out:
            for asset in parts:
                with _call("GET", asset["url"], token, accept="application/octet-stream") as response:
                    while True:
                        block = response.read(COPY_BLOCK)
                        if not block:
                            break
                        out.write(block)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    if dest.stat().st_size != sum(a["size"] for a in parts):
        dest.unlink(missing_ok=True)
        raise BackupError(t("backup.cloud.failed", error="size mismatch"))
    return dest

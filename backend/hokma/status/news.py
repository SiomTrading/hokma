"""News: the CHANGELOG of the installed package and the latest published version."""
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from hokma import __version__
from hokma.core.config import current_slot, slot_dir
from hokma.i18n import t
from hokma.update.source import UpdateError, configured_ref, parse_version

MAX_CHANGELOG_BYTES = 256 * 1024
MAX_NOTES_CHARS = 20000
CACHE_SECONDS = 300
SECTION = re.compile(r"^##\s+(\d+\.\d+\.\d+)\s*$", re.M)


@dataclass(frozen=True)
class Latest:
    version: str
    notes: str | None = None
    published_at: str | None = None


_cache: dict[str, tuple[float, Latest]] = {}


def clear_cache() -> None:
    _cache.clear()


def changelog_path() -> Path:
    return Path(__file__).resolve().parents[3] / "CHANGELOG.md"


def parse_changelog(text: str) -> list[dict]:
    """Sections `## x.y.z` as [{version, body}] in file order. The body stays Markdown (rendered without HTML)."""
    marks = list(SECTION.finditer(text))
    out = []
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out.append({"version": mark.group(1), "body": text[mark.end():end].strip()})
    return out


def read_changelog() -> list[dict]:
    path = changelog_path()
    try:
        if path.stat().st_size > MAX_CHANGELOG_BYTES:
            return []
        return parse_changelog(path.read_text(encoding="utf-8"))
    except OSError:
        return []


def installed_at() -> str | None:
    """When this version was installed: the modification time of its runtime manifest. None in a dev checkout."""
    slot = current_slot()
    if slot is None:
        return None
    try:
        stamp = (slot_dir(slot) / "manifest.json").stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(stamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_latest() -> Latest:
    """Latest version from the update source: the override manifest if HOKMA_UPDATE_SOURCE is set, else the
    latest GitHub Release (with its notes). Raises UpdateError."""
    ref = configured_ref()
    if ref:
        from hokma.update.source import ManifestSource

        return Latest(ManifestSource(ref).fetch().version)
    from hokma.update import github

    source, token = github.GitHubReleaseSource(), github.github_token()
    try:
        release = source._latest(token)
    except github._TokenRejected:                      # stale credential: the repository is public anyway
        release = source._latest(None)
    tag = release.get("tag_name") if isinstance(release, dict) else None
    if not isinstance(tag, str):
        raise UpdateError(t("update.github.release_incomplete", version="?"))
    version = tag[1:] if tag.startswith("v") else tag
    parse_version(version)
    notes, published = release.get("body"), release.get("published_at")
    return Latest(version, notes[:MAX_NOTES_CHARS] if isinstance(notes, str) and notes.strip() else None,
                  published if isinstance(published, str) else None)


def latest_cached(refresh: bool = False) -> Latest:
    key = configured_ref() or "github"
    hit = _cache.get(key)
    if hit and not refresh and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    latest = fetch_latest()
    _cache[key] = (time.monotonic(), latest)
    return latest


def collect(refresh: bool = False) -> dict:
    latest, error = None, None
    try:
        latest = latest_cached(refresh)
    except UpdateError as exc:
        error = str(exc)
    except Exception:                                   # offline or an odd answer: the page still shows the changelog
        error = t("status.news.unreachable")
    newer = latest is not None and parse_version(latest.version) > parse_version(__version__)
    return {
        "installed": {"version": __version__, "installed_at": installed_at()},
        "changelog": read_changelog(),
        "latest": None if latest is None else {"version": latest.version, "notes": latest.notes,
                                               "published_at": latest.published_at},
        "latest_error": error,
        "newer": newer,
    }

"""Where a new Hokma version comes from.

`UpdateSource` is the seam. The default source is the latest published GitHub Release of the Hokma
repository (`GitHubReleaseSource`, hokma/update/github.py). A local manifest file or an https URL
(`ManifestSource`) is the override, chosen with `--from` or HOKMA_UPDATE_SOURCE. A later
`LicenseServerSource` can implement the same method without touching the update runner.

Version manifest (JSON):
    {"version": "0.2.0",
     "url": "hokma-0.2.0.tar.gz",         # tar.gz or zip; relative to the manifest, or https://...
     "sha256": "<64 hex of the archive>"}

[RISK] The manifest is the root of trust of an update: whoever controls it controls what gets installed.
Over https the transport is authenticated, but there is no signature yet; the license-server source
should sign it.
"""
import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from hokma.i18n import t

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_MANIFEST_BYTES = 64 * 1024


class UpdateError(RuntimeError):
    """A failed update. The previous version is still the active one."""


@dataclass(frozen=True)
class VersionManifest:
    version: str
    url: str          # absolute: a local path or an https URL
    sha256: str
    # Extra request headers for the download (for example Authorization). Never shown, never compared.
    headers: dict[str, str] = field(default_factory=dict, repr=False, compare=False)


class UpdateSource(Protocol):
    def fetch(self) -> VersionManifest: ...


def parse_version(v: str) -> tuple[int, int, int]:
    if not VERSION_RE.match(v):
        raise UpdateError(t("update.invalid_version", version=repr(v)))
    a, b, c = v.split(".")
    return int(a), int(b), int(c)


def validate_manifest(raw: object, base: str) -> VersionManifest:
    if not isinstance(raw, dict):
        raise UpdateError(t("update.manifest_not_object"))
    missing = [k for k in ("version", "url", "sha256") if not isinstance(raw.get(k), str) or not raw[k]]
    if missing:
        raise UpdateError(t("update.manifest_missing", fields=", ".join(missing)))
    parse_version(raw["version"])
    if not SHA_RE.match(raw["sha256"].lower()):
        raise UpdateError(t("update.manifest_bad_sha"))
    url = raw["url"]
    if url.startswith("http://"):
        raise UpdateError(t("update.https_only"))
    if not url.startswith("https://"):
        if base.startswith("https://"):
            url = urllib.parse.urljoin(base, url)
        else:
            url = str((Path(base).parent / url).resolve()) if not Path(url).is_absolute() else url
    return VersionManifest(raw["version"], url, raw["sha256"].lower())


class ManifestSource:
    """Reads a version manifest from a local file or an https URL."""

    def __init__(self, ref: str):
        self.ref = ref

    def fetch(self) -> VersionManifest:
        try:
            if self.ref.startswith("https://"):
                with urllib.request.urlopen(self.ref, timeout=30) as r:
                    data = r.read(MAX_MANIFEST_BYTES + 1)
            elif self.ref.startswith("http://"):
                raise UpdateError(t("update.source_https_or_local"))
            else:
                data = Path(self.ref).read_bytes()
            if len(data) > MAX_MANIFEST_BYTES:
                raise UpdateError(t("update.manifest_too_big"))
            return validate_manifest(json.loads(data), self.ref)
        except UpdateError:
            raise
        except (OSError, ValueError) as exc:
            raise UpdateError(t("update.manifest_unreadable", ref=self.ref, error=exc)) from exc


def configured_ref() -> str | None:
    """Update source override for this install: the HOKMA_UPDATE_SOURCE variable, if set."""
    import os

    return os.environ.get("HOKMA_UPDATE_SOURCE") or None


def resolve_source(ref: str | None) -> UpdateSource:
    """`ref` (the --from option) or the variable selects a manifest; otherwise the latest GitHub Release."""
    chosen = ref or configured_ref()
    if chosen:
        return ManifestSource(chosen)
    from hokma.update.github import GitHubReleaseSource

    return GitHubReleaseSource()

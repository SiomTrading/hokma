"""Download, verify and safely extract a release archive."""
import hashlib
import shutil
import stat
import tarfile
import urllib.request
import zipfile
from pathlib import Path

from hokma.i18n import t
from hokma.update.source import UpdateError

MAX_ARCHIVE_BYTES = 500 * 1024 * 1024
MAX_UNPACKED_BYTES = 1500 * 1024 * 1024
REQUIRED = ("pyproject.toml", "uv.lock", "backend/hokma/__init__.py", "cli/hokma_cli/main.py",
            "dashboard/dist/index.html", "installer/lock.json", "kit/template")


def fetch_archive(url: str, dest: Path, expected_sha256: str, headers: dict[str, str] | None = None) -> None:
    """Copy or download the archive to `dest` and verify its sha256. Raises BEFORE anything is unpacked.

    `headers` are sent only to `url`: they are added as unredirected headers, so a redirect (the download CDN)
    never receives them."""
    h = hashlib.sha256()
    total = 0
    try:
        if url.startswith("https://"):
            request = urllib.request.Request(url)
            for name, value in (headers or {}).items():
                request.add_unredirected_header(name, value)
            src = urllib.request.urlopen(request, timeout=60)
        else:
            src = open(url, "rb")
        with src, open(dest, "wb") as out:
            while chunk := src.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_ARCHIVE_BYTES:
                    raise UpdateError(t("update.package_too_big"))
                h.update(chunk)
                out.write(chunk)
    except UpdateError:
        raise
    except OSError as exc:
        raise UpdateError(t("update.package_fetch_failed", url=url, error=exc)) from exc
    if h.hexdigest() != expected_sha256:
        dest.unlink(missing_ok=True)
        raise UpdateError(t("update.package_sha_mismatch"))


def _check_name(name: str) -> None:
    p = Path(name)
    if p.is_absolute() or ".." in p.parts or name.startswith(("/", "\\")) or (p.parts and ":" in p.parts[0]):
        raise UpdateError(t("update.package_unsafe_path", name=repr(name)))


def extract(archive: Path, dest: Path) -> Path:
    """Extract without links, devices or path tricks; return the release root folder."""
    dest.mkdir(parents=True, exist_ok=True)
    try:
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as z:
                infos = z.infolist()
                if sum(i.file_size for i in infos) > MAX_UNPACKED_BYTES:
                    raise UpdateError(t("update.package_unpacked_too_big"))
                for i in infos:
                    _check_name(i.filename)
                    if stat.S_ISLNK(i.external_attr >> 16):
                        raise UpdateError(t("update.package_symlink", name=repr(i.filename)))
                z.extractall(dest)
        elif tarfile.is_tarfile(archive):
            with tarfile.open(archive) as tar:
                members = tar.getmembers()
                if sum(m.size for m in members) > MAX_UNPACKED_BYTES:
                    raise UpdateError(t("update.package_unpacked_too_big"))
                for m in members:
                    _check_name(m.name)
                    if not (m.isfile() or m.isdir()):
                        raise UpdateError(t("update.package_bad_member", name=repr(m.name)))
                tar.extractall(dest, filter="data")
        else:
            raise UpdateError(t("update.package_not_archive"))
    except (tarfile.TarError, zipfile.BadZipFile, OSError) as exc:
        raise UpdateError(t("update.package_unpack_failed", error=exc)) from exc

    root = dest
    children = [c for c in dest.iterdir()]
    if not (dest / "pyproject.toml").exists() and len(children) == 1 and children[0].is_dir():
        root = children[0]
    missing = [r for r in REQUIRED if not (root / r).exists()]
    if missing:
        shutil.rmtree(dest, ignore_errors=True)
        raise UpdateError(t("update.package_structure", missing=", ".join(missing)))
    return root

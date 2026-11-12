"""Download of the embedding model, in the background, with no action from the student (SPEC-010 §4.4).

Facts about the files (URL pinned to a commit, size, sha256) come from `models.json`, never from the interface.
The download is resumable (`.part` file + Range), verified by size and sha256, and renamed atomically.
Failures retry with a growing wait while Hokma is open; after 3 in a row the state is `failed` (the interface
shows one "Retry" button) but automatic retries keep running."""
import hashlib
import http.client
import json
import os
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable

from hokma.core.config import hokma_data_dir

MANIFEST_PATH = Path(__file__).with_name("models.json")
RETRY_WAITS = (60, 300, 900, 3600)  # 1, 5, 15 min, then every hour
FAILURES_BEFORE_FAILED = 3
CHUNK = 256 * 1024
TIMEOUT = 30

DOWNLOADING, READY, FAILED = "downloading", "ready", "failed"


class ModelError(Exception):
    """A download attempt failed (network, size or sha256). Never shown raw to the student."""


def load_manifest(path: Path = MANIFEST_PATH) -> dict:
    """The packaged manifest. Every URL must be https."""
    data = json.loads(path.read_text(encoding="utf-8"))["embedding"]
    for entry in data["files"]:
        if not entry["url"].startswith("https://"):
            raise ValueError(f"model url is not https: {entry['name']}")
    return data


def models_dir() -> Path:
    return hokma_data_dir() / "models"


def file_ok(path: Path, size: int, sha256: str) -> bool:
    try:
        if path.stat().st_size != size:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest() == sha256
    except OSError:
        return False


class ModelDownloader:
    def __init__(self, manifest: dict | None = None, directory: Path | None = None,
                 waits: tuple[int, ...] = RETRY_WAITS, opener: Callable = urllib.request.urlopen) -> None:
        self.manifest = manifest if manifest is not None else load_manifest()
        self.directory = directory if directory is not None else models_dir() / self.manifest["id"]
        self.waits = waits
        self._open = opener
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = DOWNLOADING
        self._done_bytes = 0
        self._part_bytes = 0
        self._failures = 0
        self._total = sum(f["size"] for f in self.manifest["files"])

    # --- state -------------------------------------------------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            got = self._total if self._state == READY else min(self._total, self._done_bytes + self._part_bytes)
            percent = 100 if self._state == READY else int(got * 100 / self._total) if self._total else 0
            return {"state": self._state, "percent": percent, "bytes": got, "total": self._total}

    def path_of(self, name: str) -> Path:
        return self.directory / name

    def is_ready(self) -> bool:
        return self.status()["state"] == READY

    # --- control -----------------------------------------------------------------------------------------------
    def start(self) -> None:
        """Launch the background download once. Safe to call on every start."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="hokma-model-download", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def retry(self) -> bool:
        """Only valid in `failed`: restart the wait and download now. Returns False in any other state."""
        with self._lock:
            if self._state != FAILED:
                return False
            self._state = DOWNLOADING
            self._failures = 0
        self._wake.set()
        return True

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    # --- work --------------------------------------------------------------------------------------------------
    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                self._fetch_all()
                with self._lock:
                    self._state = READY
                    self._failures = 0
                return
            except ModelError:
                with self._lock:
                    self._failures += 1
                    if self._failures >= FAILURES_BEFORE_FAILED:
                        self._state = FAILED
                wait = self.waits[min(attempt, len(self.waits) - 1)]
                attempt += 1
                self._wake.clear()
                self._wake.wait(wait)
                if self._wake.is_set() and not self._stop.is_set():
                    attempt = 0  # a manual retry restarts the growing wait

    def _fetch_all(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._done_bytes = 0
            self._part_bytes = 0
        for entry in self.manifest["files"]:
            if self._stop.is_set():
                raise ModelError("stopped")
            target = self.path_of(entry["name"])
            if file_ok(target, entry["size"], entry["sha256"]):
                with self._lock:
                    self._done_bytes += entry["size"]
                continue
            self._fetch_one(entry, target)
            with self._lock:
                self._done_bytes += entry["size"]
                self._part_bytes = 0

    def _fetch_one(self, entry: dict, target: Path) -> None:
        part = target.with_name(target.name + ".part")
        have = part.stat().st_size if part.exists() else 0
        if have > entry["size"]:
            part.unlink()
            have = 0
        request = urllib.request.Request(entry["url"], headers={"User-Agent": "Hokma"})
        if have:
            request.add_header("Range", f"bytes={have}-")
        try:
            with self._open(request, timeout=TIMEOUT) as response:
                if have and getattr(response, "status", 200) != 206:
                    have = 0  # the server ignored Range: start over
                with part.open("ab" if have else "wb") as out:
                    written = have
                    while True:
                        if self._stop.is_set():
                            raise ModelError("stopped")
                        block = response.read(CHUNK)
                        if not block:
                            break
                        out.write(block)
                        written += len(block)
                        with self._lock:
                            self._part_bytes = written
        except (OSError, http.client.HTTPException) as exc:  # URLError, HTTPError and timeouts are OSError
            raise ModelError(str(exc)) from exc
        if part.stat().st_size < entry["size"]:
            raise ModelError(f"cut short: {entry['name']}")  # keep the partial: the next attempt resumes it
        if not file_ok(part, entry["size"], entry["sha256"]):
            part.unlink(missing_ok=True)  # a corrupt partial must not be resumed
            raise ModelError(f"size or sha256 mismatch: {entry['name']}")
        os.replace(part, target)


_DOWNLOADER: ModelDownloader | None = None


def downloader() -> ModelDownloader:
    """The one downloader of this process."""
    global _DOWNLOADER
    if _DOWNLOADER is None:
        _DOWNLOADER = ModelDownloader()
    return _DOWNLOADER


def reset_for_tests() -> None:
    global _DOWNLOADER
    if _DOWNLOADER is not None:
        _DOWNLOADER.stop()
    _DOWNLOADER = None

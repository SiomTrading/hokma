"""The Whisper model on disk: where it lives, whether it is complete, and a background download with progress.

The model is fetched once, the first time voice is used, into `<data>/models/whisper/<size>`. Only the model files
come from the network; audio is never sent anywhere.
"""
import os
import socket
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hokma.core.config import hokma_data_dir

DEFAULT_SIZE = "small"                 # about 480 MB; good enough for Spanish and English, runs on CPU
KNOWN_SIZES = ("tiny", "base", "small", "medium")
APPROX_DOWNLOAD_MB = {"tiny": 75, "base": 145, "small": 485, "medium": 1530}
ALLOW_PATTERNS =["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]
MARKER = ".hokma-complete"


def model_size() -> str:
    chosen = os.environ.get("HOKMA_WHISPER_MODEL", "").strip().lower()
    return chosen if chosen in KNOWN_SIZES else DEFAULT_SIZE


def model_dir(size: str) -> Path:
    return hokma_data_dir() / "models" / "whisper" / size


def repo_id(size: str) -> str:
    return f"Systran/faster-whisper-{size}"


def classify_download_error(error: BaseException) -> str:
    """'offline' when the cause is the network, 'disk' when the disk is full or not writable, else 'download_failed'."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        name = type(current).__name__
        if isinstance(current, (ConnectionError, TimeoutError, socket.gaierror, socket.timeout)):
            return "offline"
        if any(word in name for word in ("Connect", "Timeout", "Offline", "LocalEntryNotFound", "NameResolution")):
            return "offline"
        if isinstance(current, OSError) and current.errno in (28, 13, 30):   # ENOSPC, EACCES, EROFS
            return "disk"
        current = current.__cause__ or current.__context__
    return "download_failed"


@dataclass
class DownloadState:
    state: str = "missing"             # missing | downloading | ready | error
    bars: dict[str, list[int]] = field(default_factory=dict)       # bar name -> [bytes done, bytes total]
    error_code: str | None = None

    def counters(self) -> tuple[int, int]:
        """(done, total) of the biggest byte bar. The hub library draws several bars (per file, overall,
        reconstruction) depending on its version; the biggest one is always the model file."""
        if not self.bars:
            return 0, 0
        done, total = max(self.bars.values(), key=lambda bar: bar[1])
        return min(done, total) if total else done, total


class ModelManager:
    def __init__(self, size: str | None = None, fetch=None) -> None:
        self.size = size or model_size()
        self._fetch = fetch or self._fetch_from_hub
        self._lock = threading.Lock()
        self._download = DownloadState()
        self._thread: threading.Thread | None = None

    @property
    def path(self) -> Path:
        return model_dir(self.size)

    def is_ready(self) -> bool:
        return (self.path / MARKER).is_file() and (self.path / "model.bin").is_file()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            d = DownloadState(self._download.state, {k: list(v) for k, v in self._download.bars.items()},
                              self._download.error_code)
        done, total = d.counters()
        if self.is_ready():
            return {"state": "ready", "bytes_done": total or done, "bytes_total": total or done, "progress": 1.0,
                    "error_code": None}
        if d.state == "ready":                      # the folder was removed after a download
            d.state = "missing"
        progress = min(1.0, done / total) if total else 0.0
        return {"state": d.state, "bytes_done": done, "bytes_total": total, "progress": progress,
                "error_code": d.error_code}

    def start_download(self) -> None:
        """Starts the download in the background; a no-op while one runs or when the model is already complete."""
        with self._lock:
            if self.is_ready() or self._download.state == "downloading":
                return
            self._download = DownloadState(state="downloading")
            self._thread = threading.Thread(target=self._run, name="hokma-whisper-download", daemon=True)
            self._thread.start()

    def wait(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _report(self, bar: str, total: int | None = None, done: int = 0) -> None:
        """Called by the download for every byte bar: its (new) total and the bytes just received."""
        with self._lock:
            counters = self._download.bars.setdefault(bar, [0, 0])
            if total:
                counters[1] = max(counters[1], int(total))
            counters[0] += int(done)

    def _run(self) -> None:
        try:
            self.path.mkdir(parents=True, exist_ok=True)
            self._fetch(repo_id(self.size), self.path, self._report)
            if not (self.path / "model.bin").is_file():
                raise FileNotFoundError("model.bin missing after the download")
            (self.path / MARKER).write_text(self.size, encoding="ascii")
            with self._lock:
                self._download.state = "ready"
        except BaseException as exc:                # the thread must always end in a visible state
            with self._lock:
                self._download.state = "error"
                self._download.error_code = classify_download_error(exc)

    @staticmethod
    def _fetch_from_hub(repo: str, target: Path, report) -> None:
        from huggingface_hub import snapshot_download
        from tqdm.auto import tqdm

        class Progress(tqdm):
            """Reports instead of drawing: the bar is disabled, so the counters are kept here."""
            counter = 0

            def __init__(self, *args, **kwargs):
                kwargs.pop("name", None)
                self._is_bytes = kwargs.get("unit") == "B"
                Progress.counter += 1
                self._key = f"bar{Progress.counter}"
                kwargs["disable"] = True
                super().__init__(*args, **kwargs)
                if self._is_bytes:
                    report(self._key, total=kwargs.get("total"))

            def update(self, n=1):
                if self._is_bytes:
                    report(self._key, total=self.total, done=n or 0)
                return super().update(n)

        snapshot_download(repo, local_dir=str(target), allow_patterns=ALLOW_PATTERNS, tqdm_class=Progress)

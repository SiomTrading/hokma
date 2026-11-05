"""Small on-disk cache for upstream answers, plus a sliding-window rate limiter. Standard library only."""
import hashlib
import json
import os
import threading
import time
from collections import deque
from pathlib import Path

MAX_ENTRIES = 300


class DiskCache:
    """One JSON file per key: `{"fetched_at": epoch, "body": text}`. Writes are atomic."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def _file(self, key: str) -> Path:
        return self.folder / (hashlib.sha256(key.encode("utf-8")).hexdigest()[:32] + ".json")

    def get(self, key: str) -> tuple[int, str] | None:
        try:
            data = json.loads(self._file(key).read_text(encoding="utf-8"))
            return int(data["fetched_at"]), str(data["body"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def put(self, key: str, body: str, now: int) -> None:
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            target = self._file(key)
            tmp = target.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps({"fetched_at": now, "body": body}), encoding="utf-8")
            os.replace(tmp, target)
            self._prune()
        except OSError:
            pass  # a cache that cannot be written is only a slower app

    def _prune(self) -> None:
        files = [p for p in self.folder.glob("*.json")]
        if len(files) <= MAX_ENTRIES:
            return
        files.sort(key=lambda p: p.stat().st_mtime)
        for old in files[: len(files) - MAX_ENTRIES]:
            try:
                old.unlink()
            except OSError:
                pass


class RateLimiter:
    """At most `limit` events in any `window` seconds."""

    def __init__(self, limit: int, window: float, clock=time.monotonic) -> None:
        self.limit = limit
        self.window = window
        self._clock = clock
        self._events: deque[float] = deque()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        now = self._clock()
        with self._lock:
            while self._events and now - self._events[0] >= self.window:
                self._events.popleft()
            if len(self._events) >= self.limit:
                return False
            self._events.append(now)
            return True

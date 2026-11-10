"""What the Novedades screen needs to know about updates, in one place.

- `check()` asks the update source for the latest version (through `hokma.status.news`) and remembers when it
  last succeeded in `profile.update_checked_at`.
- `auto_check_loop()` runs `check()` shortly after the engine starts and then every 24 hours.
- The update itself runs in a detached process outside the runtime slots (see `hokma.update.detached`), while this
  engine is down. When Hokma comes back, `snapshot()` reads the outcome that process left in
  `<data>/update-status.json`: a failure becomes the phase `error` with its message, so the panel can show it with
  "Retry". `hokma update --progress-file` still reports stages to the small progress file read here.
"""
import asyncio
import json
import os
import threading
import time
from datetime import datetime, timezone

from hokma import __version__
from hokma.core import config
from hokma.core.session import supervised
from hokma.status import news
from hokma.update.source import parse_version

AUTO_CHECK_FIRST_DELAY_SECONDS = 5
AUTO_CHECK_INTERVAL_SECONDS = 24 * 60 * 60
STAGES = ("download", "unpack", "python", "dependencies", "verify", "switch")


def progress_path():
    return config.hokma_data_dir() / "update-progress.json"


class ProgressWriter:
    """Used inside the update child process: records the current stage atomically."""

    def __init__(self, path) -> None:
        self.path = path
        self.started_at = time.time()

    def __call__(self, stage: str) -> None:
        body = {"stage": stage, "started_at": self.started_at, "stage_at": time.time()}
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(body), encoding="ascii")
            os.replace(tmp, self.path)
        except OSError:
            pass                                          # progress is a courtesy; the update goes on


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class UpdateState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.phase = "idle"             # idle | updating | restarting | restart_required | error
        self.error: str | None = None
        self.target_version: str | None = None
        self._began: float | None = None
        self._checked_fallback: float | None = None

    # ---- the check -----------------------------------------------------------------------------------------

    def check(self, db, refresh: bool) -> dict:
        """The `/api/news` payload, plus `checked_at`. A successful network check is stored in the profile."""
        before = news.last_fetch_at()
        payload = news.collect(refresh)
        after = news.last_fetch_at()
        if after is not None and after != before:
            self._checked_fallback = after
            self._store_checked(db, after)
        payload["checked_at"] = self.checked_at(db)
        return payload

    def _store_checked(self, db, epoch: float) -> None:
        stamp = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        try:
            with db.write() as unit:
                if unit.profile.get() is not None:        # before the first run there is no profile row yet
                    unit.profile.save(update_checked_at=stamp)
        except Exception:
            pass                                          # remembered in memory; the screen still works

    def checked_at(self, db) -> str | None:
        try:
            with db.read() as unit:
                stored = unit.profile.get_or_default().update_checked_at
        except Exception:
            stored = None
        if stored:
            from hokma.db.timeutil import from_db_time

            return from_db_time(stored).strftime("%Y-%m-%dT%H:%M:%SZ")
        return _iso(self._checked_fallback)

    async def auto_check_loop(self, get_db) -> None:
        """First check a few seconds after start, then every 24 hours. Never raises."""
        await asyncio.sleep(AUTO_CHECK_FIRST_DELAY_SECONDS)
        while True:
            if self.phase not in ("updating", "restarting"):
                try:
                    await asyncio.to_thread(self.check, get_db(), True)
                except Exception:
                    pass
            await asyncio.sleep(AUTO_CHECK_INTERVAL_SECONDS)

    # ---- the update ----------------------------------------------------------------------------------------

    def begin_update(self) -> bool:
        with self._lock:
            if self.phase in ("updating", "restarting"):
                return False
            self.phase, self.error, self.target_version = "updating", None, None
            self._began = time.time()
        progress_path().unlink(missing_ok=True)
        from hokma.update import detached

        detached.clear_status()
        return True

    def launched(self, target_version: str) -> None:
        """The detached updater is running and this engine is about to shut down for it."""
        self.target_version = target_version
        self.phase = "restarting"

    def finish_update(self, result: dict, restarting: bool) -> None:
        self.target_version = result.get("to_version")
        self.phase = "idle" if result.get("status") != "updated" else ("restarting" if restarting else "restart_required")

    def fail_update(self, message: str) -> None:
        self.phase, self.error = "error", message

    # ---- what the screen reads -----------------------------------------------------------------------------

    def snapshot(self, db) -> dict:
        stage, elapsed, stage_elapsed = None, None, None
        phase, error = self.phase, self.error
        if phase == "idle":
            from hokma.update import detached

            failed = detached.failed_outcome()          # the last detached update failed: say why, offer Retry
            if failed is not None:
                phase, error = "error", failed
        if self.phase == "updating":
            now = time.time()
            elapsed = max(0, int(now - (self._began or now)))
            try:
                body = json.loads(progress_path().read_text(encoding="ascii"))
                if body.get("stage") in STAGES:
                    stage = body["stage"]
                    stage_elapsed = max(0, int(now - float(body["stage_at"])))
            except (OSError, ValueError, KeyError, TypeError):
                pass
        latest = news.cached_latest()
        newer = False
        if latest is not None:
            try:
                newer = parse_version(latest.version) > parse_version(__version__)
            except Exception:
                newer = False
        return {
            "version": __version__,
            "phase": phase,
            "stage": stage,
            "elapsed_s": elapsed,
            "stage_elapsed_s": stage_elapsed,
            "error": error,
            "target_version": self.target_version,
            "checked_at": self.checked_at(db),
            "newer": newer,
            "latest_version": latest.version if latest else None,
            "supervised": supervised(),
            "engine_log": str(config.engine_log_path()),
        }

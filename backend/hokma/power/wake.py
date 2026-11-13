"""Wake the computer before the next routine. Windows only: macOS has no scheduled wake (needs root), so it reports
`unsupported` and the keep-awake block is all there is. States: ok, off, needs_permission, unsupported."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from hokma.db import from_db_time
from hokma.i18n import t

LEAD = timedelta(seconds=120)


class Backend(Protocol):
    def check(self) -> str | None: ...
    def schedule(self, at: datetime) -> bool: ...
    def cancel(self) -> None: ...


class NullWake:
    def check(self) -> str | None:
        return "platform"

    def schedule(self, at: datetime) -> bool:
        return False

    def cancel(self) -> None:
        return None


def platform_backend(platform: str | None = None) -> Backend:
    if (platform or sys.platform) == "win32":
        from hokma.power.wake_windows import WindowsWake

        return WindowsWake()
    return NullWake()


def next_wake(db: Any, now: datetime | None = None) -> datetime | None:
    """Two minutes before the next approved, active routine; None when there is none ahead."""
    now = now or datetime.now(timezone.utc)
    with db.read() as u:
        jobs = u.scheduled_jobs.list(active=True)
    ahead = [from_db_time(j.next_run_at) - LEAD for j in jobs if j.next_run_at and j.approval_id is not None]
    return min((at for at in ahead if at > now), default=None)


class Waker:
    """Keeps exactly one wake-up registered: the next one, or none. Idempotent; talks to the system only on a change."""

    def __init__(self, backend: Backend | None = None) -> None:
        self.backend = backend or platform_backend()
        self.status, self.reason, self.at = "off", None, None
        self._key: Any = None

    def sync(self, enabled: bool, at: datetime | None) -> str:
        key = (bool(enabled), at)
        if key == self._key:
            return self.status
        self._key = key
        self.reason = self.backend.check() if enabled else None
        if not enabled:
            if self.at is not None:
                self.backend.cancel()
            self.status, self.at = "off", None
        elif self.reason:
            self.status, self.at = "unsupported", None
        elif at is None:
            if self.at is not None:
                self.backend.cancel()
            self.status, self.at = "ok", None
        elif self.backend.schedule(at):
            self.status, self.at = "ok", at
        else:
            self.status, self.at = "needs_permission", None
        return self.status

    def info(self) -> dict[str, Any]:
        code = f"unsupported_{self.reason}" if self.status == "unsupported" else self.status
        message = "" if self.status == "off" else t(f"power.wake.{code}")
        return {"status": self.status, "at": self.at.isoformat() if self.at else None, "message": message}


_instance: dict[str, Waker | None] = {"waker": None}


def waker() -> Waker:
    if _instance["waker"] is None:
        _instance["waker"] = Waker()
    return _instance["waker"]  # type: ignore[return-value]


def apply_wake(db: Any) -> str:
    """Register the single wake-up from the stored preference and the routines ahead."""
    with db.read() as u:
        enabled = u.profile.get_or_default().wake_enabled
    return waker().sync(enabled, next_wake(db) if enabled else None)

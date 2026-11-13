"""Session reset policy: when a conversation is overdue and must start fresh."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional


@dataclass
class SessionResetPolicy:
    """When sessions reset: "daily" (at ``at_hour``), "idle" (after ``idle_minutes``),
    "both" (whichever first), "none" (only compression manages context)."""
    mode: str = "both"
    at_hour: int = 4  # 0-23, local time
    idle_minutes: int = 120
    notify: bool = True  # Notify the user when auto-reset occurs


def _policy_reset_reason(policy, updated_at: datetime, now: datetime) -> Optional[str]:
    """Return "idle"/"daily" when *updated_at* is overdue under *policy*, else None."""
    if policy.mode == "none":
        return None
    if policy.mode in {"idle", "both"} and now > updated_at + timedelta(minutes=policy.idle_minutes):
        return "idle"
    if policy.mode in {"daily", "both"}:
        today_reset = now.replace(hour=policy.at_hour, minute=0, second=0, microsecond=0)
        if now.hour < policy.at_hour:
            today_reset -= timedelta(days=1)
        if updated_at < today_reset:
            return "daily"
    return None


def should_reset(policy, updated_at: datetime, now: datetime, busy: bool) -> Optional[str]:
    """Reset reason ("idle"/"daily") if the policy says reset, else None; a busy session never resets."""
    if busy:
        return None
    return _policy_reset_reason(policy, updated_at, now)

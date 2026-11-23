"""Sentinels and the common result of every data call (SPEC-017, section 4.5; TA-53).

A provider that answers with no rows produces `NO_DATA`. A provider that cannot answer (down, throttled, key missing)
produces `DATA_UNAVAILABLE` with the provider and the reason. An agent always receives the sentinel and never a zero,
an empty table or a guess: `DataResult.value` is None unless the status is `ok`.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from typing import Any

OK = "ok"
NO_DATA = "NO_DATA"
DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
STALE = "STALE"
STATUSES = (OK, NO_DATA, DATA_UNAVAILABLE, STALE)

# Reasons that callers and tests rely on.
REASON_EMPTY = "empty_result"
REASON_KEY_MISSING = "provider_key_missing"
REASON_UNREACHABLE = "provider_unreachable"
REASON_RATE_LIMITED = "rate_limited"
REASON_STALE = "stale_data"
REASON_NOT_OBSERVED = "window_not_observed"
REASON_NOT_PUBLISHED = "not_published_by_as_of"
REASON_LIVE_PROFILE = "live_profile_withheld"
REASON_UNSUPPORTED = "unsupported_for_historical_as_of"


def make_module_callable(module_name: str, function_name: str) -> None:
    """Let a module be called like its main function. The package exports `macro`, `universe` and `snapshot` as functions
    (SPEC-017, 4.5) and those names are also module names: the module itself answers the call, so both uses work."""
    import sys
    import types

    module = sys.modules[module_name]

    class _CallableModule(types.ModuleType):
        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            return getattr(self, function_name)(*args, **kwargs)

    module.__class__ = _CallableModule


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_utc(value: datetime) -> datetime:
    """UTC-aware datetime; a naive value is taken as UTC."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def parse_time(value: Any) -> datetime:
    """A datetime, a date or an ISO-8601 text as a UTC-aware datetime. Raises ValueError for anything else."""
    if isinstance(value, datetime):
        return to_utc(value)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        try:
            return to_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
        except ValueError as error:
            raise ValueError(f"not a date or an ISO-8601 time: {value!r}") from error
    raise ValueError(f"not a date or an ISO-8601 time: {value!r}")


def scrub(text: str, *secrets: str | None) -> str:
    """Remove secrets (API keys) from a message before it is stored or shown."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


@dataclass(frozen=True)
class DataResult:
    """What every data function returns.

    `received_at` is when this process received the data, not when it was published. `as_of` is the cut the caller asked
    for: nothing after it is in `value`. `value` is None unless `status` is `ok`; `reason` explains any other status.
    """

    value: Any
    source: str
    received_at: datetime
    as_of: datetime | None = None
    status: str = OK
    reason: str = ""
    warnings: tuple[str, ...] = ()
    meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"unknown status {self.status!r}")
        if self.status != OK and self.value is not None:
            raise ValueError("a result that is not ok carries no value: the sentinel is the answer")
        if self.status != OK and not self.reason:
            raise ValueError("a result that is not ok must say why")

    @property
    def ok(self) -> bool:
        return self.status == OK

    def with_warnings(self, *extra: str) -> "DataResult":
        return replace(self, warnings=self.warnings + tuple(w for w in extra if w))

    def to_dict(self) -> dict[str, Any]:
        value = self.value
        if hasattr(value, "to_dict") and callable(value.to_dict):
            try:
                value = value.reset_index().to_dict(orient="records") if hasattr(value, "reset_index") else value.to_dict()
            except TypeError:
                value = value.to_dict()
        return {
            "value": value, "source": self.source, "received_at": self.received_at.isoformat(),
            "as_of": self.as_of.isoformat() if self.as_of else None, "status": self.status, "reason": self.reason,
            "warnings": list(self.warnings), "meta": dict(self.meta),
        }


def ok(value: Any, *, source: str, as_of: datetime | None = None, received_at: datetime | None = None,
       warnings: tuple[str, ...] = (), meta: dict | None = None) -> DataResult:
    return DataResult(value, source, received_at or utcnow(), as_of, OK, "", tuple(warnings), dict(meta or {}))


def no_data(source: str, reason: str, *, as_of: datetime | None = None, received_at: datetime | None = None,
            warnings: tuple[str, ...] = (), meta: dict | None = None) -> DataResult:
    """The source answered and has no rows for this request."""
    return DataResult(None, source, received_at or utcnow(), as_of, NO_DATA, reason, tuple(warnings), dict(meta or {}))


def unavailable(source: str, reason: str, *, as_of: datetime | None = None, received_at: datetime | None = None,
                warnings: tuple[str, ...] = (), meta: dict | None = None) -> DataResult:
    """The source could not answer (down, throttled, key missing). Says nothing about the instrument."""
    return DataResult(None, source, received_at or utcnow(), as_of, DATA_UNAVAILABLE, reason, tuple(warnings), dict(meta or {}))


def stale(source: str, reason: str, *, as_of: datetime | None = None, received_at: datetime | None = None,
          warnings: tuple[str, ...] = (), meta: dict | None = None) -> DataResult:
    """The source answered with data too old to use. The old rows are not returned."""
    return DataResult(None, source, received_at or utcnow(), as_of, STALE, reason, tuple(warnings), dict(meta or {}))
